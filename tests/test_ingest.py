"""Tests for the ingest pipeline.

The invariant under test throughout: syncing the same source twice must never
duplicate a node.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from activegraph import Graph

from task_graph.connectors.base import SourceItem
from task_graph.embeddings import HashingEmbedder
from task_graph.ontology.types import RelationType, SourceKind, TaskState
from task_graph.pipeline.ingest import (
    Ingestor,
    item_fingerprint,
    map_state,
)
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
    return Ingestor(graph, store, embedder=HashingEmbedder())


def item(uri: str = "github:issue:o/r#1", **overrides) -> SourceItem:
    payload = {
        "source": SourceKind.GITHUB,
        "source_uri": uri,
        "title": "Fix the billing pipeline",
        "body": "The nightly job fails on invoices over 1000.",
        "source_state": "open",
        "owner": "tykendrick",
        "url": "https://github.com/o/r/issues/1",
    }
    payload.update(overrides)
    return SourceItem(**payload)


# ------------------------------------------------------------- state mapping


@pytest.mark.parametrize(
    ("source", "raw", "expected"),
    [
        (SourceKind.GITHUB, "open", TaskState.ACTIVE),
        (SourceKind.GITHUB, "closed", TaskState.DONE),
        (SourceKind.GITHUB, "MERGED", TaskState.DONE),
        (SourceKind.ADO, "New", TaskState.TRIAGE),
        (SourceKind.ADO, "Active", TaskState.ACTIVE),
        (SourceKind.ADO, "  Resolved  ", TaskState.DONE),
        (SourceKind.ADO, "Removed", TaskState.DROPPED),
        (SourceKind.ADO, "Blocked", TaskState.BLOCKED),
        (SourceKind.MAIL, "flagged", TaskState.ACTIVE),
    ],
)
def test_map_state(source, raw, expected):
    assert map_state(source, raw) is expected


def test_unknown_states_fall_back_to_triage():
    """An unrecognised state means "a human should look", not a guess."""
    assert map_state(SourceKind.ADO, "Sprint Backlog Maybe") is TaskState.TRIAGE
    assert map_state(SourceKind.GITHUB, None) is TaskState.TRIAGE
    assert map_state("not-a-source", "open") is TaskState.TRIAGE
    assert map_state("", "open") is TaskState.TRIAGE


# ------------------------------------------------------------- fingerprints


def test_fingerprint_is_stable_for_equal_content():
    assert item_fingerprint(item()) == item_fingerprint(item())


def test_fingerprint_changes_with_meaningful_fields():
    base = item_fingerprint(item())
    assert item_fingerprint(item(title="Different")) != base
    assert item_fingerprint(item(source_state="closed")) != base
    assert item_fingerprint(item(labels=["p1"])) != base


def test_fingerprint_ignores_noise():
    """Sources bump updated_at for reactions and other irrelevancies."""
    base = item_fingerprint(item())
    noisy = item(updated_at=datetime(2026, 5, 1, tzinfo=UTC), raw={"reactions": 12})
    assert item_fingerprint(noisy) == base


# --------------------------------------------------------------- first sync


def test_ingest_creates_source_item_and_task(ingestor, store):
    report = ingestor.ingest([item()])

    assert len(report.created) == 1
    assert len(report.tasks_created) == 1
    assert len(store.find_objects("source_item")) == 1
    assert len(store.find_objects("task")) == 1


def test_ingest_links_evidence_of(ingestor, store):
    ingestor.ingest([item()])
    source = store.get_object_by_source_uri("github:issue:o/r#1")
    relations = store.find_relations(source=source.id, type=RelationType.EVIDENCE_OF.value)
    assert len(relations) == 1
    assert store.get_object(relations[0].target).type == "task"


def test_task_inherits_mapped_state_and_owner(ingestor, store):
    ingestor.ingest([item(source_state="open", owner="tykendrick")])
    task = store.find_objects("task")[0]
    assert task.data["state"] == TaskState.ACTIVE.value
    assert task.data["owner"] == "tykendrick"
    assert task.data["source_uris"] == ["github:issue:o/r#1"]


def test_ingest_handles_several_sources(ingestor, store):
    report = ingestor.ingest(
        [
            item("github:issue:o/r#1"),
            item("ado:workitem:555", source=SourceKind.ADO, source_state="Active"),
            item("mail:<abc@corp>", source=SourceKind.MAIL, source_state="flagged"),
        ]
    )
    assert len(report.created) == 3
    assert len(store.find_objects("task")) == 3


# --------------------------------------------------------------- idempotency


def test_resyncing_identical_items_creates_nothing(ingestor, store):
    ingestor.ingest([item()])
    report = ingestor.ingest([item()])

    assert report.created == []
    assert report.updated == []
    assert len(report.unchanged) == 1
    assert len(store.find_objects("source_item")) == 1
    assert len(store.find_objects("task")) == 1


def test_resyncing_many_times_is_stable(ingestor, store):
    for _ in range(5):
        ingestor.ingest([item(), item("ado:workitem:9", source=SourceKind.ADO)])
    assert len(store.find_objects("source_item")) == 2
    assert len(store.find_objects("task")) == 2
    assert len(store.find_relations(type=RelationType.EVIDENCE_OF.value)) == 2


def test_changed_item_updates_in_place(ingestor, store):
    ingestor.ingest([item()])
    report = ingestor.ingest([item(title="Fix the billing pipeline (urgent)")])

    assert len(report.updated) == 1
    assert report.created == []
    assert len(store.find_objects("source_item")) == 1
    source = store.get_object_by_source_uri("github:issue:o/r#1")
    assert source.data["title"] == "Fix the billing pipeline (urgent)"


def test_changed_item_propagates_to_its_task(ingestor, store):
    ingestor.ingest([item()])
    ingestor.ingest([item(title="Renamed", source_state="closed")])

    task = store.find_objects("task")[0]
    assert task.data["title"] == "Renamed"
    assert task.data["state"] == TaskState.DONE.value


# ------------------------------------------------- multi-source task closure


def test_closure_needs_every_backing_source(ingestor, graph, store):
    """Closing an ADO item must not close work still open in another system."""
    ingestor.ingest(
        [
            item("ado:workitem:1", source=SourceKind.ADO, source_state="Active"),
            item("github:issue:o/r#1", source_state="open"),
        ]
    )
    tasks = store.find_objects("task")
    survivor, absorbed = tasks[0], tasks[1]

    # Simulate what dedupe will do: one task backed by both sources.
    for rel in store.find_relations(target=absorbed.id, type=RelationType.EVIDENCE_OF.value):
        graph.add_relation(rel.source, survivor.id, RelationType.EVIDENCE_OF.value)
    graph.patch_object(
        survivor.id, {"source_uris": ["ado:workitem:1", "github:issue:o/r#1"]}
    )

    ingestor.ingest([item("ado:workitem:1", source=SourceKind.ADO, source_state="Closed")])
    assert store.get_object(survivor.id).data["state"] != TaskState.DONE.value

    ingestor.ingest([item("github:issue:o/r#1", source_state="closed")])
    assert store.get_object(survivor.id).data["state"] == TaskState.DONE.value


# ------------------------------------------------------------ error handling


def test_one_bad_item_does_not_abort_the_sync(ingestor, store):
    class Exploding(SourceItem):
        def to_props(self):
            raise RuntimeError("connector bug")

    bad = Exploding(source=SourceKind.GITHUB, source_uri="github:issue:o/r#99", title="boom")
    report = ingestor.ingest([item(), bad, item("ado:workitem:2", source=SourceKind.ADO)])

    assert len(report.created) == 2
    assert len(report.errors) == 1
    assert "github:issue:o/r#99" in report.errors[0]


def test_report_summary_is_readable(ingestor):
    ingestor.ingest([item()])
    summary = ingestor.ingest([item()]).summary()
    assert "unchanged" in summary


# --------------------------------------------------------------- embeddings


def test_ingest_embeds_new_objects(ingestor, store):
    report = ingestor.ingest([item()])
    assert report.embedded > 0
    assert ingestor.search.vectors.count() == report.embedded


def test_resync_does_not_re_embed_unchanged_objects(ingestor):
    ingestor.ingest([item()])
    assert ingestor.ingest([item()]).embedded == 0


def test_changed_text_is_re_embedded(ingestor):
    ingestor.ingest([item()])
    assert ingestor.ingest([item(title="Completely different heading")]).embedded > 0


def test_ingest_without_an_embedder_still_works(graph, store):
    plain = Ingestor(graph, store, embedder=None)
    report = plain.ingest([item()])
    assert len(report.created) == 1
    assert report.embedded == 0


def test_embedded_objects_are_semantically_searchable(ingestor):
    ingestor.ingest(
        [
            item("github:issue:o/r#1", title="Fix the billing pipeline"),
            item("ado:workitem:7", source=SourceKind.ADO, title="Order more coffee"),
        ]
    )
    embedder = HashingEmbedder()
    hits = ingestor.search.hybrid(
        "billing pipeline", embedder.embed_one("billing pipeline"), k=5, object_type="task"
    )
    assert hits
    top = ingestor.store.get_object(hits[0].object_id)
    assert "billing" in top.data["title"].lower()


# ------------------------------------------------------------- graph helpers


def test_sources_and_tasks_resolve_both_ways(ingestor, store):
    ingestor.ingest([item()])
    source = store.get_object_by_source_uri("github:issue:o/r#1")
    task = store.find_objects("task")[0]

    assert [t.id for t in ingestor.tasks_for_source(source.id)] == [task.id]
    assert [s.id for s in ingestor.sources_for_task(task.id)] == [source.id]


def test_source_uris_helper_lists_everything_ingested(ingestor, store):
    ingestor.ingest([item("github:issue:o/r#1"), item("ado:workitem:3", source=SourceKind.ADO)])
    assert store.source_uris() == {"github:issue:o/r#1", "ado:workitem:3"}
