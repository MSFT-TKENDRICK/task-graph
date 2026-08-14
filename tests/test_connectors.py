from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from task_graph.connectors import (
    AdoConnector,
    ConnectorError,
    GitHubConnector,
    MailConnector,
    ado_workitem_uri,
    extract_external_refs,
    get_connector,
    github_discussion_uri,
    github_issue_uri,
    github_pr_uri,
    is_actionable_message,
    mail_uri,
)
from task_graph.connectors.ado import _assert_read_only, _first_org
from task_graph.connectors.mcp_client import extract_json_from_mcp_result
from task_graph.ontology.types import SourceKind

FIXTURES = Path(__file__).parent / "fixtures"


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeAgency:
    def __init__(self, tools: list[Any], payload: Any, *, fail: bool = False) -> None:
        self.tools = tools
        self.payload = payload
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Optional per-tool responses; falls back to ``payload``.
        self.responses: dict[str, Any] = {}

    def __enter__(self) -> FakeAgency:
        if self.fail:
            raise RuntimeError("server unavailable")
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def list_tools(self) -> list[Any]:
        return self.tools

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if name in self.responses:
            return self.responses[name]
        return self.payload


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_source_uri_builders_are_canonical_and_stable() -> None:
    assert github_issue_uri("Owner/Repo", 123) == "github:issue:owner/repo#123"
    assert github_pr_uri("Owner/Repo", "45") == "github:pr:owner/repo#45"
    assert github_discussion_uri("Owner/Repo", 7) == "github:discussion:owner/repo#7"
    assert ado_workitem_uri(12345) == "ado:workitem:12345"
    assert mail_uri("<message-id>") == "mail:<message-id>"
    assert github_issue_uri("Owner/Repo", 123) == github_issue_uri("owner/repo", 123)


def test_external_refs_extract_cross_system_references() -> None:
    refs = extract_external_refs("Fix AB#12345, see #123 and https://example.com/path?q=1.")
    assert refs == ["AB#12345", "#123", "https://example.com/path?q=1"]


def test_github_connector_maps_paginated_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        load_fixture("github_search_page_1.json"),
        load_fixture("github_search_page_2.json"),
        [],
        [],
    ]

    def fake_run(args: list[str], **_kwargs: Any) -> FakeCompleted:
        assert args[0] == "gh"
        if args[1:3] == ["api", "graphql"]:
            return FakeCompleted("[]")
        return FakeCompleted(json.dumps(pages.pop(0)))

    monkeypatch.setattr(subprocess, "run", fake_run)

    items = list(GitHubConnector().fetch())

    assert [item.source_uri for item in items] == [
        "github:issue:octo/repo#123",
        "github:pr:octo/repo#45",
    ]
    assert items[0].external_refs == ["AB#12345", "#77", "https://example.com/spec"]
    assert items[0].assignees == ["me"]
    assert items[1].labels == ["enhancement"]


def test_mcp_result_parsing_text_and_structured_content() -> None:
    assert extract_json_from_mcp_result(
        {"content": [{"type": "text", "text": '{"items": [{"id": 1}]}'}]}
    ) == {"items": [{"id": 1}]}
    assert extract_json_from_mcp_result({"structuredContent": {"items": [{"id": 2}]}}) == {
        "items": [{"id": 2}]
    }


def test_ado_connector_reads_work_items_from_text_block_json() -> None:
    """Tool names mirror the real Agency ADO MCP surface, verified live."""
    fake = FakeAgency(
        [{"name": "wit_work_item"}, {"name": "wit_work_item_write"}],
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "workItems": [
                                {
                                    "id": 12345,
                                    "fields": {
                                        "System.Title": "Ship connector",
                                        "System.State": "Active",
                                        "System.BoardColumn": "Doing",
                                        "System.AssignedTo": {"displayName": "Taylor"},
                                        "System.Tags": "connector;backend",
                                    },
                                }
                            ]
                        }
                    ),
                }
            ]
        },
    )

    items = list(
        AdoConnector(lambda: fake, organization="org", projects=["Proj"]).fetch()
    )

    tool, args = fake.calls[0]
    assert tool == "wit_work_item"
    assert args["action"] == "my"
    assert args["orgName"] == "org"
    assert args["project"] == "Proj"
    assert items[0].source_uri == "ado:workitem:12345"
    assert items[0].source_state == "Active / Doing"
    assert items[0].assignees == ["Taylor"]
    assert items[0].labels == ["connector", "backend"]


