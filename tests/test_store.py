"""Conformance tests for :class:`SqliteGraphStore`.

The strongest available oracle is activegraph's own ``InMemoryGraphStore``: for
any sequence of operations both stores must agree. Anything the SQL pushdown
gets subtly wrong shows up here as a divergence.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from activegraph import Graph, InMemoryGraphStore, Object, Patch, Relation

from task_graph.store import SqliteGraphStore, searchable_text


@pytest.fixture
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "graph.db")
    yield s
    s.close()


@pytest.fixture
def both(tmp_path):
    """A SQLite store and an in-memory store to compare it against."""
    sqlite_store = SqliteGraphStore(tmp_path / "cmp.db")
    yield sqlite_store, InMemoryGraphStore()
    sqlite_store.close()


def obj(oid: str, otype: str = "task", **data) -> Object:
    return Object(id=oid, type=otype, data=data or {"title": oid}, version=1, provenance={})


def rel(rid: str, source: str, target: str, rtype: str = "BLOCKS") -> Relation:
    return Relation(id=rid, source=source, target=target, type=rtype, data={}, provenance={})


def sort_ids(items) -> list[str]:
    return sorted(i.id for i in items)


# --------------------------------------------------------------- round trips


def test_object_round_trip(store):
    original = obj("t1", "task", title="Fix billing", state="active", priority=0.75)
    store.put_object(original)
    loaded = store.get_object("t1")
    assert loaded == original


def test_put_object_is_an_upsert(store):
    store.put_object(obj("t1", title="before"))
    store.put_object(
        Object(id="t1", type="task", data={"title": "after"}, version=2, provenance={})
    )
    loaded = store.get_object("t1")
    assert loaded.data["title"] == "after"
    assert loaded.version == 2
    assert len(store.all_objects()) == 1


def test_relation_round_trip(store):
    store.put_object(obj("a"))
    store.put_object(obj("b"))
    original = rel("r1", "a", "b", "DEPENDS_ON")
    store.put_relation(original)
    assert store.get_relation("r1") == original


def test_patch_round_trip(store):
    patch = Patch(
        id="p1",
        target="t1",
        op="update",
        value={"state": "done"},
        expected_version=1,
        proposed_by="dedupe",
        rationale="same work as t2",
        evidence=["e1", "e2"],
        status="proposed",
        rejection_reason=None,
        provenance={},
    )
    store.put_patch(patch)
    assert store.get_patch("p1") == patch
    assert store.all_patches() == [patch]


def test_missing_lookups_return_none(store):
    assert store.get_object("nope") is None
    assert store.get_relation("nope") is None
    assert store.get_patch("nope") is None


def test_remove_is_idempotent(store):
    store.remove_object("ghost")
    store.remove_relation("ghost")
    store.remove_patch("ghost")


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "persist.db"
    first = SqliteGraphStore(path)
    first.put_object(obj("t1", title="survives"))
    first.close()

    second = SqliteGraphStore(path)
    assert second.get_object("t1").data["title"] == "survives"
    second.close()


def test_schema_version_mismatch_is_rejected(tmp_path):
    path = tmp_path / "old.db"
    store = SqliteGraphStore(path)
    store.connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    store.close()

    with pytest.raises(RuntimeError, match="tg rebuild"):
        SqliteGraphStore(path)


# ------------------------------------------------------- ingest idempotency


def test_source_uri_is_unique(store):
    """Ingest idempotency depends on this constraint holding at the DB level."""
    store.put_object(obj("s1", "source_item", source_uri="github:issue:o/r#1", title="one"))
    with pytest.raises(sqlite3.IntegrityError):
        store.put_object(obj("s2", "source_item", source_uri="github:issue:o/r#1", title="dup"))


def test_tasks_without_source_uri_do_not_collide(store):
    store.put_object(obj("t1", "task", title="a"))
    store.put_object(obj("t2", "task", title="b"))
    assert len(store.all_objects()) == 2


# ---------------------------------------------------------- query pushdown


def test_find_objects_matches_reference(both):
    sqlite_store, memory_store = both
    objects = [obj("t1"), obj("t2"), obj("s1", "source_item", source_uri="u1", title="s")]
    for o in objects:
        sqlite_store.put_object(o)
        memory_store.put_object(o)

    assert sort_ids(sqlite_store.find_objects("task")) == sort_ids(
        memory_store.find_objects("task")
    )
    assert sort_ids(sqlite_store.find_objects()) == sort_ids(memory_store.find_objects())
    assert sqlite_store.find_objects("nonexistent") == []


def test_find_objects_in_types_matches_reference(both):
    sqlite_store, memory_store = both
    for o in (obj("t1"), obj("p1", "person", display_name="Ada"), obj("t2")):
        sqlite_store.put_object(o)
        memory_store.put_object(o)

    for types in ([], ["task"], ["task", "person"], ["missing"]):
        assert sort_ids(sqlite_store.find_objects_in_types(types)) == sort_ids(
            memory_store.find_objects_in_types(types)
        )


def test_find_relations_matches_reference(both):
    sqlite_store, memory_store = both
    for o in (obj("a"), obj("b"), obj("c")):
        sqlite_store.put_object(o)
        memory_store.put_object(o)
    relations = [rel("r1", "a", "b", "BLOCKS"), rel("r2", "b", "c", "SAME_AS"), rel("r3", "a", "c")]
    for r in relations:
        sqlite_store.put_relation(r)
        memory_store.put_relation(r)

    cases = [
        {},
        {"source": "a"},
        {"target": "c"},
        {"type": "BLOCKS"},
        {"source": "a", "type": "BLOCKS"},
        {"source": "a", "target": "c", "type": "BLOCKS"},
        {"source": "missing"},
    ]
    for kwargs in cases:
        assert sort_ids(sqlite_store.find_relations(**kwargs)) == sort_ids(
            memory_store.find_relations(**kwargs)
        ), kwargs


# ------------------------------------------------------------- neighborhood


def _build_chain(*stores):
    """a -> b -> c -> d plus an isolated node and a dangling edge."""
    for s in stores:
        for name in ("a", "b", "c", "d", "isolated"):
            s.put_object(obj(name))
        s.put_relation(rel("r1", "a", "b"))
        s.put_relation(rel("r2", "b", "c"))
        s.put_relation(rel("r3", "c", "d"))
        # Endpoint that is not a materialised object; must be skipped, not crash.
        s.put_relation(rel("r_dangling", "d", "ghost"))


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4, 10])
def test_neighborhood_matches_reference(both, depth):
    sqlite_store, memory_store = both
    _build_chain(sqlite_store, memory_store)

    got_objects, got_relations = sqlite_store.neighborhood("a", depth)
    want_objects, want_relations = memory_store.neighborhood("a", depth)

    assert sort_ids(got_objects) == sort_ids(want_objects)
    assert sort_ids(got_relations) == sort_ids(want_relations)


def test_neighborhood_of_unknown_object_is_empty(both):
    sqlite_store, memory_store = both
    _build_chain(sqlite_store, memory_store)
    assert sqlite_store.neighborhood("ghost", 3) == ([], [])
    assert memory_store.neighborhood("ghost", 3) == ([], [])


def test_neighborhood_handles_cycles(both):
    sqlite_store, memory_store = both
    for s in (sqlite_store, memory_store):
        for name in ("a", "b", "c"):
            s.put_object(obj(name))
        s.put_relation(rel("r1", "a", "b"))
        s.put_relation(rel("r2", "b", "c"))
        s.put_relation(rel("r3", "c", "a"))

    got, got_rels = sqlite_store.neighborhood("a", 5)
    want, want_rels = memory_store.neighborhood("a", 5)
    assert sort_ids(got) == sort_ids(want)
    assert sort_ids(got_rels) == sort_ids(want_rels)


def test_neighborhood_wide_frontier_exceeds_sql_variable_limit(both):
    """Frontier chunking must not drop edges on a high-degree hub."""
    sqlite_store, memory_store = both
    for s in (sqlite_store, memory_store):
        s.put_object(obj("hub"))
        for i in range(900):
            s.put_object(obj(f"n{i}"))
            s.put_relation(rel(f"r{i}", "hub", f"n{i}"))

    got, got_rels = sqlite_store.neighborhood("hub", 2)
    want, want_rels = memory_store.neighborhood("hub", 2)
    assert sort_ids(got) == sort_ids(want)
    assert sort_ids(got_rels) == sort_ids(want_rels)


# --------------------------------------------------------------- match_chain


def test_match_chain_matches_reference(both):
    """Inherited from the base class, but it rides on our pushed-down hooks."""
    sqlite_store, memory_store = both
    for s in (sqlite_store, memory_store):
        s.put_object(obj("si1", "source_item", source_uri="u1", title="issue"))
        s.put_object(obj("t1", "task", title="task one"))
        s.put_object(obj("t2", "task", title="task two"))
        s.put_relation(rel("e1", "si1", "t1", "EVIDENCE_OF"))
        s.put_relation(rel("e2", "t1", "t2", "BLOCKS"))

    cases = [
        (["source_item", "task"], [("EVIDENCE_OF", "right")]),
        (["task", "source_item"], [("EVIDENCE_OF", "left")]),
        (["source_item", "task", "task"], [("EVIDENCE_OF", "right"), ("BLOCKS", "right")]),
        ([None, None], [(None, "right")]),
    ]
    for node_types, rels_spec in cases:
        got = sqlite_store.match_chain(node_types, rels_spec)
        want = memory_store.match_chain(node_types, rels_spec)
        norm = lambda ms: sorted(  # noqa: E731
            (tuple(o.id for o in m.objects), tuple(r.id for r in m.relations)) for m in ms
        )
        assert norm(got) == norm(want), (node_types, rels_spec)


# ---------------------------------------------------------------------- FTS


def test_fts_index_is_maintained_on_write(store):
    store.put_object(obj("t1", "task", title="Fix the billing pipeline", summary="urgent"))
    rows = store.connection.execute(
        "SELECT object_id FROM objects_fts WHERE objects_fts MATCH 'billing'"
    ).fetchall()
    assert [r["object_id"] for r in rows] == ["t1"]


def test_fts_index_follows_updates(store):
    store.put_object(obj("t1", "task", title="billing"))
    store.put_object(
        Object(id="t1", type="task", data={"title": "shipping"}, version=2, provenance={})
    )
    assert store.connection.execute(
        "SELECT COUNT(*) n FROM objects_fts WHERE objects_fts MATCH 'billing'"
    ).fetchone()["n"] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) n FROM objects_fts WHERE objects_fts MATCH 'shipping'"
    ).fetchone()["n"] == 1


def test_fts_index_is_cleaned_on_remove(store):
    store.put_object(obj("t1", "task", title="billing"))
    store.remove_object("t1")
    assert store.counts()["objects_fts"] == 0


def test_rebuild_fts_recovers_a_dropped_index(store):
    store.put_object(obj("t1", "task", title="billing"))
    store.put_object(obj("t2", "task", title="shipping"))
    store.connection.execute("DELETE FROM objects_fts")
    assert store.rebuild_fts() == 2
    assert store.counts()["objects_fts"] == 2


def test_searchable_text_flattens_lists():
    text = searchable_text(
        "source_item",
        {"title": "Fix", "body": "details", "labels": ["bug", "p1"], "external_refs": ["AB#123"]},
    )
    assert "Fix" in text and "bug" in text and "AB#123" in text


def test_searchable_text_ignores_unknown_and_empty_fields():
    assert searchable_text("task", {"title": "", "summary": None}) == ""
    assert searchable_text("mystery_type", {"title": "still indexed"}) == "still indexed"


# ------------------------------------------------------------------- bulk io


def test_bulk_writes_commit_together(store):
    with store.bulk_writes():
        for i in range(50):
            store.put_object(obj(f"t{i}"))
    assert store.counts()["objects"] == 50


def test_bulk_writes_roll_back_on_error(store):
    store.put_object(obj("keep"))
    with pytest.raises(ValueError):
        with store.bulk_writes():
            store.put_object(obj("discard"))
            raise ValueError("boom")
    assert store.get_object("discard") is None
    assert store.get_object("keep") is not None


def test_bulk_writes_are_reentrant(store):
    with store.bulk_writes():
        store.put_object(obj("a"))
        with store.bulk_writes():
            store.put_object(obj("b"))
    assert store.counts()["objects"] == 2


def test_clear_empties_every_table(store):
    store.put_object(obj("t1", title="billing"))
    store.put_relation(rel("r1", "t1", "t1"))
    store.clear()
    assert store.counts() == {
        "objects": 0,
        "relations": 0,
        "patches": 0,
        "embeddings": 0,
        "objects_fts": 0,
    }


# ------------------------------------------------- integration with activegraph


def test_graph_writes_through_to_the_store(store):
    """The real integration: activegraph's projector must drive our store."""
    graph = Graph(graph_store=store)
    task = graph.add_object("task", {"title": "Ship the thing", "state": "triage"})
    other = graph.add_object("task", {"title": "Blocked on this", "state": "triage"})
    graph.add_relation(task.id, other.id, "BLOCKS")

    assert store.get_object(task.id) is not None
    assert len(store.find_objects("task")) == 2
    assert len(store.find_relations(source=task.id, type="BLOCKS")) == 1


