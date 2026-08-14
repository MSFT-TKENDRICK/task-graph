from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from task_graph.app import TaskGraphApp
from task_graph.config import Settings, set_settings
from task_graph.connectors.base import SourceItem
from task_graph.mcpserver import tools
from task_graph.ontology.types import ApprovalState, CorrectionKind, SourceKind
from task_graph.pipeline import remediation


def item(uri: str = "github:issue:o/r#1", **overrides) -> SourceItem:
    payload = {
        "source": SourceKind.GITHUB,
        "source_uri": uri,
        "title": "Fix the billing pipeline",
        "body": "Please review the nightly job failure on invoices over 1000.",
        "source_state": "open",
        "owner": "tykendrick",
        "url": "https://github.com/o/r/issues/1",
        "labels": ["p1"],
        "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
        "raw": {"directToMe": True},
    }
    payload.update(overrides)
    return SourceItem(**payload)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_GRAPH_HOME", str(tmp_path))
    monkeypatch.setenv("TASK_GRAPH_EMBEDDINGS", "hashing")
    set_settings(Settings(home=tmp_path, embedding_provider="hashing"))
    with TaskGraphApp() as app:
        app.ingestor.ingest(
            [
                item(),
                item(
                    "ado:workitem:7",
                    source=SourceKind.ADO,
                    title="Update billing work item",
                    source_state="Active",
                    url="https://dev.azure.com/o/p/_workitems/edit/7",
                ),
            ]
        )
        app.rank()
        yield app
    set_settings(Settings(home=tmp_path, embedding_provider="hashing"))


def task_id(app: TaskGraphApp) -> str:
    return app.store.find_objects("task")[0].id


def github_task_id(app: TaskGraphApp) -> str:
    for task in app.store.find_objects("task"):
        if any(str(uri).startswith("github:issue:") for uri in task.data.get("source_uris") or []):
            return task.id
    raise AssertionError("seeded GitHub task not found")


def assert_jsonable(result):
    json.dumps(result)
    assert "ok" in result


def test_seeded_query_tools_return_json(app):
    tid = task_id(app)
    results = [
        tools.list_tasks(app, limit=10),
        tools.search_tasks(app, "billing", limit=5),
        tools.get_task(app, tid),
        tools.get_task_graph(app, tid, depth=1),
        tools.explain_priority(app, tid),
        tools.list_pending_merges(app),
        tools.propose_remediations(app, tid),
        tools.list_pending_approvals(app),
        tools.preview_action(app, app.pending_approvals()[0].id),
        tools.get_status(app),
        tools.run_doctor(app),
    ]
    for result in results:
        assert_jsonable(result)
        assert result["ok"] is True

    assert tools.list_tasks(app, limit=10)["tasks"]
    assert tools.search_tasks(app, "billing", limit=5)["results"]
    assert tools.get_task(app, tid)["task"]["sources"][0]["url"]
    assert "explanation" in tools.explain_priority(app, tid)["priority"]


def test_remaining_tools_return_json_even_on_clean_errors(app, monkeypatch):
    monkeypatch.setattr(
        app,
        "sync",
        lambda **_kwargs: type(
            "Report",
            (),
            {
                "summary": lambda self: "fake sync",
                "ingest": {},
                "dedupe": {},
                "ranked": 0,
                "proposed_actions": 0,
                "errors": [],
            },
        )(),
    )

    for result in [
        tools.sync_sources(app, sources=[], since=None, propose=False),
        tools.approve_merge(app, "missing-patch"),
        tools.reject_merge(app, "missing-patch", "not duplicates"),
        tools.execute_action(app, "missing-remediation"),
    ]:
        assert_jsonable(result)


def test_unknown_task_errors_are_clean(app):
    get_result = tools.get_task(app, "missing-task")
    explain_result = tools.explain_priority(app, "missing-task")

    assert get_result["ok"] is False
    assert get_result["error"]["type"] == "not_found"
    assert explain_result["ok"] is False
    assert "missing-task" in explain_result["error"]["message"]
    json.dumps(get_result)
    json.dumps(explain_result)


def test_approve_action_does_not_execute_then_execute_does(app, monkeypatch):
    tid = github_task_id(app)
    rem_1 = app.propose_for(tid)[0]
    rem_2 = app.propose_for(tid)[0]
    calls: list[dict] = []
    old = remediation._ACTIONS["comment_github"]

    def fake_executor(_client, params):
        calls.append(dict(params))
        return "fake executor called"

    monkeypatch.setitem(
        remediation._ACTIONS, "comment_github", replace(old, executor=fake_executor)
    )

    preview = tools.preview_action(app, rem_1.id)
    assert preview["ok"] is True
    assert calls == []

    approved = tools.approve_action(app, rem_1.id)
    assert approved["ok"] is True
    assert approved["remediation"]["data"]["approval"] == ApprovalState.GRANTED.value
    assert calls == []

    executed = tools.execute_action(app, rem_1.id)
    assert executed["ok"] is True
    assert executed["remediation"]["data"]["approval"] == ApprovalState.EXECUTED.value
    assert len(calls) == 1

    denied = tools.execute_action(app, rem_2.id)
    assert denied["ok"] is False
    assert "requires granted approval" in denied["error"]["message"]
    assert len(calls) == 1


def test_record_correction_and_learn_reports_weight_change(app):
    tid = task_id(app)

    recorded = tools.record_correction(
        app,
        tid,
        CorrectionKind.PRIORITY.value,
        "This is more important than ranked.",
        direction=1.0,
    )
    learned = tools.learn_from_corrections(app)

    assert recorded["ok"] is True
    assert learned["ok"] is True
    assert learned["report"]["corrections_applied"] == 1
    assert learned["report"]["weights_changed"]
    json.dumps(learned)


def test_server_imports_and_tool_metadata():
    from task_graph.mcpserver import server

    assert callable(server.main)
    registered = server.registered_tools()
    assert {tool.name for tool in registered} == set(tools.TOOLS_BY_NAME)
    assert all(tool.description.strip() for tool in registered)
    approve_description = tools.TOOLS_BY_NAME["approve_action"].description.lower()
    assert "does not execute" in approve_description
