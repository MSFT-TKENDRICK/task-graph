"""Vector storage and similarity search over the graph projection.

Deliberately *not* backed by ``sqlite-vec``: it publishes no ``win_arm64``
wheel, and this project runs on Windows on ARM. Rather than require a C
toolchain, vectors are stored as little-endian float32 BLOBs and scored with a
single numpy matrix multiply.

That is not a compromise at this scale. A personal task graph is O(10k) nodes;
10k x 512 float32 is 20 MB and one brute-force pass is well under 100 ms —
faster than an ANN index would be after its own overhead, and exact rather than
approximate. :class:`VectorIndex` is a protocol so an ANN backend can replace
this without touching callers if the graph ever outgrows it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

#: float32, little-endian, C-contiguous. Fixed explicitly so a database written
#: on one architecture stays readable on another.
_DTYPE = np.dtype("<f4")


def pack_vector(vec: Sequence[float]) -> bytes:
    """Encode a vector as a little-endian float32 BLOB."""
    return np.asarray(vec, dtype=_DTYPE).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    """Decode a BLOB written by :func:`pack_vector`."""
    return np.frombuffer(blob, dtype=_DTYPE)


def text_fingerprint(text: str) -> str:
    """Stable digest of embedded text, so unchanged objects are not re-embedded."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True)
class VectorHit:
    object_id: str
    score: float


class VectorIndex(Protocol):
    """Similarity search over stored object embeddings."""

    def upsert(self, object_id: str, vec: Sequence[float], model: str, text_hash: str) -> None: ...

    def search(
        self, query: Sequence[float], k: int = 10, object_type: str | None = None
    ) -> list[VectorHit]: ...


class SqliteVectorIndex:
    """Brute-force cosine index over the ``embeddings`` table.

    Vectors are expected to arrive L2-normalised (the embedding providers
    guarantee this), so cosine similarity is a plain dot product and the whole
    search is one ``matrix @ vector``.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._matrix: np.ndarray | None = None
        self._ids: list[str] = []
        self._types: np.ndarray | None = None
        self._dirty = True

    # ------------------------------------------------------------- mutation

    def upsert(self, object_id: str, vec: Sequence[float], model: str, text_hash: str) -> None:
        array = np.asarray(vec, dtype=_DTYPE)
        self._conn.execute(
            """
            INSERT INTO embeddings(object_id, model, dim, vec, text_hash)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(object_id) DO UPDATE SET
                model = excluded.model,
                dim = excluded.dim,
                vec = excluded.vec,
                text_hash = excluded.text_hash
            """,
            (object_id, model, int(array.size), array.tobytes(), text_hash),
        )
        self._dirty = True

    def upsert_many(self, rows: Iterable[tuple[str, Sequence[float], str, str]]) -> int:
        count = 0
        for object_id, vec, model, text_hash in rows:
            self.upsert(object_id, vec, model, text_hash)
            count += 1
        return count

    def remove(self, object_id: str) -> None:
        self._conn.execute("DELETE FROM embeddings WHERE object_id = ?", (object_id,))
        self._dirty = True

    def invalidate(self) -> None:
        """Force the cached matrix to be rebuilt on next search."""
        self._dirty = True

    # -------------------------------------------------------------- queries

    def stale_objects(self, candidates: dict[str, str], model: str) -> list[str]:
        """Of ``{object_id: text_hash}``, which need embedding under ``model``.

        Anything absent, embedded by a different model, or whose text changed.
        """
        if not candidates:
            return []
        current: dict[str, tuple[str, str]] = {}
        rows = self._conn.execute("SELECT object_id, model, text_hash FROM embeddings").fetchall()
        for row in rows:
            current[row["object_id"]] = (row["model"], row["text_hash"])
        return [
            object_id
            for object_id, text_hash in candidates.items()
            if current.get(object_id) != (model, text_hash)
        ]

    def _load(self) -> None:
        rows = self._conn.execute(
            """
            SELECT e.object_id, e.vec, o.type
            FROM embeddings e
            JOIN objects o ON o.id = e.object_id
            ORDER BY e.object_id
            """
        ).fetchall()
        if not rows:
            self._matrix = None
            self._ids = []
            self._types = None
            self._dirty = False
            return

        vectors = [unpack_vector(r["vec"]) for r in rows]
        # A model change can leave mixed dimensions behind; keep only the
        # majority dimension rather than crashing on a ragged stack.
        width = max({v.size for v in vectors}, key=lambda d: sum(v.size == d for v in vectors))
        keep = [i for i, v in enumerate(vectors) if v.size == width]

        self._matrix = np.vstack([vectors[i] for i in keep])
        self._ids = [rows[i]["object_id"] for i in keep]
        self._types = np.array([rows[i]["type"] for i in keep], dtype=object)
        self._dirty = False

    def search(
        self, query: Sequence[float], k: int = 10, object_type: str | None = None
    ) -> list[VectorHit]:
        if self._dirty:
            self._load()
        if self._matrix is None or not self._ids:
            return []

        q = np.asarray(query, dtype=_DTYPE)
        if q.size != self._matrix.shape[1]:
            return []

        matrix = self._matrix
        ids = self._ids
        if object_type is not None and self._types is not None:
            mask = self._types == object_type
            if not mask.any():
                return []
            matrix = matrix[mask]
            ids = [i for i, keep in zip(self._ids, mask, strict=True) if keep]

        scores = matrix @ q
        k = min(k, scores.size)
        if k <= 0:
            return []
        # argpartition finds the top-k without a full sort, then we sort only
        # those k.
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [VectorHit(object_id=ids[i], score=float(scores[i])) for i in top]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
