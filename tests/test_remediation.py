from __future__ import annotations

import pytest
from activegraph import Graph

from task_graph.ontology.types import ApprovalState, ObjectType, RelationType, SourceKind, TaskState
from task_graph.pipeline.remediation import all_actions, get_action, propose_remediations
from task_graph.store import SqliteGraphStore


def test_all_actions_are_registered():
    assert {action.name for action in all_actions()} == {
        "update_ado_state",
        "comment_github",
        "close_github_issue",
        "draft_mail_reply",
        "draft_customer_email",
        "advance_opportunity_stage",
        "advance_milestone",
        "post_teams_update",
        "post_account_team_update",
    }
    assert get_action("advance_milestone").verified is False
    assert get_action("advance_opportunity_stage").verified is False


@pytest.mark.parametrize(
    ("action_name", "params", "preview"),
    [
        (
            "update_ado_state",
            {"work_item_id": 12345, "current_state": "Active", "new_state": "Resolved"},
            "Set ADO #12345 state: Active \u2192 Resolved",
        ),
        (
            "comment_github",
            {"owner_repo": "octo/repo", "number": 42, "body": "Please review."},
            "Comment on GitHub octo/repo#42: Please review.",
        ),
        (
            "close_github_issue",
            {"owner_repo": "octo/repo", "number": 42},
            "Close GitHub issue octo/repo#42",
        ),
        (
            "draft_mail_reply",
            {
                "message_id": "m1",
                "to": "ada@example.com",
                "subject": "Re: Launch",
                "body": "Thanks.",
            },
            "Draft mail reply to ada@example.com: Re: Launch",
        ),
        (
            "draft_customer_email",
            {
                "recipients": ["alex@contoso.com", "lee@contoso.com"],
                "subject": "Next steps",
                "body": "Hello,\nPlease confirm next steps.",
                "related_id": "opp-1",
            },
            (
                "Draft customer email to alex@contoso.com, lee@contoso.com: Next steps\n\n"
                "Hello,\nPlease confirm next steps."
            ),
        ),
        (
            "advance_opportunity_stage",
            {
                "opportunity_id": "opp-1",
                "opportunity_name": "Contoso renewal",
                "current_stage": "Propose",
                "new_stage": "Close",
            },
            "Contoso renewal: Propose -> Close",
        ),
        (
            "advance_milestone",
            {"milestone_id": "msx-7", "current_status": "Qualify", "new_status": "Complete"},
            "Advance MSX milestone msx-7: Qualify \u2192 Complete",
        ),
        (
            "post_teams_update",
            {"channel": "General", "message": "Ship update"},
            "Post Teams update to General: Ship update",
        ),
        (
            "post_account_team_update",
            {"destination": "chat-1", "message": "Stakeholder update"},
            "Post account team update to chat-1:\n\nStakeholder update",
        ),
    ],
)
def test_preview_rendering_is_exact(action_name, params, preview):
    assert get_action(action_name).preview(None, params) == preview


def test_propose_remediations_creates_pending_objects_and_links_them(tmp_path):
    store = SqliteGraphStore(tmp_path / "graph.db")
    try:
        graph = Graph(graph_store=store)
        task = graph.add_object(
            ObjectType.TASK,
            {
                "title": "Fix checkout",
                "summary": "The work is resolved and ready to close.",
                "state": TaskState.ACTIVE,
                "source_uris": ["ado:workitem:12345", "github:issue:octo/repo#42"],
            },
        )
        ado = graph.add_object(
            ObjectType.SOURCE_ITEM,
            {
                "source": SourceKind.ADO,
                "source_uri": "ado:workitem:12345",
                "title": "ADO checkout",
                "source_state": "Active",
            },
        )
        gh = graph.add_object(
            ObjectType.SOURCE_ITEM,
            {
                "source": SourceKind.GITHUB,
                "source_uri": "github:issue:octo/repo#42",
                "title": "GH checkout",
                "source_state": "open",
            },
        )
        graph.add_relation(ado.id, task.id, RelationType.EVIDENCE_OF)
        graph.add_relation(gh.id, task.id, RelationType.EVIDENCE_OF)

        proposed = propose_remediations(graph, task)

        assert {obj.data["action"] for obj in proposed} == {
            "update_ado_state",
            "close_github_issue",
        }
        assert all(obj.data["approval"] == ApprovalState.PENDING for obj in proposed)
        assert {
            rel.source
            for rel in graph.relations(target=task.id, type=RelationType.REMEDIATES)
        } == {obj.id for obj in proposed}
        assert any(
            obj.data["preview"] == "Set ADO #12345 state: Active \u2192 Resolved"
            for obj in proposed
        )
    finally:
        store.close()
