"""Tests for CRM/account-team projection."""

from __future__ import annotations

import pytest
from activegraph import Graph, SQLiteEventStore

from task_graph.connectors.base import SourceItem
from task_graph.ontology.types import ObjectType, RelationType, SourceKind
from task_graph.pipeline.crm import CrmProjector, normalise_account_name, person_candidate
from task_graph.pipeline.ingest import Ingestor
from task_graph.store import SqliteGraphStore


@pytest.fixture
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "graph.db")
    yield s
    s.close()


@pytest.fixture
def graph(store):
    return Graph(graph_store=store)


@pytest.fixture
def ingestor(graph, store):
    return Ingestor(graph, store, embedder=None)


@pytest.fixture
def projector(graph, store):
    return CrmProjector(graph, store)


def msx_opportunity(uri: str = "msx:opportunity:opp-1", **raw_overrides) -> SourceItem:
    raw = {
        "opportunityId": "opp-1",
        "opportunityName": "Contoso renewal",
        "salesStage": "Propose",
        "estimatedRevenue": 1250000,
        "estimatedCloseDate": "2026-09-30T00:00:00Z",
        "accountName": "Contoso, Inc.",
        "msxAccountId": "acct-1",
        "tpid": "42",
        "owner": {"displayName": "Taylor Lee", "email": "Taylor.Lee@Microsoft.com"},
        "dealTeam": [
            {"displayName": "Adele Vance", "upn": "adele.vance@microsoft.com"},
            {"email": "priya@example.com"},
        ],
    }
    raw.update(raw_overrides)
    return SourceItem(
        source=SourceKind.MSX,
        source_uri=uri,
        title=str(raw.get("opportunityName") or raw.get("name") or "Contoso renewal"),
        source_state=str(raw.get("salesStage") or "Propose"),
        owner="Taylor.Lee@Microsoft.com",
        assignees=["Adele Vance", "priya@example.com"],
        raw=raw,
    )


def msx_milestone(**raw_overrides) -> SourceItem:
    raw = {
        "milestoneId": "ms-1",
        "milestoneName": "Business decision",
        "status": "In Progress",
        "dueDate": "2026-08-20T12:00:00Z",
        "opportunityId": "opp-1",
        "opportunityName": "Contoso renewal",
        "accountName": "Contoso Inc",
        "owner": "Taylor Lee",
        "dealTeam": ["Adele Vance"],
    }
    raw.update(raw_overrides)
    return SourceItem(
        source=SourceKind.MSX,
        source_uri="msx:milestone:ms-1",
        title=str(raw.get("milestoneName") or raw.get("name") or "Business decision"),
        source_state=str(raw.get("status") or "In Progress"),
        owner="Taylor Lee",
        raw=raw,
    )


def mail_item(uri: str = "mail:<1>", **raw_overrides) -> SourceItem:
    raw = {
        "subject": "Follow up",
        "from": {"emailAddress": {"name": "Taylor Lee", "address": "taylor.lee@microsoft.com"}},
    }
    raw.update(raw_overrides)
    return SourceItem(
        source=SourceKind.MAIL,
        source_uri=uri,
        title="Follow up",
        source_state="flagged",
        owner="taylor.lee@microsoft.com",
        raw=raw,
    )


def teams_item(uri: str = "teams:message:chat:1", **raw_overrides) -> SourceItem:
    raw = {
        "from": {
            "user": {
                "displayName": "Taylor Lee",
                "userPrincipalName": "TAYLOR.LEE@microsoft.com",
            }
        },
        "mentions": [{"displayName": "Adele Vance", "email": "adele.vance@microsoft.com"}],
    }
    raw.update(raw_overrides)
    return SourceItem(
        source=SourceKind.TEAMS,
        source_uri=uri,
        title="Teams: please review",
        source_state="{}",
        owner="Taylor Lee",
        raw=raw,
    )


def task_for_source(store, uri: str):
    source = store.get_object_by_source_uri(uri)
    rel = store.find_relations(source=source.id, type=RelationType.EVIDENCE_OF.value)[0]
    return store.get_object(rel.target)


def test_projects_accounts_opportunities_milestones_and_people(ingestor, projector, store):
    ingestor.ingest([msx_opportunity(), msx_milestone()])

    report = projector.run()

    assert report.accounts_created == 1
    assert report.opportunities_created == 1
    assert report.milestones_created == 1
    assert report.people_created == 3
    account = store.find_objects(ObjectType.ACCOUNT.value)[0]
    opportunity = store.find_objects(ObjectType.OPPORTUNITY.value)[0]
    milestone = store.find_objects(ObjectType.MILESTONE.value)[0]
    people = store.find_objects(ObjectType.PERSON.value)
    assert account.data["name"] == "Contoso, Inc."
    assert account.data["msx_account_id"] == "acct-1"
    assert opportunity.data["estimated_value"] == 1250000.0
    assert opportunity.data["close_date"] == "2026-09-30T00:00:00Z"
    assert milestone.data["due_at"] == "2026-08-20T12:00:00Z"
    assert {p.data["email"] for p in people} >= {"taylor.lee@microsoft.com", "priya@example.com"}


