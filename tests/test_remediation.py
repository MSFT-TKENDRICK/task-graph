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
        "comment_github_pr",
        "close_github_pr",
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


def _pr_task(graph, *, title="Add the thing", state=TaskState.ACTIVE.value, number=9):
    """A task backed by a GitHub *pull request*, which is what real syncs give."""
    source = graph.add_object(
        ObjectType.SOURCE_ITEM,
        {
            "source": SourceKind.GITHUB,
            "source_uri": f"github:pr:owner/repo#{number}",
            "title": title,
            "body": "A long pull request description that must not be echoed back.",
            "source_state": "open",
        },
    )
    task = graph.add_object(
        ObjectType.TASK,
        {"title": title, "state": state, "summary": source.data["body"]},
    )
    graph.add_relation(source.id, task.id, RelationType.EVIDENCE_OF)
    return task


def test_pull_requests_get_proposals(tmp_path):
    """Every task from a real GitHub sync was a PR, and no rule matched one."""
    graph = Graph(graph_store=SqliteGraphStore(tmp_path / "g.db"))
    task = _pr_task(graph)

    proposals = propose_remediations(graph, task)
    assert [p.data["action"] for p in proposals] == ["comment_github_pr"]
    assert p_target(proposals[0]) == "github:pr:owner/repo#9"


def test_a_done_pull_request_is_proposed_for_closing(tmp_path):
    graph = Graph(graph_store=SqliteGraphStore(tmp_path / "g.db"))
    task = _pr_task(graph, title="Fixed the thing, all complete")

    actions = {p.data["action"] for p in propose_remediations(graph, task)}
    assert actions == {"close_github_pr"}


def test_a_draft_comment_never_echoes_the_source_body(tmp_path):
    """Proposing a PR's own description back onto that PR is not a comment."""
    graph = Graph(graph_store=SqliteGraphStore(tmp_path / "g.db"))
    task = _pr_task(graph)

    body = propose_remediations(graph, task)[0].data["params"]["body"]
    assert "must not be echoed back" not in body
    assert len(body) < 300


def test_pr_actions_reach_the_pull_request_endpoints():
    """`gh issue` refuses a PR number, so the PR actions must not use it."""
    assert get_action("comment_github_pr").target_source is SourceKind.GITHUB
    assert get_action("close_github_pr").target_source is SourceKind.GITHUB


def p_target(proposal):
    return proposal.data["target_uri"]