def test_ado_connector_never_calls_a_mutating_tool() -> None:
    """A read-only sync must not be able to select a write tool.

    Regression: fuzzy name matching once chose `wit_work_item_write` on a live
    sync, because it matched "work" and "item" and sorted first.
    """
    fake = FakeAgency(
        [{"name": "wit_work_item_write"}, {"name": "wit_work_item_comment_write"}],
        {"workItems": []},
    )
    with pytest.raises(RuntimeError, match="no `wit_work_item` tool"):
        list(AdoConnector(lambda: fake, organization="org", projects=["P"]).fetch())
    assert fake.calls == []


def test_assert_read_only_rejects_mutating_tool_names() -> None:
    for name in (
        "wit_work_item_write",
        "wiki_upsert_page",
        "repo_create_branch",
        "wit_work_item_attachment_upload",
    ):
        with pytest.raises(RuntimeError, match="Refusing to call mutating tool"):
            _assert_read_only(name)
    _assert_read_only("wit_work_item")


def test_ado_connector_asks_the_user_to_pin_org_and_projects() -> None:
    """Large tenants expose hundreds of projects; guessing is worse than asking."""
    fake = FakeAgency(
        [
            {"name": "wit_work_item"},
            {"name": "core_list_orgs"},
            {"name": "core_list_projects"},
        ],
        {"workItems": []},
    )
    fake.responses = {
        "core_list_orgs": {"result": [{"orgName": "contoso"}, {"orgName": "fabrikam"}]},
        "core_list_projects": {"result": [{"name": "Platform"}, {"name": "Billing"}]},
    }

    with pytest.raises(RuntimeError, match="TASK_GRAPH_ADO_ORG"):
        list(AdoConnector(lambda: fake).fetch())

    # The error must name the real options, not just complain.
    try:
        list(AdoConnector(lambda: fake).fetch())
    except RuntimeError as exc:
        assert "contoso" in str(exc) and "fabrikam" in str(exc)

    with pytest.raises(RuntimeError, match="TASK_GRAPH_ADO_PROJECTS"):
        list(AdoConnector(lambda: fake, organization="contoso").fetch())


def test_ado_org_names_are_read_from_the_live_key_shape() -> None:
    """The live tenant returns `orgName`, not `name`."""
    fake = FakeAgency([{"name": "core_list_orgs"}], None)
    fake.responses = {"core_list_orgs": {"result": [{"orgName": "mseng"}]}}
    assert _first_org(fake, ["core_list_orgs"]) == "mseng"


def test_ado_connector_reports_a_missing_organization() -> None:
    fake = FakeAgency([{"name": "wit_work_item"}], {"workItems": []})
    with pytest.raises(RuntimeError, match="organization"):
        list(AdoConnector(lambda: fake).fetch())


def test_mail_connector_discovers_tool_and_maps_structured_content() -> None:
    fake = FakeAgency(
        [{"name": "list_mail_messages"}],
        {
            "structuredContent": {
                "messages": [
                    {
                        "id": "msg-1",
                        "subject": "Please review AB#678",
                        "body": {"content": "Can you look at https://github.com/o/r/issues/1?"},
                        "flag": {"flagStatus": "flagged"},
                        "from": {"emailAddress": {"address": "a@example.com"}},
                    }
                ]
            }
        },
    )

    items = list(MailConnector(lambda: fake).fetch())

    assert fake.calls[0][0] == "list_mail_messages"
    assert items[0].source_uri == "mail:msg-1"
    assert items[0].owner == "a@example.com"
    assert items[0].external_refs == ["AB#678", "https://github.com/o/r/issues/1"]


def test_mail_actionability_heuristic() -> None:
    assert is_actionable_message({"subject": "Follow up", "flag": {"flagStatus": "flagged"}})
    assert is_actionable_message(
        {"subject": "Question", "body": "Can you review?", "directToMe": True}
    )
    assert not is_actionable_message({"subject": "Newsletter", "body": "unsubscribe"})
    assert not is_actionable_message({"subject": "FYI update", "body": "No action required"})


def test_is_available_never_raises_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import task_graph.connectors.github as github_module

    monkeypatch.setattr(github_module.shutil, "which", lambda _name: None)

    status = GitHubConnector().is_available()

    assert not status.available
    assert "not found" in status.detail
    assert status.remediation


def test_registry_round_trips_and_unknown_raises() -> None:
    assert isinstance(get_connector(SourceKind.GITHUB), GitHubConnector)
    assert isinstance(get_connector("ado"), AdoConnector)
    with pytest.raises(ConnectorError, match="Unknown connector kind"):
        get_connector("unknown")


def test_connectors_star_import_has_no_subprocess_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("import spawned subprocess")

    monkeypatch.setattr(subprocess, "run", fail_run)
    namespace: dict[str, Any] = {}
    exec("from task_graph.connectors import *", namespace)
    assert "GitHubConnector" in namespace