def test_relations_have_expected_direction(ingestor, projector, store):
    ingestor.ingest([msx_opportunity(), msx_milestone()])
    projector.run()
    task = task_for_source(store, "msx:opportunity:opp-1")
    account = store.find_objects(ObjectType.ACCOUNT.value)[0]
    opportunity = store.find_objects(ObjectType.OPPORTUNITY.value)[0]
    milestone = store.find_objects(ObjectType.MILESTONE.value)[0]
    owner = [p for p in store.find_objects(ObjectType.PERSON.value) if p.data["email"]][0]

    assert store.find_relations(task.id, account.id, RelationType.ABOUT_ACCOUNT.value)
    assert store.find_relations(task.id, opportunity.id, RelationType.PART_OF.value)
    assert store.find_relations(opportunity.id, account.id, RelationType.ABOUT_ACCOUNT.value)
    assert store.find_relations(milestone.id, opportunity.id, RelationType.PART_OF.value)
    assert store.find_relations(task.id, owner.id, RelationType.OWNED_BY.value)
    assert store.find_relations(source=task.id, type=RelationType.MENTIONS.value)


def test_running_twice_is_idempotent(ingestor, projector, store):
    ingestor.ingest([msx_opportunity(), msx_milestone(), mail_item()])
    projector.run()
    counts = store.counts()

    second = projector.run()

    assert second.entities_created == 0
    assert second.relations_created == 0
    assert second.objects_updated == 0
    assert store.counts() == counts


def test_person_resolution_collapses_display_smtp_and_upn(ingestor, projector, store):
    ingestor.ingest(
        [
            msx_opportunity(owner="Taylor Lee", dealTeam=[]),
            mail_item("mail:<2>"),
            teams_item("teams:message:chat:2"),
        ]
    )

    projector.run()

    matches = [
        p
        for p in store.find_objects(ObjectType.PERSON.value)
        if "taylor.lee@microsoft.com" in p.data.get("aliases", [])
        or p.data.get("email") == "taylor.lee@microsoft.com"
    ]
    assert len(matches) == 1
    assert {"Taylor Lee", "taylor.lee", "taylor.lee@microsoft.com"} <= set(
        matches[0].data["aliases"]
    )


def test_similar_person_names_do_not_collapse(ingestor, projector, store):
    ingestor.ingest(
        [
            mail_item(
                "mail:<adele>",
                **{"from": {"emailAddress": {"name": "Adele", "address": "adele@example.com"}}},
            ),
            teams_item(
                "teams:message:chat:adele-vance",
                **{"from": {"user": {"displayName": "Adele Vance"}}},
                mentions=[],
            ),
        ]
    )

    projector.run()

    names = {p.data["display_name"] for p in store.find_objects(ObjectType.PERSON.value)}
    assert {"Adele", "Adele Vance"} <= names


def test_account_normalisation_collapses_suffix_variants_not_distinct_names(
    ingestor, projector, store
):
    ingestor.ingest(
        [
            msx_opportunity(
                "msx:opportunity:opp-a",
                opportunityId="opp-a",
                accountName="Contoso, Inc.",
            ),
            msx_opportunity(
                "msx:opportunity:opp-b",
                opportunityId="opp-b",
                accountName="Contoso Inc",
                msxAccountId=None,
                tpid=None,
            ),
            msx_opportunity(
                "msx:opportunity:opp-c",
                opportunityId="opp-c",
                accountName="Contoso Energy",
                msxAccountId=None,
                tpid=None,
            ),
        ]
    )

    projector.run()

    assert normalise_account_name("Contoso, Inc.") == normalise_account_name("Contoso Inc")
    accounts = store.find_objects(ObjectType.ACCOUNT.value)
    assert len(accounts) == 2
    assert {a.data["normalized_name"] for a in accounts} == {"contoso", "contoso energy"}


def test_source_without_crm_content_is_ignored(ingestor, projector, store):
    ingestor.ingest(
        [SourceItem(source=SourceKind.GITHUB, source_uri="github:issue:o/r#1", title="No CRM")]
    )

    report = projector.run()

    assert report.entities_created == 0
    assert store.find_objects(ObjectType.ACCOUNT.value) == []
    assert store.find_objects(ObjectType.PERSON.value) == []


def test_partial_msx_payload_degrades_gracefully(ingestor, projector, store):
    ingestor.ingest(
        [
            msx_opportunity(
                estimatedRevenue=None,
                estimatedCloseDate=None,
                dealTeam=None,
                owner=None,
            )
        ]
    )

    report = projector.run()

    assert report.errors == []
    opportunity = store.find_objects(ObjectType.OPPORTUNITY.value)[0]
    assert "estimated_value" not in opportunity.data
    assert "close_date" not in opportunity.data


def test_projection_is_rebuildable_from_the_event_log(tmp_path):
    store = SqliteGraphStore(tmp_path / "graph.db")
    events = SQLiteEventStore(str(tmp_path / "events.db"), run_id="run-crm")
    graph = Graph(graph_store=store, run_id="run-crm")
    graph.attach_store(events)
    ingestor = Ingestor(graph, store, embedder=None)
    projector = CrmProjector(graph, store)
    ingestor.ingest([msx_opportunity(), msx_milestone(), mail_item()])
    projector.run()
    before = store.counts()
    event_list = list(events.iter_events())
    store.close()
    events.close()

    rebuilt = SqliteGraphStore(tmp_path / "rebuilt.db")
    replay = Graph(graph_store=rebuilt, run_id="run-crm")
    with rebuilt.bulk_writes():
        for event in event_list:
            replay.emit(event)

    assert rebuilt.counts()["objects"] == before["objects"]
    assert rebuilt.counts()["relations"] == before["relations"]
    assert len(rebuilt.find_objects(ObjectType.ACCOUNT.value)) == 1
    assert len(rebuilt.find_relations(type=RelationType.ABOUT_ACCOUNT.value)) >= 2
    rebuilt.close()


def test_person_candidate_keeps_email_local_part_alias():
    candidate = person_candidate("Taylor.Lee@Microsoft.com")

    assert candidate is not None
    assert candidate.email == "taylor.lee@microsoft.com"
    assert candidate.display_name == "Taylor Lee"
