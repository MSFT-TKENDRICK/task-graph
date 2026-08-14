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


def add_stakeholder_remediation(
    graph: Graph, action: str, approval: ApprovalState = ApprovalState.PENDING
):
    data = {
        "draft_customer_email": {
            "target_source": SourceKind.MAIL,
            "target_uri": "mail:draft:opp-1",
            "params": {
                "recipients": ["alex@contoso.com"],
                "subject": "Next steps",
                "body": "Hello Alex.",
                "related_id": "opp-1",
            },
            "preview": "Draft customer email to alex@contoso.com: Next steps\n\nHello Alex.",
        },
        "post_account_team_update": {
            "target_source": SourceKind.TEAMS,
            "target_uri": "teams:chat-1",
            "params": {"destination": "chat-1", "message": "Update for the account team."},
            "preview": "Post account team update to chat-1:\n\nUpdate for the account team.",
        },
        "advance_opportunity_stage": {
            "target_source": SourceKind.MSX,
            "target_uri": "msx:opportunity:opp-1",
            "params": {
                "opportunity_id": "opp-1",
                "opportunity_name": "Contoso renewal",
                "current_stage": "Propose",
                "new_stage": "Close",
            },
            "preview": "Contoso renewal: Propose -> Close",
        },
        "advance_milestone": {
            "target_source": SourceKind.MSX,
            "target_uri": "msx:milestone:7",
            "params": {
                "milestone_id": "7",
                "current_status": "Qualify",
                "new_status": "Complete",
            },
            "preview": "Advance MSX milestone 7: Qualify \u2192 Complete",
        },
    }[action]
    return graph.add_object(
        ObjectType.REMEDIATION,
        {
            "action": action,
            "target_source": data["target_source"],
            "target_uri": data["target_uri"],
            "params": data["params"],
            "preview": data["preview"],
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


@pytest.mark.parametrize(
    "action",
    ["draft_customer_email", "post_account_team_update", "advance_opportunity_stage"],
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
def test_new_actions_refuse_ungranted_states(graph, action, state):
    remediation = add_stakeholder_remediation(graph, action, state)
    client = RecordingClient()

    with pytest.raises(ApprovalRequiredError):
        ApprovalQueue(graph).execute_approved(remediation.id, client=client)

    assert client.calls == []


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


def test_granted_account_team_update_executes_exactly_once(graph):
    remediation = add_stakeholder_remediation(graph, "post_account_team_update")
    queue = ApprovalQueue(graph)
    queue.grant(remediation.id, approved_by="ada")
    client = RecordingClient()

    executed = queue.execute_approved(remediation.id, client=client, actor="ada")

    assert executed.data["approval"] == ApprovalState.EXECUTED
    assert executed.data["outcome"] == "teams_post_message ok"
    assert client.calls == [
        (
            "teams_post_message",
            {"destination": "chat-1", "message": "Update for the account team."},
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


def test_account_team_update_failure_marks_failed_and_clears_grant(graph):
    remediation = add_stakeholder_remediation(graph, "post_account_team_update")
    queue = ApprovalQueue(graph)
    queue.grant(remediation.id, approved_by="ada")

    with pytest.raises(RemediationExecutionError, match="boom"):
        queue.execute_approved(remediation.id, client=RecordingClient(fail=True))

    stored = graph.get_object(remediation.id)
    assert stored.data["approval"] == ApprovalState.FAILED
    assert "boom" in stored.data["outcome"]
    assert stored.data["approval"] != ApprovalState.GRANTED


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


@pytest.mark.parametrize("action", ["advance_opportunity_stage", "advance_milestone"])
def test_unverified_stakeholder_msx_actions_refuse_even_when_granted(graph, action):
    remediation = add_stakeholder_remediation(graph, action)
    queue = ApprovalQueue(graph)
    queue.grant(remediation.id, approved_by="ada")
    client = RecordingClient()

    with pytest.raises(RemediationExecutionError, match="write semantics are verified"):
        queue.execute_approved(remediation.id, client=client)

    assert client.calls == []
    stored = graph.get_object(remediation.id)
    assert stored.data["approval"] == ApprovalState.FAILED
    assert "propose-only" in stored.data["outcome"]


def test_dry_run_never_invokes_executor(graph):
    remediation = add_remediation(graph)
    client = RecordingClient()

    assert ApprovalQueue(graph).dry_run(remediation.id) == (
        "Comment on GitHub octo/repo#42: Approved update."
    )
    assert client.calls == []


def test_new_action_dry_run_never_invokes_executor(graph):
    remediation = add_stakeholder_remediation(graph, "post_account_team_update")
    client = RecordingClient()

    assert ApprovalQueue(graph).dry_run(remediation.id) == (
        "Post account team update to chat-1:\n\nUpdate for the account team."
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
