"""Tests for vector storage and hybrid retrieval."""

from __future__ import annotations

import numpy as np
import pytest
from activegraph import Object

from task_graph.embeddings import HashingEmbedder
from task_graph.store import (
    RRF_K,
    SearchIndex,
    SqliteGraphStore,
    SqliteVectorIndex,
    escape_fts_query,
    pack_vector,
    text_fingerprint,
    unpack_vector,
)


@pytest.fixture
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "graph.db")
    yield s
    s.close()


@pytest.fixture
def embedder():
    return HashingEmbedder()


def put_task(store, oid: str, title: str, **extra) -> Object:
    obj = Object(
        id=oid, type="task", data={"title": title, **extra}, version=1, provenance={}
    )
    store.put_object(obj)
    return obj


# ------------------------------------------------------------------- codec


def test_vector_round_trip():
    original = [0.5, -0.25, 0.125, 1.0]
    restored = unpack_vector(pack_vector(original))
    assert np.allclose(restored, original)
    assert restored.dtype == np.dtype("<f4")


def test_packing_is_byte_stable():
    """The BLOB format is a persisted contract; it must not drift."""
    assert pack_vector([1.0, 0.0]) == pack_vector([1.0, 0.0])
    assert len(pack_vector([1.0, 2.0, 3.0])) == 12


def test_text_fingerprint_is_stable_and_discriminating():
    assert text_fingerprint("hello") == text_fingerprint("hello")
    assert text_fingerprint("hello") != text_fingerprint("hello ")


# ------------------------------------------------------------ vector index


def test_search_ranks_by_cosine(store, embedder):
    index = SqliteVectorIndex(store.connection)
    texts = {
        "t1": "Update the billing API documentation",
        "t2": "Revise billing API docs",
        "t3": "Order more coffee for the kitchen",
    }
    for oid, text in texts.items():
        put_task(store, oid, text)
        index.upsert(oid, embedder.embed_one(text), embedder.name, text_fingerprint(text))

    hits = index.search(embedder.embed_one("billing API documentation update"), k=3)
    assert [h.object_id for h in hits][:2] == ["t1", "t2"]
    assert hits[0].score > hits[-1].score


def test_search_on_empty_index_returns_nothing(store, embedder):
    index = SqliteVectorIndex(store.connection)
    assert index.search(embedder.embed_one("anything")) == []


def test_search_respects_type_filter(store, embedder):
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "billing work")
    store.put_object(
        Object(id="s1", type="source_item", data={"title": "billing work", "source_uri": "u1"},
               version=1, provenance={})
    )
    for oid in ("t1", "s1"):
        index.upsert(oid, embedder.embed_one("billing work"), embedder.name, "h")

    query = embedder.embed_one("billing")
    assert [h.object_id for h in index.search(query, object_type="task")] == ["t1"]
    assert [h.object_id for h in index.search(query, object_type="source_item")] == ["s1"]


def test_upsert_replaces_previous_vector(store, embedder):
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "first")
    index.upsert("t1", embedder.embed_one("first"), embedder.name, "h1")
    index.upsert("t1", embedder.embed_one("second"), embedder.name, "h2")
    assert index.count() == 1


def test_removing_an_object_drops_its_vector(store, embedder):
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "billing")
    index.upsert("t1", embedder.embed_one("billing"), embedder.name, "h")
    store.remove_object("t1")
    index.invalidate()
    assert index.count() == 0


def test_cache_is_invalidated_by_writes(store, embedder):
    """A stale cached matrix would silently hide newly ingested work."""
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "billing")
    index.upsert("t1", embedder.embed_one("billing"), embedder.name, "h")
    assert len(index.search(embedder.embed_one("billing"), k=10)) == 1

    put_task(store, "t2", "shipping")
    index.upsert("t2", embedder.embed_one("shipping"), embedder.name, "h")
    assert len(index.search(embedder.embed_one("billing"), k=10)) == 2


def test_stale_objects_detects_what_needs_embedding(store, embedder):
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "billing")
    index.upsert("t1", embedder.embed_one("billing"), "hashing", "hash-a")

    assert index.stale_objects({"t1": "hash-a"}, "hashing") == []
    assert index.stale_objects({"t1": "hash-b"}, "hashing") == ["t1"]
    assert index.stale_objects({"t1": "hash-a"}, "other-model") == ["t1"]
    assert index.stale_objects({"t2": "hash-z"}, "hashing") == ["t2"]
    assert index.stale_objects({}, "hashing") == []


def test_mismatched_query_dimension_is_ignored(store, embedder):
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "billing")
    index.upsert("t1", embedder.embed_one("billing"), embedder.name, "h")
    assert index.search([0.1, 0.2, 0.3]) == []


