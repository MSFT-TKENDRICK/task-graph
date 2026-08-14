"""Tests for the GitHub write client and executor client resolution."""

from __future__ import annotations

import subprocess

import pytest
from activegraph import Graph

from task_graph.connectors.base import ConnectorError
from task_graph.connectors.gh_client import GitHubCliClient
from task_graph.ontology.types import ApprovalState, ObjectType, SourceKind
from task_graph.pipeline.approval import ApprovalQueue, ApprovalRequiredError, _resolve_client
from task_graph.store import SqliteGraphStore


@pytest.fixture
def graph(tmp_path):
    store = SqliteGraphStore(tmp_path / "graph.db")
    yield Graph(graph_store=store)
    store.close()


class FakeRun:
    """Records the gh invocations instead of performing them."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            args, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def test_comment_shells_out_to_gh(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    client = GitHubCliClient(executable="gh")
    outcome = client.call_tool(
        "github_comment", {"owner_repo": "o/r", "number": 7, "body": "on it"}
    )

    assert "o/r#7" in outcome
    assert fake.calls[0][:4] == ["gh", "issue", "comment", "7"]
    assert "--repo" in fake.calls[0] and "o/r" in fake.calls[0]
    assert "on it" in fake.calls[0]


def test_close_issue_passes_the_reason_as_a_comment(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    GitHubCliClient(executable="gh").call_tool(
        "github_close_issue", {"owner_repo": "o/r", "number": 9, "reason": "shipped"}
    )
    assert fake.calls[0][:4] == ["gh", "issue", "close", "9"]
    assert "--comment" in fake.calls[0] and "shipped" in fake.calls[0]


def test_gh_failure_surfaces_the_stderr(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr="not authenticated"))
    with pytest.raises(ConnectorError, match="not authenticated"):
        GitHubCliClient(executable="gh").call_tool(
            "github_comment", {"owner_repo": "o/r", "number": 1, "body": "x"}
        )


def test_missing_gh_is_actionable(monkeypatch):
    def boom(*_a, **_kw):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ConnectorError, match="gh auth login"):
        GitHubCliClient(executable="gh").call_tool(
            "github_comment", {"owner_repo": "o/r", "number": 1, "body": "x"}
        )


def test_unknown_tool_is_refused():
    with pytest.raises(ConnectorError, match="cannot perform"):
        GitHubCliClient(executable="gh").call_tool("delete_the_repo", {})


def remediation(graph, source: SourceKind):
    return graph.add_object(
        ObjectType.REMEDIATION.value,
        {
            "action": "comment_github",
            "target_source": source.value,
            "target_uri": "github:issue:o/r#1",
            "approval": ApprovalState.PENDING.value,
        },
    )


def test_github_actions_resolve_to_the_gh_client(graph):
    """Without this, a GitHub action would try to spawn a nonexistent
    `agency mcp github` server."""
    obj = remediation(graph, SourceKind.GITHUB)
    assert isinstance(_resolve_client(obj, client=None, clients=None), GitHubCliClient)


def test_explicit_clients_still_win(graph):
    obj = remediation(graph, SourceKind.GITHUB)
    sentinel = object()
    assert _resolve_client(obj, client=sentinel, clients=None) is sentinel
    assert _resolve_client(obj, client=None, clients={"github": sentinel}) is sentinel


def test_execution_still_requires_approval(graph, monkeypatch):
    """The gh client must not create a path around the approval gate."""
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    obj = remediation(graph, SourceKind.GITHUB)
    with pytest.raises(ApprovalRequiredError):
        ApprovalQueue(graph).execute_approved(obj.id)
    assert fake.calls == []