def test_patch_lifecycle_through_graph(store):
    """Corrections ride on propose/apply/reject; all three must persist."""
    graph = Graph(graph_store=store)
    task = graph.add_object("task", {"title": "wrong title", "state": "triage"})

    patch = graph.propose_patch(
        task.id, "update", {"title": "right title"}, proposed_by="dedupe", rationale="user says so"
    )
    assert store.get_patch(patch.id).status == "proposed"

    graph.apply_patch(patch.id, approved_by="user")
    assert store.get_patch(patch.id).status == "applied"
    assert store.get_object(task.id).data["title"] == "right title"


def test_rejected_patch_is_retained_for_learning(store):
    graph = Graph(graph_store=store)
    task = graph.add_object("task", {"title": "keep me", "state": "triage"})
    patch = graph.propose_patch(task.id, "update", {"title": "nope"}, proposed_by="dedupe")

    graph.reject_patch(patch.id, "not the same work", actor="user")

    stored = store.get_patch(patch.id)
    assert stored.status == "rejected"
    assert stored.rejection_reason == "not the same work"
    assert store.get_object(task.id).data["title"] == "keep me"


def test_projection_is_rebuildable_from_the_event_log(tmp_path):
    """The core durability claim: the graph db is disposable."""
    store = SqliteGraphStore(tmp_path / "g1.db")
    graph = Graph(graph_store=store)
    a = graph.add_object("task", {"title": "one"})
    b = graph.add_object("task", {"title": "two"})
    graph.add_relation(a.id, b.id, "BLOCKS")
    events = list(graph.events)
    store.close()

    replayed_store = SqliteGraphStore(tmp_path / "g2.db")
    replayed = Graph(graph_store=replayed_store)
    with replayed_store.bulk_writes():
        for event in events:
            replayed.emit(event)

    assert sort_ids(replayed_store.all_objects()) == sorted([a.id, b.id])
    assert len(replayed_store.all_relations()) == 1
    replayed_store.close()


def test_object_data_survives_non_json_native_values(store):
    """Connectors hand us datetimes; they must not blow up the projection."""
    from datetime import datetime

    store.put_object(obj("t1", "task", title="x", due_at=datetime(2026, 1, 2, 3, 4, 5)))
    loaded = store.get_object("t1")
    assert "2026-01-02" in json.dumps(loaded.data)
