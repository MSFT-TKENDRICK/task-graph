"""Lexical, semantic and hybrid retrieval over the graph projection.

Dedupe lives or dies on recall: an ADO work item and an MSX milestone tracking
the same deliverable rarely share wording, but often share an identifier — and
vice versa. So neither retrieval mode alone is sufficient.

The two are combined with Reciprocal Rank Fusion rather than a weighted sum of
scores. BM25 and cosine live on different, unbounded, corpus-dependent scales;
normalising them against each other would need constant re-tuning. RRF only
looks at *rank*, so it is scale-free and stable as the corpus grows.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from task_graph.store.vectors import SqliteVectorIndex

#: Standard RRF damping constant. Larger values flatten the contribution of top
#: ranks; 60 is the value from the original Cormack et al. formulation.
RRF_K = 60

_FTS_SPECIALS = re.compile(r'[":^*(){}\[\]\-+~]')


@dataclass
class SearchHit:
    object_id: str
    score: float
    #: Per-retriever rank, kept so results can be explained ("matched lexically
    #: at rank 2, semantically at rank 7").
    ranks: dict[str, int] = field(default_factory=dict)
    snippet: str | None = None


def escape_fts_query(text: str) -> str:
    """Make arbitrary user text safe as an FTS5 MATCH expression.

    FTS5 raises on unbalanced quotes and stray operators, which is easy to hit
    with titles like ``Update "billing" (P1)``. Tokens are stripped of syntax
    characters and re-quoted so the query is always a valid OR of phrases.
    """
    tokens = [t for t in _FTS_SPECIALS.sub(" ", text).split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


class SearchIndex:
    """Combined FTS5 + vector retrieval bound to one projection database."""

    def __init__(self, connection: sqlite3.Connection, vectors: SqliteVectorIndex | None = None):
        self._conn = connection
        self.vectors = vectors if vectors is not None else SqliteVectorIndex(connection)

    # ---------------------------------------------------------------- lexical

    def lexical(
        self, query: str, k: int = 20, object_type: str | None = None
    ) -> list[SearchHit]:
        """BM25-ranked full-text search. ``bm25()`` is negative-better, so it is
        negated to make larger mean more relevant, consistent with cosine."""
        match = escape_fts_query(query)
        if not match:
            return []

        sql = """
            SELECT object_id, -bm25(objects_fts) AS score,
                   snippet(objects_fts, 2, '[', ']', '...', 12) AS snip
            FROM objects_fts
            WHERE objects_fts MATCH ?
        """
        params: list[object] = [match]
        if object_type is not None:
            sql += " AND type = ?"
            params.append(object_type)
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(k)

        try:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.OperationalError:
            # A malformed MATCH should degrade to "no lexical hits", never take
            # down a dedupe pass.
            return []
        return [
            SearchHit(object_id=r["object_id"], score=float(r["score"]), snippet=r["snip"])
            for r in rows
        ]

    # --------------------------------------------------------------- semantic

    def semantic(
        self, query_vector: Sequence[float], k: int = 20, object_type: str | None = None
    ) -> list[SearchHit]:
        return [
            SearchHit(object_id=hit.object_id, score=hit.score)
            for hit in self.vectors.search(query_vector, k=k, object_type=object_type)
        ]

    # ----------------------------------------------------------------- hybrid

    def hybrid(
        self,
        query: str,
        query_vector: Sequence[float] | None = None,
        k: int = 20,
        object_type: str | None = None,
        candidate_depth: int | None = None,
    ) -> list[SearchHit]:
        """Fuse lexical and semantic results by reciprocal rank.

        ``candidate_depth`` controls how deep each retriever goes before
        fusion; it defaults to ``4 * k`` so an item ranked highly by only one
        retriever can still surface.
        """
        depth = candidate_depth if candidate_depth is not None else max(k * 4, 20)

        runs: dict[str, list[SearchHit]] = {"lexical": self.lexical(query, depth, object_type)}
        if query_vector is not None:
            runs["semantic"] = self.semantic(query_vector, depth, object_type)

        fused: dict[str, SearchHit] = {}
        for retriever, hits in runs.items():
            for rank, hit in enumerate(hits, start=1):
                entry = fused.get(hit.object_id)
                if entry is None:
                    entry = SearchHit(object_id=hit.object_id, score=0.0)
                    fused[hit.object_id] = entry
                entry.score += 1.0 / (RRF_K + rank)
                entry.ranks[retriever] = rank
                if entry.snippet is None and hit.snippet:
                    entry.snippet = hit.snippet

        ordered = sorted(fused.values(), key=lambda h: (-h.score, h.object_id))
        return ordered[:k]

    # ----------------------------------------------------------------- helper

    def candidates_for(
        self,
        object_id: str,
        query: str,
        query_vector: Sequence[float] | None = None,
        k: int = 10,
        object_type: str | None = None,
    ) -> list[SearchHit]:
        """Hybrid search excluding the probe object itself.

        The primary entry point for dedupe candidate generation.
        """
        hits = self.hybrid(query, query_vector, k=k + 1, object_type=object_type)
        return [h for h in hits if h.object_id != object_id][:k]
