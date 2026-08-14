from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from task_graph.app import TaskGraphApp
from task_graph.cli import main
from task_graph.config import Settings, set_settings
from task_graph.connectors.base import SourceItem
from task_graph.ontology.types import ApprovalState, ObjectType, SourceKind
from task_graph.pipeline.approval import ApprovalQueue


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_GRAPH_HOME", str(tmp_path))
    monkeypatch.setenv("TASK_GRAPH_EMBEDDINGS", "hashing")
    monkeypatch.setenv("TASK_GRAPH_COPILOT_HOME", str(tmp_path / ".copilot"))
    set_settings(Settings(home=tmp_path, embedding_provider="hashing"))
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


def item(uri: str, title: str, **overrides) -> SourceItem:
    payload = {
        "source": SourceKind.GITHUB,
        "source_uri": uri,
        "title": title,
        "body": "Can you please handle this?",
        "source_state": "open",
        "owner": "tykendrick",
        "url": "https://github.com/o/r/issues/1",
        "due_at": datetime.now(UTC) + timedelta(days=30),
    }
    payload.update(overrides)
    return SourceItem(**payload)


def seed_tasks(home, *items: SourceItem):
    settings = Settings(home=home, embedding_provider="hashing")
    with TaskGraphApp(settings) as app:
        app.ingestor.ingest(list(items))
        app.rank()
        return [task.id for task in app.store.find_objects(ObjectType.TASK.value)]


def seed_merge(home):
    settings = Settings(home=home, embedding_provider="hashing")
    with TaskGraphApp(settings) as app:
        app.ingestor.ingest(
            [
                item("github:issue:o/r#1", "Fix the billing pipeline timeout"),
                item(
                    "ado:workitem:2",
                    "Billing pipeline timeout fix",
                    source=SourceKind.ADO,
                    source_state="Active",
                ),
            ]
        )
        app.deduper.run()
        return app.deduper.pending_merges()[0].id


def seed_action(home):
    settings = Settings(home=home, embedding_provider="hashing")
    with TaskGraphApp(settings) as app:
        remediation = app.graph.add_object(
            ObjectType.REMEDIATION,
            {
                "action": "comment_github",
                "target_source": SourceKind.GITHUB,
                "target_uri": "github:issue:o/r#42",
                "params": {"owner_repo": "o/r", "number": 42, "body": "Approved update."},
                "preview": "Comment on GitHub o/r#42: Approved update.",
                "rationale": "test",
                "approval": ApprovalState.PENDING,
                "confidence": 0.9,
            },
        )
        return remediation.id


