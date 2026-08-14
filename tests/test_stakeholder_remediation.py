from __future__ import annotations

from datetime import UTC, datetime, timedelta

from activegraph import Graph

from task_graph.ontology.types import ApprovalState, ObjectType, RelationType, TaskState
from task_graph.pipeline.remediation import propose_remediations
from task_graph.store import SqliteGraphStore


def test_opportunity_stakeholder_proposals_round_trip_through_sqlite(tmp_path):
    store = SqliteGraphStore(tmp_path / "graph.db")
    try:
        graph = Graph(graph_store=store)
        task = graph.add_object(
            ObjectType.TASK,
            {
                "title": "Complete business case",
                "summary": "Business case approved and ready for closeout.",
                "state": TaskState.DONE,
            },
        )
        account = graph.add_object(
            ObjectType.ACCOUNT,
            {"name": "Contoso", "account_team_chat_id": "chat-123"},
        )
        opportunity = graph.add_object(
            ObjectType.OPPORTUNITY,
            {
                "name": "Contoso renewal",
                "msx_id": "opp-123",
                "stage": "Propose",
                "close_date": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            },
        )
        contact = graph.add_object(
            ObjectType.PERSON,
            {"display_name": "Alex Customer", "email": "alex@contoso.com"},
        )
        graph.add_relation(task.id, account.id, RelationType.ABOUT_ACCOUNT)
        graph.add_relation(task.id, opportunity.id, RelationType.PART_OF)
        graph.add_relation(task.id, contact.id, RelationType.MENTIONS)

        proposed = propose_remediations(graph, task)

        by_action = {obj.data["action"]: obj for obj in proposed}
        assert set(by_action) == {
            "draft_customer_email",
            "post_account_team_update",
            "advance_opportunity_stage",
        }
        assert all(obj.data["approval"] == ApprovalState.PENDING for obj in proposed)
        assert store.get_object(by_action["draft_customer_email"].id) is not None
        assert {
            rel.source
            for rel in graph.relations(target=task.id, type=RelationType.REMEDIATES)
        } == {obj.id for obj in proposed}
        assert "alex@contoso.com" in by_action["draft_customer_email"].data["preview"]
        assert "Business case approved" in by_action["post_account_team_update"].data["preview"]
        assert by_action["advance_opportunity_stage"].data["preview"] == (
            "Contoso renewal: Propose -> Close"
        )
    finally:
        store.close()


def test_past_due_milestone_proposes_flagging_it(tmp_path):
    store = SqliteGraphStore(tmp_path / "graph.db")
    try:
        graph = Graph(graph_store=store)
        task = graph.add_object(
            ObjectType.TASK,
            {"title": "Review milestone", "state": TaskState.ACTIVE},
        )
        milestone = graph.add_object(
            ObjectType.MILESTONE,
            {
                "name": "Security review",
                "msx_id": "mile-1",
                "status": "On Track",
                "due_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            },
        )
        graph.add_relation(task.id, milestone.id, RelationType.PART_OF)

        proposed = propose_remediations(graph, task)

        assert [obj.data["action"] for obj in proposed] == ["advance_milestone"]
        assert proposed[0].data["params"]["new_status"] == "At Risk"
        assert proposed[0].data["approval"] == ApprovalState.PENDING
        assert store.get_object(proposed[0].id) is not None
    finally:
        store.close()


def test_stakeholder_rules_are_silent_without_crm_entities(tmp_path):
    store = SqliteGraphStore(tmp_path / "graph.db")
    try:
        graph = Graph(graph_store=store)
        task = graph.add_object(
            ObjectType.TASK,
            {
                "title": "No CRM context",
                "summary": "Done but not linked to account, opportunity, milestone, or contact.",
                "state": TaskState.DONE,
            },
        )

        assert propose_remediations(graph, task) == []
    finally:
        store.close()