def test_mixed_dimensions_do_not_crash_the_index(store):
    """Switching embedding model mid-life leaves ragged rows behind."""
    index = SqliteVectorIndex(store.connection)
    for oid in ("t1", "t2", "t3"):
        put_task(store, oid, oid)
    index.upsert("t1", [1.0, 0.0, 0.0, 0.0], "big", "h")
    index.upsert("t2", [0.0, 1.0, 0.0, 0.0], "big", "h")
    index.upsert("t3", [1.0, 0.0], "small", "h")

    hits = index.search([1.0, 0.0, 0.0, 0.0], k=5)
    assert [h.object_id for h in hits] == ["t1", "t2"]


def test_k_larger_than_corpus_is_safe(store, embedder):
    index = SqliteVectorIndex(store.connection)
    put_task(store, "t1", "billing")
    index.upsert("t1", embedder.embed_one("billing"), embedder.name, "h")
    assert len(index.search(embedder.embed_one("billing"), k=100)) == 1


# ----------------------------------------------------------- fts escaping


@pytest.mark.parametrize(
    "raw",
    [
        'Update "billing" (P1)',
        "AB#12345 -- urgent",
        "foo* bar^ baz~",
        "unbalanced \" quote",
        "NEAR(a b)",
    ],
)
def test_escape_fts_query_produces_valid_match(store, raw):
    """Real titles contain FTS5 syntax; none of them may raise."""
    put_task(store, "t1", "billing")
    match = escape_fts_query(raw)
    if match:
        store.connection.execute(
            "SELECT object_id FROM objects_fts WHERE objects_fts MATCH ?", (match,)
        ).fetchall()


def test_escape_fts_query_of_only_punctuation_is_empty():
    assert escape_fts_query("--- *** ") == ""


# ------------------------------------------------------------ search index


def test_lexical_search_finds_and_ranks(store):
    index = SearchIndex(store.connection)
    put_task(store, "t1", "Fix the billing pipeline", summary="billing billing billing")
    put_task(store, "t2", "Fix the shipping pipeline")

    hits = index.lexical("billing")
    assert [h.object_id for h in hits] == ["t1"]
    assert hits[0].snippet is not None


def test_lexical_search_type_filter(store):
    index = SearchIndex(store.connection)
    put_task(store, "t1", "billing")
    store.put_object(
        Object(id="s1", type="source_item", data={"title": "billing", "source_uri": "u"},
               version=1, provenance={})
    )
    assert [h.object_id for h in index.lexical("billing", object_type="task")] == ["t1"]


def test_lexical_search_with_no_match_is_empty(store):
    index = SearchIndex(store.connection)
    put_task(store, "t1", "billing")
    assert index.lexical("nonexistentterm") == []
    assert index.lexical("") == []


def test_hybrid_fuses_both_retrievers(store, embedder):
    index = SearchIndex(store.connection)
    # Lexically identical to the query, semantically ordinary.
    put_task(store, "lex", "AB#98765 tracking identifier")
    # Semantically close to the query, shares no rare token.
    put_task(store, "sem", "Revise the invoicing documentation")
    for oid, text in (("lex", "AB#98765 tracking identifier"),
                      ("sem", "Revise the invoicing documentation")):
        index.vectors.upsert(oid, embedder.embed_one(text), embedder.name, text_fingerprint(text))

    query = "AB#98765"
    hits = index.hybrid(query, embedder.embed_one(query), k=5)
    ids = [h.object_id for h in hits]
    assert "lex" in ids
    assert hits[0].ranks, "fused hits must record which retriever found them"


def test_hybrid_without_a_vector_is_lexical_only(store):
    index = SearchIndex(store.connection)
    put_task(store, "t1", "billing")
    hits = index.hybrid("billing", None, k=5)
    assert [h.object_id for h in hits] == ["t1"]
    assert set(hits[0].ranks) == {"lexical"}


def test_rrf_score_matches_the_formula(store, embedder):
    """An item found at rank 1 by both retrievers scores 2/(k+1)."""
    index = SearchIndex(store.connection)
    put_task(store, "only", "billing pipeline")
    index.vectors.upsert(
        "only", embedder.embed_one("billing pipeline"), embedder.name, "h"
    )
    hits = index.hybrid("billing pipeline", embedder.embed_one("billing pipeline"), k=1)
    assert hits[0].score == pytest.approx(2.0 / (RRF_K + 1))


def test_candidates_for_excludes_the_probe(store, embedder):
    index = SearchIndex(store.connection)
    for oid in ("t1", "t2", "t3"):
        put_task(store, oid, "billing pipeline work")
        index.vectors.upsert(oid, embedder.embed_one("billing pipeline work"), embedder.name, "h")

    hits = index.candidates_for("t1", "billing pipeline work", embedder.embed_one("billing"), k=5)
    assert "t1" not in [h.object_id for h in hits]
    assert len(hits) == 2


def test_hybrid_result_count_is_capped(store, embedder):
    index = SearchIndex(store.connection)
    for i in range(20):
        put_task(store, f"t{i}", "billing pipeline work")
        index.vectors.upsert(f"t{i}", embedder.embed_one("billing"), embedder.name, "h")
    assert len(index.hybrid("billing", embedder.embed_one("billing"), k=5)) == 5