def test_triage_renders_priority_order_and_json(runner, isolated_env):
    old = datetime.now(UTC) - timedelta(days=1)
    future = datetime.now(UTC) + timedelta(days=20)
    seed_tasks(
        isolated_env,
        item("github:issue:o/r#1", "Overdue billing fix", due_at=old),
        item("github:issue:o/r#2", "Later coffee order", due_at=future),
    )

    result = runner.invoke(main, ["triage"])
    assert result.exit_code == 0, result.output
    assert "Overdue billing fix" in result.output
    assert result.output.index("Overdue billing fix") < result.output.index("Later coffee order")

    result = runner.invoke(main, ["--json", "triage"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["title"] == "Overdue billing fix"
    assert data[0]["priority"] >= data[1]["priority"]


def test_show_why_and_search_real_task(runner, isolated_env):
    (task_id,) = seed_tasks(
        isolated_env,
        item("github:issue:o/r#3", "Fix the billing pipeline"),
    )

    show = runner.invoke(main, ["show", task_id])
    assert show.exit_code == 0, show.output
    assert "Fix the billing pipeline" in show.output
    assert "Sources:" in show.output

    why = runner.invoke(main, ["why", task_id])
    assert why.exit_code == 0, why.output
    assert "Priority:" in why.output
    assert "Factors:" in why.output

    search = runner.invoke(main, ["search", "billing pipeline"])
    assert search.exit_code == 0, search.output
    assert task_id in search.output


def test_merges_approve_and_reject_records_correction(runner, isolated_env):
    patch_id = seed_merge(isolated_env)
    listed = runner.invoke(main, ["merges"])
    assert listed.exit_code == 0, listed.output
    assert patch_id in listed.output

    approved = runner.invoke(main, ["approve", "merge", patch_id])
    assert approved.exit_code == 0, approved.output
    assert "Approved merge" in approved.output

    patch_id = seed_merge(isolated_env / "reject")
    result = runner.invoke(
        main,
        ["--home", str(isolated_env / "reject"), "reject", "merge", patch_id, "--reason", "nope"],
    )
    assert result.exit_code == 0, result.output
    with TaskGraphApp(Settings(home=isolated_env / "reject", embedding_provider="hashing")) as app:
        corrections = app.store.find_objects(ObjectType.CORRECTION.value)
        assert corrections
        assert corrections[0].data["kind"] == "dedupe"


def test_approve_action_without_execute_never_executes(runner, isolated_env, monkeypatch):
    approval_id = seed_action(isolated_env)
    calls = []

    def fake_execute(self, remediation_id, **kwargs):
        calls.append((self, remediation_id, kwargs))
        raise AssertionError("execute_approved must not be called without --execute")

    monkeypatch.setattr(ApprovalQueue, "execute_approved", fake_execute)

    result = runner.invoke(main, ["approve", "action", approval_id])
    assert result.exit_code == 0, result.output
    assert "DRY RUN ONLY" in result.output
    assert calls == []
    with TaskGraphApp(Settings(home=isolated_env, embedding_provider="hashing")) as app:
        assert app.approvals.get(approval_id).data["approval"] == ApprovalState.GRANTED


def test_correct_not_a_task_and_learn_reports_weight_change(runner, isolated_env):
    first, second = seed_tasks(
        isolated_env,
        item("github:issue:o/r#4", "Priority is too low"),
        item("github:issue:o/r#5", "Not actually a task"),
    )

    higher = runner.invoke(main, ["correct", first, "--higher", "--reason", "important"])
    assert higher.exit_code == 0, higher.output
    dropped = runner.invoke(main, ["correct", second, "--not-a-task", "--reason", "noise"])
    assert dropped.exit_code == 0, dropped.output

    with TaskGraphApp(Settings(home=isolated_env, embedding_provider="hashing")) as app:
        assert app.store.get_object(second).data["state"] == "dropped"

    learned = runner.invoke(main, ["learn"])
    assert learned.exit_code == 0, learned.output
    assert "learned from" in learned.output
    assert "->" in learned.output


def test_doctor_runs_healthy_and_json_parseable(runner, isolated_env, monkeypatch):
    monkeypatch.setattr(
        TaskGraphApp,
        "preflight",
        lambda self: [
            {
                "check": "connector:github",
                "ok": False,
                "detail": "not configured",
                "remediation": "optional",
                "critical": False,
            }
        ],
    )

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "[WARN] connector:github" in result.output
    assert "[OK] graph_db_integrity" in result.output

    result = runner.invoke(main, ["--json", "doctor"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["checks"]


def test_init_print_config_and_idempotent_merge(runner, isolated_env):
    copilot_dir = isolated_env / ".copilot"
    config = copilot_dir / "mcp-config.json"

    printed = runner.invoke(main, ["init", "--print-config"])
    assert printed.exit_code == 0, printed.output
    assert "task-graph" in json.loads(printed.output)
    assert not config.exists()

    copilot_dir.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "keep", "args": []}}}),
        encoding="utf-8",
    )
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    written = json.loads(config.read_text(encoding="utf-8"))
    assert "other" in written["mcpServers"]
    assert "task-graph" in written["mcpServers"]

    again = runner.invoke(main, ["init"])
    assert again.exit_code == 0, again.output
    assert "already configured" in again.output
    assert json.loads(config.read_text(encoding="utf-8")) == written


@pytest.mark.parametrize(
    "args",
    [
        ["triage"],
        ["show", "missing"],
        ["why", "missing"],
        ["search", "billing"],
        ["merges"],
        ["actions"],
    ],
)
def test_empty_graph_commands_are_helpful(runner, args):
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "Run `tg sync` first" in result.output
    assert "Traceback" not in result.output
