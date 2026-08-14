from __future__ import annotations

import pytest
from activegraph import Graph

from task_graph.ontology.types import ApprovalState, ObjectType, SourceKind
from task_graph.pipeline.approval import (
    ApprovalQueue,
    ApprovalRequiredError,
    RemediationExecutionError,
    execute_approved,
    remediation_policy,
)
from task_graph.store import SqliteGraphStore


class RecordingClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("boom")
        return {"outcome": f"{name} ok"}


@pytest.fixture
def graph(tmp_path):
    store = SqliteGraphStore(tmp_path / "approval.db")
    try:
        graph = Graph(graph_store=store)
        graph._test_store = store
        yield graph
    finally:
        store.close()


def add_remediation(graph: Graph, approval: ApprovalState = ApprovalState.PENDING):
    return graph.add_object(
        ObjectType.REMEDIATION,
        {
            "action": "comment_github",
            "target_source": SourceKind.GITHUB,
            "target_uri": "github:issue:octo/repo#42",
            "params": {"owner_repo": "octo/repo", "number": 42, "body": "Approved update."},
            "preview": "Comment on GitHub octo/repo#42: Approved update.",
            "rationale": "test",
            "approval": approval,
            "confidence": 0.9,
        },
    )


@pytest.mark.parametrize(
    "state",
    [
        ApprovalState.PENDING,
        ApprovalState.REJECTED,
        ApprovalState.EXECUTED,
        ApprovalState.FAILED,
    ],
)
def test_execute_approved_refuses_ungranted_states(graph, state):
    remediation = add_remediation(graph, state)
    client = RecordingClient()

    with pytest.raises(ApprovalRequiredError):
        execute_approved(graph, remediation.id, client=client)

    assert client.calls == []


def test_tampering_with_in_memory_object_does_not_bypass_store_gate(graph):
    remediation = add_remediation(graph, ApprovalState.PENDING)
    remediation.data["approval"] = ApprovalState.GRANTED
    client = RecordingClient()

    with pytest.raises(ApprovalRequiredError):
        ApprovalQueue(graph).execute_approved(remediation.id, client=client)

    assert client.calls == []
    assert graph.get_object(remediation.id).data["approval"] == ApprovalState.PENDING


def test_granted_action_executes_exactly_once(graph):
    remediation = add_remediation(graph)
    queue = ApprovalQueue(graph)
    queue.grant(remediation.id, approved_by="ada")
    client = RecordingClient()

    executed = queue.execute_approved(remediation.id, client=client, actor="ada")

    assert executed.data["approval"] == ApprovalState.EXECUTED
    assert executed.data["outcome"] == "github_add_issue_comment ok"
    assert client.calls == [
        (
            "github_add_issue_comment",
            {"owner_repo": "octo/repo", "number": 42, "body": "Approved update."},
        )
    ]
    with pytest.raises(ApprovalRequiredError):
        queue.execute_approved(remediation.id, client=client)
    assert len(client.calls) == 1


def test_executor_failure_marks_failed_and_reraises(graph):
    remediation = add_remediation(graph)
    queue = ApprovalQueue(graph)
    queue.grant(remediation.id, approved_by="ada")

    with pytest.raises(RemediationExecutionError, match="boom"):
        queue.execute_approved(remediation.id, client=RecordingClient(fail=True))

    stored = graph.get_object(remediation.id)
    assert stored.data["approval"] == ApprovalState.FAILED
    assert "boom" in stored.data["outcome"]


def test_unverified_msx_action_refuses_even_when_granted(graph):
    remediation = graph.add_object(
        ObjectType.REMEDIATION,
        {
            "action": "advance_milestone",
            "target_source": SourceKind.MSX,
            "target_uri": "msx:milestone:7",
            "params": {
                "milestone_id": "7",
                "current_status": "Qualify",
                "new_status": "Complete",
            },
            "preview": "Advance MSX milestone 7: Qualify \u2192 Complete",
            "rationale": "test",
            "approval": ApprovalState.PENDING,
            "confidence": 0.4,
        },
    )
    queue = ApprovalQueue(graph)
    queue.grant(remediation.id, approved_by="ada")
    client = RecordingClient()

    with pytest.raises(RemediationExecutionError, match="propose-only"):
        queue.execute_approved(remediation.id, client=client)

    assert client.calls == []
    assert graph.get_object(remediation.id).data["approval"] == ApprovalState.FAILED


def test_dry_run_never_invokes_executor(graph):
    remediation = add_remediation(graph)
    client = RecordingClient()

    assert ApprovalQueue(graph).dry_run(remediation.id) == (
        "Comment on GitHub octo/repo#42: Approved update."
    )
    assert client.calls == []


def test_grants_and_rejections_are_recorded_as_patches(graph):
    granted = add_remediation(graph)
    rejected = add_remediation(graph)
    queue = ApprovalQueue(graph)

    queue.grant(granted.id, approved_by="ada", note="looks good")
    queue.reject(rejected.id, reason="wrong thread", actor="ada")

    patches = graph._test_store.all_patches()
    assert len(patches) == 2
    assert {patch.status for patch in patches} == {"applied"}
    assert graph.get_object(granted.id).data["approval"] == ApprovalState.GRANTED
    assert graph.get_object(rejected.id).data["approval"] == ApprovalState.REJECTED
    assert graph.get_object(rejected.id).data["outcome"] == "wrong thread"


def test_remediation_policy_requires_remediation_approval():
    assert remediation_policy().requires_approval == ["remediation"]
