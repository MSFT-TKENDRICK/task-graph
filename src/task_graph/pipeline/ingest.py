"""Turning connector output into graph nodes, idempotently.

Ingest is deliberately dumb: one ``source_item`` per source record, one ``task``
per ``source_item``, linked by ``EVIDENCE_OF``. It makes no judgement about
whether two records describe the same work — that is dedupe's job, and keeping
the two apart means a bad merge can be corrected without re-fetching anything.

The invariant that matters here is idempotency: re-running a sync must update
existing nodes, never duplicate them. That is anchored on ``source_uri``, which
connectors guarantee is stable across syncs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from activegraph import Graph, Object

from task_graph.connectors.base import SourceItem
from task_graph.embeddings import EmbeddingProvider
from task_graph.ontology.types import (
    ObjectType,
    RelationType,
    SourceKind,
    TaskState,
)
from task_graph.store.search import SearchIndex
from task_graph.store.sqlite_graph_store import SqliteGraphStore, searchable_text
from task_graph.store.vectors import text_fingerprint

#: How each source system's own vocabulary maps onto our lifecycle. Lower-cased
#: on lookup. Kept as plain data because it is one of the first things user
#: corrections will want to adjust.
STATE_MAP: dict[SourceKind, dict[str, TaskState]] = {
    SourceKind.GITHUB: {
        "open": TaskState.ACTIVE,
        "draft": TaskState.ACTIVE,
        "closed": TaskState.DONE,
        "merged": TaskState.DONE,
    },
    SourceKind.ADO: {
        "new": TaskState.TRIAGE,
        "to do": TaskState.TRIAGE,
        "proposed": TaskState.TRIAGE,
        "active": TaskState.ACTIVE,
        "committed": TaskState.ACTIVE,
        "in progress": TaskState.ACTIVE,
        "doing": TaskState.ACTIVE,
        "blocked": TaskState.BLOCKED,
        "resolved": TaskState.DONE,
        "closed": TaskState.DONE,
        "done": TaskState.DONE,
        "completed": TaskState.DONE,
        "removed": TaskState.DROPPED,
        "cut": TaskState.DROPPED,
    },
    SourceKind.MAIL: {
        "flagged": TaskState.ACTIVE,
        "notstarted": TaskState.TRIAGE,
        "complete": TaskState.DONE,
        "completed": TaskState.DONE,
    },
    SourceKind.PLANNER: {
        "notstarted": TaskState.TRIAGE,
        "inprogress": TaskState.ACTIVE,
        "completed": TaskState.DONE,
    },
    # Verified against the live Agency calendar MCP, which reports either a
    # show-as value, an invite response, or "cancelled".
    SourceKind.CALENDAR: {
        "cancelled": TaskState.DROPPED,
        "declined": TaskState.DROPPED,
        "organizer": TaskState.ACTIVE,
        "accepted": TaskState.ACTIVE,
        "tentativelyaccepted": TaskState.WAITING,
        "tentative": TaskState.WAITING,
        "notresponded": TaskState.TRIAGE,
        "busy": TaskState.ACTIVE,
        "oof": TaskState.WAITING,
        "free": TaskState.TRIAGE,
    },
    # Teams messages carry no lifecycle of their own — the connector packs
    # importance/unread flags into source_state — so they land in TRIAGE and
    # are resolved by the user, which is the correct default for an inbound ask.
    SourceKind.MSX: {
        # Sales stages: work is live until the deal reaches a terminal state.
        "qualify": TaskState.ACTIVE,
        "develop": TaskState.ACTIVE,
        "propose": TaskState.ACTIVE,
        "close": TaskState.ACTIVE,
        "open": TaskState.ACTIVE,
        "in progress": TaskState.ACTIVE,
        "inprogress": TaskState.ACTIVE,
        "blocked": TaskState.BLOCKED,
        "on hold": TaskState.WAITING,
        # Terminal states.
        "won": TaskState.DONE,
        "lost": TaskState.DROPPED,
        "completed": TaskState.DONE,
        "closed": TaskState.DONE,
        "canceled": TaskState.DROPPED,
        "cancelled": TaskState.DROPPED,
    },
}


def map_state(source: SourceKind | str, source_state: str | None) -> TaskState:
    """Translate a source system's state into our lifecycle.

    Unknown states and unknown sources deliberately fall back to ``TRIAGE``
    rather than guessing: something unrecognised is something the user should
    look at.
    """
    if not source_state:
        return TaskState.TRIAGE
    try:
        kind = source if isinstance(source, SourceKind) else SourceKind(source)
    except ValueError:
        return TaskState.TRIAGE
    return STATE_MAP.get(kind, {}).get(source_state.strip().lower(), TaskState.TRIAGE)


#: Fields whose change should cause a re-write (and re-embed) of a source item.
_FINGERPRINT_FIELDS = (
    "title",
    "body",
    "source_state",
    "owner",
    "assignees",
    "due_at",
    "labels",
    "external_refs",
)


def item_fingerprint(item: SourceItem) -> str:
    """Digest of the fields we actually care about.

    Sources bump ``updated_at`` for reasons that do not concern us (a reaction,
    a field we ignore); hashing only the meaningful fields avoids pointless
    re-embedding on every sync.
    """
    payload = {f: getattr(item, f, None) for f in _FINGERPRINT_FIELDS}
    return hashlib.blake2b(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8"), digest_size=16
    ).hexdigest()


@dataclass
class IngestReport:
    """What a sync actually did. Surfaced by ``tg sync`` and the MCP tool."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    tasks_created: list[str] = field(default_factory=list)
    embedded: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)

    def summary(self) -> str:
        parts = [
            f"{len(self.created)} new",
            f"{len(self.updated)} updated",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.embedded:
            parts.append(f"{self.embedded} embedded")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


class Ingestor:
    """Writes connector output into the graph."""

    def __init__(
        self,
        graph: Graph,
        store: SqliteGraphStore,
        embedder: EmbeddingProvider | None = None,
        search: SearchIndex | None = None,
    ) -> None:
        self.graph = graph
        self.store = store
        self.embedder = embedder
        self.search = search if search is not None else SearchIndex(store.connection)

    # ------------------------------------------------------------------ main

    def ingest(self, items: Iterable[SourceItem], actor: str = "ingest") -> IngestReport:
        report = IngestReport()
        with self.store.bulk_writes():
            for item in items:
                try:
                    self._ingest_one(item, actor, report)
                except Exception as exc:  # one bad record must not abort a sync
                    report.errors.append(f"{getattr(item, 'source_uri', '?')}: {exc}")
        if self.embedder is not None:
            report.embedded = self.embed_stale()
        return report

    def _ingest_one(self, item: SourceItem, actor: str, report: IngestReport) -> None:
        existing = self.store.get_object_by_source_uri(item.source_uri)
        fingerprint = item_fingerprint(item)
        now = datetime.now(UTC)

        if existing is None:
            source_obj = self.graph.add_object(
                ObjectType.SOURCE_ITEM.value,
                {**item.to_props(), "content_hash": fingerprint, "ingested_at": now.isoformat()},
                actor=actor,
            )
            task = self._create_task_for(item, source_obj, actor, now)
            report.created.append(source_obj.id)
            report.tasks_created.append(task.id)
            return

        if existing.data.get("content_hash") == fingerprint:
            report.unchanged.append(existing.id)
            return

        self.graph.patch_object(
            existing.id,
            {**item.to_props(), "content_hash": fingerprint, "ingested_at": now.isoformat()},
            actor=actor,
        )
        self._propagate_to_task(existing.id, item, actor, now)
        report.updated.append(existing.id)

    # ----------------------------------------------------------------- tasks

    def _create_task_for(
        self, item: SourceItem, source_obj: Object, actor: str, now: datetime
    ) -> Object:
        task = self.graph.add_object(
            ObjectType.TASK.value,
            {
                "title": item.title,
                "summary": (item.body or "")[:500],
                "state": map_state(item.source, item.source_state).value,
                "owner": item.owner,
                "due_at": item.due_at.isoformat() if item.due_at else None,
                "source_uris": [item.source_uri],
                "first_seen_at": now.isoformat(),
                "last_seen_at": now.isoformat(),
            },
            actor=actor,
        )
        self.graph.add_relation(
            source_obj.id, task.id, RelationType.EVIDENCE_OF.value, actor=actor
        )
        return task

    def _propagate_to_task(
        self, source_id: str, item: SourceItem, actor: str, now: datetime
    ) -> None:
        """Push a changed source record onto the canonical tasks it backs.

        A task may be backed by several sources after dedupe, so only fields
        this source is authoritative for are pushed, and the task's own state is
        only advanced — never regressed — by one source going quiet.
        """
        for task in self.tasks_for_source(source_id):
            update: dict[str, Any] = {"last_seen_at": now.isoformat()}
            new_state = map_state(item.source, item.source_state)

            uris = list(task.data.get("source_uris") or [])
            if item.source_uri not in uris:
                uris.append(item.source_uri)
                update["source_uris"] = uris

            # Only one source backs this task, so it is authoritative.
            if len(uris) == 1:
                update["title"] = item.title
                update["state"] = new_state.value
                if item.due_at:
                    update["due_at"] = item.due_at.isoformat()
            elif new_state in (TaskState.DONE, TaskState.DROPPED):
                # With several sources, closure is only believed when every
                # backing source agrees; otherwise closing an ADO item would
                # silently close work still open in MSX.
                if self._all_sources_closed(task, item):
                    update["state"] = new_state.value

            self.graph.patch_object(task.id, update, actor=actor)

    def _all_sources_closed(self, task: Object, latest: SourceItem) -> bool:
        for uri in task.data.get("source_uris") or []:
            if uri == latest.source_uri:
                continue
            other = self.store.get_object_by_source_uri(uri)
            if other is None:
                continue
            state = map_state(other.data.get("source", ""), other.data.get("source_state"))
            if state not in (TaskState.DONE, TaskState.DROPPED):
                return False
        return True

    def tasks_for_source(self, source_id: str) -> list[Object]:
        """Canonical tasks a source item is evidence for."""
        out: list[Object] = []
        for rel in self.store.find_relations(
            source=source_id, type=RelationType.EVIDENCE_OF.value
        ):
            task = self.store.get_object(rel.target)
            if task is not None:
                out.append(task)
        return out

    def sources_for_task(self, task_id: str) -> list[Object]:
        out: list[Object] = []
        for rel in self.store.find_relations(
            target=task_id, type=RelationType.EVIDENCE_OF.value
        ):
            source = self.store.get_object(rel.source)
            if source is not None:
                out.append(source)
        return out

    # ------------------------------------------------------------ embeddings

    def embed_stale(self, object_types: tuple[str, ...] = ("task", "source_item")) -> int:
        """Embed anything whose searchable text changed since it was last embedded.

        Batched through the provider so a Foundry Local round trip covers many
        objects at once.
        """
        if self.embedder is None:
            return 0

        wanted: dict[str, str] = {}
        texts: dict[str, str] = {}
        for obj in self.store.find_objects_in_types(list(object_types)):
            text = searchable_text(obj.type, obj.data)
            if not text:
                continue
            texts[obj.id] = text
            wanted[obj.id] = text_fingerprint(text)

        stale = self.search.vectors.stale_objects(wanted, self.embedder.name)
        if not stale:
            return 0

        vectors = self.embedder.embed([texts[oid] for oid in stale])
        for object_id, vector in zip(stale, vectors, strict=True):
            self.search.vectors.upsert(
                object_id, vector, self.embedder.name, wanted[object_id]
            )
        self.search.vectors.invalidate()
        return len(stale)
