"""SQLite-backed :class:`activegraph.GraphStore`.

The activegraph event log is the source of truth; this store is the queryable
*projection* of it. Losing it is recoverable (``tg rebuild`` replays the log),
which is what makes it safe to keep denormalised search structures — an FTS5
index and embedding vectors — right next to the graph itself.

Only the structural query hooks are pushed down into SQL. ``match_chain`` is
deliberately left to the base class: it is defined in terms of
:meth:`find_objects` / :meth:`find_relations`, both of which *are* pushed down,
so it gets the index speedup without risking semantic divergence from
activegraph's homomorphic matching rules.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from activegraph import GraphStore, Object, Patch, Relation

SCHEMA_VERSION = "1"
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Per-object-type projection of the fields worth indexing for lexical search.
#: Ordered most- to least-important; FTS5 has no field weighting here, so the
#: ordering only affects snippet quality.
_SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "source_item": ("title", "body", "owner", "labels", "external_refs"),
    "task": ("title", "summary", "owner"),
    "person": ("display_name", "email", "upn", "aliases"),
    "account": ("name", "tpid"),
    "opportunity": ("name", "stage"),
    "milestone": ("name", "status"),
    "project": ("name",),
    "remediation": ("action", "preview", "rationale"),
    "correction": ("rationale",),
}

#: Fallback when an object type has no explicit projection.
_DEFAULT_SEARCH_FIELDS = ("title", "name", "summary", "body")


def searchable_text(obj_type: str, data: dict[str, Any]) -> str:
    """Flatten an object's indexable fields into one FTS document.

    Lists are joined rather than skipped so labels and cross-reference
    identifiers (a strong dedupe signal) stay searchable.
    """
    fields = _SEARCH_FIELDS.get(obj_type, _DEFAULT_SEARCH_FIELDS)
    parts: list[str] = []
    for field in fields:
        value = data.get(field)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value if v)
        elif isinstance(value, dict):
            continue
        else:
            text = str(value).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


class SqliteGraphStore(GraphStore):
    """Property-graph projection in a single SQLite file."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the MCP server can serve from its event
        # loop's worker threads; every mutation is serialised by _lock anyway.
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._in_bulk = False
        self._init_schema()

    # ------------------------------------------------------------- lifecycle

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif row["value"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"graph projection at {self.path} is schema version "
                    f"{row['value']}, expected {SCHEMA_VERSION}. "
                    "Run `tg rebuild` to regenerate it from the event log."
                )

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying connection, for sibling modules (search, vectors)."""
        return self._conn

    @contextmanager
    def bulk_writes(self) -> Iterator[None]:
        """Batch many mutations into one transaction.

        Replaying a long event log one autocommit at a time is dominated by
        fsync; wrapping the replay in this makes rebuild roughly two orders of
        magnitude faster. Re-entrant, so nested use is harmless.
        """
        with self._lock:
            if self._in_bulk:
                yield
                return
            self._in_bulk = True
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._in_bulk = False

    def clear(self) -> None:
        with self._lock, self.bulk_writes():
            for table in ("objects", "relations", "patches", "objects_fts", "embeddings"):
                self._conn.execute(f"DELETE FROM {table}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --------------------------------------------------------- (de)serialise

    @staticmethod
    def _to_object(row: sqlite3.Row) -> Object:
        return Object(
            id=row["id"],
            type=row["type"],
            data=json.loads(row["data"]),
            version=row["version"],
            provenance=json.loads(row["provenance"]),
        )

    @staticmethod
    def _to_relation(row: sqlite3.Row) -> Relation:
        return Relation(
            id=row["id"],
            source=row["source"],
            target=row["target"],
            type=row["type"],
            data=json.loads(row["data"]),
            provenance=json.loads(row["provenance"]),
        )

    @staticmethod
    def _to_patch(row: sqlite3.Row) -> Patch:
        return Patch(
            id=row["id"],
            target=row["target"],
            op=row["op"],
            value=json.loads(row["value"]),
            expected_version=row["expected_version"],
            proposed_by=row["proposed_by"],
            rationale=row["rationale"],
            evidence=json.loads(row["evidence"]),
            status=row["status"],
            rejection_reason=row["rejection_reason"],
            provenance=json.loads(row["provenance"]),
        )

    # --------------------------------------------------------------- objects

    def put_object(self, obj: Object) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO objects(id, type, data, version, provenance)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    type = excluded.type,
                    data = excluded.data,
                    version = excluded.version,
                    provenance = excluded.provenance
                """,
                (
                    obj.id,
                    obj.type,
                    json.dumps(obj.data, default=str),
                    obj.version,
                    json.dumps(obj.provenance, default=str),
                ),
            )
            self._reindex_fts(obj.id, obj.type, obj.data)

    def get_object(self, object_id: str) -> Object | None:
        row = self._conn.execute("SELECT * FROM objects WHERE id = ?", (object_id,)).fetchone()
        return self._to_object(row) if row else None

    def remove_object(self, object_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM objects WHERE id = ?", (object_id,))
            self._conn.execute("DELETE FROM objects_fts WHERE object_id = ?", (object_id,))
            self._conn.execute("DELETE FROM embeddings WHERE object_id = ?", (object_id,))

    def all_objects(self) -> list[Object]:
        rows = self._conn.execute("SELECT * FROM objects").fetchall()
        return [self._to_object(r) for r in rows]

    def get_object_by_source_uri(self, source_uri: str) -> Object | None:
        """Resolve a source record to its ``source_item`` object.

        The backbone of idempotent ingest: re-syncing a source must update the
        existing node rather than create a second one. Backed by the partial
        unique index on the generated ``source_uri`` column.
        """
        row = self._conn.execute(
            "SELECT * FROM objects WHERE source_uri = ?", (source_uri,)
        ).fetchone()
        return self._to_object(row) if row else None

    def source_uris(self) -> set[str]:
        """Every ``source_uri`` already ingested, for cheap bulk diffing."""
        rows = self._conn.execute(
            "SELECT source_uri FROM objects WHERE source_uri IS NOT NULL"
        ).fetchall()
        return {r["source_uri"] for r in rows}

    # ------------------------------------------------------------- relations

    def put_relation(self, rel: Relation) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relations(id, source, target, type, data, provenance)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    target = excluded.target,
                    type = excluded.type,
                    data = excluded.data,
                    provenance = excluded.provenance
                """,
                (
                    rel.id,
                    rel.source,
                    rel.target,
                    rel.type,
                    json.dumps(rel.data, default=str),
                    json.dumps(rel.provenance, default=str),
                ),
            )

    def get_relation(self, relation_id: str) -> Relation | None:
        row = self._conn.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
        return self._to_relation(row) if row else None

    def remove_relation(self, relation_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM relations WHERE id = ?", (relation_id,))

    def all_relations(self) -> list[Relation]:
        rows = self._conn.execute("SELECT * FROM relations").fetchall()
        return [self._to_relation(r) for r in rows]

    # --------------------------------------------------------------- patches

    def put_patch(self, patch: Patch) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO patches(
                    id, target, op, value, expected_version, proposed_by,
                    rationale, evidence, status, rejection_reason, provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    target = excluded.target,
                    op = excluded.op,
                    value = excluded.value,
                    expected_version = excluded.expected_version,
                    proposed_by = excluded.proposed_by,
                    rationale = excluded.rationale,
                    evidence = excluded.evidence,
                    status = excluded.status,
                    rejection_reason = excluded.rejection_reason,
                    provenance = excluded.provenance
                """,
                (
                    patch.id,
                    patch.target,
                    patch.op,
                    json.dumps(patch.value, default=str),
                    patch.expected_version,
                    patch.proposed_by,
                    patch.rationale,
                    json.dumps(list(patch.evidence), default=str),
                    patch.status,
                    patch.rejection_reason,
                    json.dumps(patch.provenance, default=str),
                ),
            )

    def get_patch(self, patch_id: str) -> Patch | None:
        row = self._conn.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
        return self._to_patch(row) if row else None

    def remove_patch(self, patch_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM patches WHERE id = ?", (patch_id,))

    def all_patches(self) -> list[Patch]:
        rows = self._conn.execute("SELECT * FROM patches").fetchall()
        return [self._to_patch(r) for r in rows]

    # -------------------------------------------------------- query pushdown

    def find_objects(self, type: str | None = None) -> list[Object]:  # noqa: A002
        if type is None:
            return self.all_objects()
        rows = self._conn.execute("SELECT * FROM objects WHERE type = ?", (type,)).fetchall()
        return [self._to_object(r) for r in rows]

    def find_objects_in_types(self, types: list[str]) -> list[Object]:
        if not types:
            return []
        placeholders = ",".join("?" * len(types))
        rows = self._conn.execute(
            f"SELECT * FROM objects WHERE type IN ({placeholders})", tuple(types)
        ).fetchall()
        return [self._to_object(r) for r in rows]

    def find_relations(
        self,
        source: str | None = None,
        target: str | None = None,
        type: str | None = None,  # noqa: A002
    ) -> list[Relation]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (("source", source), ("target", target), ("type", type)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM relations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._to_relation(r) for r in rows]

    def neighborhood(self, object_id: str, depth: int = 1) -> tuple[list[Object], list[Relation]]:
        """Undirected breadth-first walk, identical in semantics to the base class.

        The base implementation scans *every* relation once per depth level. This
        runs the same level-by-level algorithm but fetches only the edges
        incident to the current frontier, using the ``source``/``target``
        indexes. Keeping the algorithm identical (rather than expressing it as a
        recursive CTE) is what guarantees the result sets match exactly.
        """
        if self.get_object(object_id) is None:
            return ([], [])

        seen_objects = {object_id}
        frontier = {object_id}
        seen_relations: dict[str, Relation] = {}

        for _ in range(max(depth, 0)):
            next_frontier: set[str] = set()
            for rel in self._incident_relations(frontier):
                seen_relations.setdefault(rel.id, rel)
                if rel.source not in seen_objects:
                    next_frontier.add(rel.source)
                if rel.target not in seen_objects:
                    next_frontier.add(rel.target)
            seen_objects |= next_frontier
            frontier = next_frontier
            if not frontier:
                break

        objects = [obj for obj in (self.get_object(i) for i in seen_objects) if obj is not None]
        return (objects, list(seen_relations.values()))

    def _incident_relations(self, node_ids: set[str]) -> list[Relation]:
        """Every relation with an endpoint in ``node_ids``, in one indexed query."""
        if not node_ids:
            return []
        ids = tuple(node_ids)
        # Chunked to stay under SQLITE_MAX_VARIABLE_NUMBER on wide frontiers.
        out: dict[str, Relation] = {}
        chunk_size = 400
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT * FROM relations WHERE source IN ({placeholders}) "
                f"UNION SELECT * FROM relations WHERE target IN ({placeholders})",
                chunk + chunk,
            ).fetchall()
            for row in rows:
                out.setdefault(row["id"], self._to_relation(row))
        return list(out.values())

    # ------------------------------------------------------------------ FTS

    def _reindex_fts(self, object_id: str, obj_type: str, data: dict[str, Any]) -> None:
        text = searchable_text(obj_type, data)
        self._conn.execute("DELETE FROM objects_fts WHERE object_id = ?", (object_id,))
        if text:
            self._conn.execute(
                "INSERT INTO objects_fts(object_id, type, text) VALUES (?, ?, ?)",
                (object_id, obj_type, text),
            )

    def rebuild_fts(self) -> int:
        """Re-derive the whole FTS index. Used after changing the text projection."""
        with self._lock, self.bulk_writes():
            self._conn.execute("DELETE FROM objects_fts")
            count = 0
            for row in self._conn.execute("SELECT id, type, data FROM objects").fetchall():
                text = searchable_text(row["type"], json.loads(row["data"]))
                if text:
                    self._conn.execute(
                        "INSERT INTO objects_fts(object_id, type, text) VALUES (?, ?, ?)",
                        (row["id"], row["type"], text),
                    )
                    count += 1
            return count

    # ---------------------------------------------------------------- counts

    def counts(self) -> dict[str, int]:
        """Row counts per table, for ``tg doctor`` and tests."""
        out: dict[str, int] = {}
        for table in ("objects", "relations", "patches", "embeddings"):
            out[table] = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        out["objects_fts"] = self._conn.execute(
            "SELECT COUNT(*) AS n FROM objects_fts"
        ).fetchone()["n"]
        return out
