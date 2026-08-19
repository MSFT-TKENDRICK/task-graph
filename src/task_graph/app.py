"""The service facade shared by the CLI and the MCP server.

Both front ends are thin wrappers over this class, so a capability only has to
be written once and cannot drift between the two surfaces.

Wiring note: the run id is persisted in the projection's ``meta`` table. Events
in activegraph are scoped to a run, so reusing the id across process restarts is
what makes ``events.db`` a single continuous history rather than a pile of
disconnected runs — and it is what makes ``rebuild`` able to reconstruct the
whole graph.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from activegraph import Graph, Object, SQLiteEventStore

from task_graph.config import Settings, get_settings
from task_graph.connectors.base import (
    SourceItem,
    get_connector,
    registered_connectors,
)
from task_graph.embeddings import describe_embedder, get_embedder
from task_graph.learning.corrections import Corrector, LearningReport
from task_graph.learning.weights import Weights
from task_graph.ontology.types import CLOSED_STATES, ObjectType, SourceKind
from task_graph.pipeline import priority as priority_mod
from task_graph.pipeline.approval import ApprovalQueue
from task_graph.pipeline.crm import CrmProjector, CrmReport
from task_graph.pipeline.dedupe import Deduper, DedupeReport
from task_graph.pipeline.ingest import Ingestor, IngestReport
from task_graph.pipeline.remediation import propose_remediations
from task_graph.progress import Reporter, null_reporter
from task_graph.store import SearchIndex, SqliteGraphStore

_RUN_ID_KEY = "run_id"


@dataclass
class SyncReport:
    ingest: IngestReport
    dedupe: DedupeReport
    crm: CrmReport
    ranked: int = 0
    proposed_actions: int = 0
    errors: list[str] | None = None

    def summary(self) -> str:
        parts = [
            self.ingest.summary(),
            self.dedupe.summary(),
            self.crm.summary(),
            f"{self.ranked} tasks ranked",
        ]
        if self.proposed_actions:
            parts.append(f"{self.proposed_actions} action(s) proposed")
        return "; ".join(parts)


class TaskGraphApp:
    """Everything wired together, opened against one state directory."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedder: Any = None,
        reporter: Reporter | None = None,
    ) -> None:
        report = reporter or null_reporter()
        self.settings = settings or get_settings()
        self.settings.ensure_home()

        report.log("opening the projection")
        self.store = SqliteGraphStore(self.settings.graph_db)
        run_id = self._resolve_run_id()

        report.log("reading the event log")
        self.graph = Graph(graph_store=self.store, run_id=run_id)
        self.events = SQLiteEventStore(str(self.settings.events_db), run_id=run_id)
        self.graph.ids.reseed_from_events(self.events.iter_events())
        self.graph.attach_store(self.events)

        report.log("loading learned weights")
        self.weights = Weights.load(self.settings.weights_path)
        report.log("preparing the embedder")
        self.embedder = embedder if embedder is not None else get_embedder(self.settings)
        report.log("opening the search index")
        self.search = SearchIndex(self.store.connection)

        self.ingestor = Ingestor(self.graph, self.store, self.embedder, self.search)
        self.deduper = Deduper(
            self.graph, self.store, self.weights, self.search, self.embedder
        )
        self.crm_projector = CrmProjector(self.graph, self.store)
        self.corrector = Corrector(self.graph, self.store, self.weights)
        self.approvals = ApprovalQueue(self.graph)

    # ------------------------------------------------------------- lifecycle

    def _resolve_run_id(self) -> str:
        row = self.store.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (_RUN_ID_KEY,)
        ).fetchone()
        if row is not None:
            return row["value"]
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        self.store.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)", (_RUN_ID_KEY, run_id)
        )
        return run_id

    def save_weights(self) -> None:
        self.weights.save(self.settings.weights_path)

    def close(self) -> None:
        self.save_weights()
        self.store.close()
        self.events.close()

    def __enter__(self) -> TaskGraphApp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ sync

    def sync(
        self,
        sources: Iterable[SourceKind | str] | None = None,
        *,
        since: datetime | None = None,
        dedupe: bool = True,
        rank: bool = True,
        propose: bool = False,
        reporter: Reporter | None = None,
    ) -> SyncReport:
        """Pull from sources, unify, rank and optionally propose actions.

        ``propose`` is off by default: proposals are only useful once the user
        has reviewed the graph, and generating them on every scheduled sync
        would fill the approval queue with noise.

        Progress is reported per source because that is where the time goes:
        each connector spawns an `agency mcp` server and waits on it, so
        "which source are we on" is the only number that moves for whole
        seconds at a time.
        """
        report = reporter or null_reporter()
        kinds = [SourceKind(s) for s in sources] if sources else self.enabled_sources(report)
        items: list[SourceItem] = []
        errors: list[str] = []

        report.phase("sources", total=len(kinds))
        for kind in kinds:
            report.log(f"{kind.value}: connecting")
            try:
                connector = get_connector(kind)
                status = connector.is_available()
                if not status.available:
                    errors.append(f"{kind.value}: {status.detail}")
                    report.advance(message=f"{kind.value}: unavailable")
                    continue
                fetched = list(connector.fetch(since=since))
                items.extend(fetched)
                report.advance(message=f"{kind.value}: {len(fetched)} items")
            except Exception as exc:
                errors.append(f"{kind.value}: {exc}")
                report.advance(message=f"{kind.value}: failed")

        ingest_report = self.ingestor.ingest(items, reporter=report)
        ingest_report.errors.extend(errors)

        report.phase("dedupe")
        dedupe_report = self.deduper.run() if dedupe else DedupeReport()

        report.phase("crm")
        try:
            crm_report = self.crm()
        except Exception as exc:  # CRM context must not make source sync fail
            errors.append(f"crm: {exc}")
            crm_report = CrmReport(errors=[str(exc)])

        report.phase("rank")
        ranked = self.rank() if rank else []

        proposed = 0
        if propose:
            report.phase("propose", total=len(ranked))
            for task, _ in ranked:
                proposed += len(propose_remediations(self.graph, task))
                report.advance()

        report.phase("done", message=f"{len(items)} items from {len(kinds)} sources")
        return SyncReport(
            ingest=ingest_report,
            dedupe=dedupe_report,
            crm=crm_report,
            ranked=len(ranked),
            proposed_actions=proposed,
            errors=errors,
        )

    def _probe(self, kind: SourceKind) -> dict[str, Any]:
        """Probe one connector, never raising."""
        try:
            status = get_connector(kind).is_available()
        except Exception as exc:
            return {
                "check": f"connector:{kind.value}",
                "ok": False,
                "detail": str(exc),
                "remediation": None,
            }
        return {
            "check": f"connector:{kind.value}",
            "ok": status.available,
            "detail": status.detail,
            "remediation": status.remediation,
        }

    def _probe_all(
        self, kinds: list[SourceKind], report: Reporter
    ) -> dict[SourceKind, dict[str, Any]]:
        """Probe every connector at once, reporting each as it lands.

        Probes are independent subprocess spawns that spend their time waiting,
        so running them one after another simply adds up the waits -- six
        sources took over thirty seconds of which almost none was work. Results
        are collected into a dict and read back in registration order, so
        finishing out of order does not make the output jump around.
        """
        results: dict[SourceKind, dict[str, Any]] = {}
        if not kinds:
            return results
        with ThreadPoolExecutor(max_workers=min(8, len(kinds))) as pool:
            futures = {pool.submit(self._probe, kind): kind for kind in kinds}
            for future in as_completed(futures):
                kind = futures[future]
                results[kind] = future.result()
                report.advance(message=f"{kind.value} checked")
        return results

    def enabled_sources(self, reporter: Reporter | None = None) -> list[SourceKind]:
        """Connectors that are registered and currently usable.

        Each probe spawns a server and waits on it, so this reports per source:
        it is the first thing a bare `sync` does and used to be a silent wait.
        """
        report = reporter or null_reporter()
        kinds = registered_connectors()
        report.phase("probing", total=len(kinds))
        results = self._probe_all(kinds, report)
        return [kind for kind in kinds if results.get(kind, {}).get("ok")]

    # -------------------------------------------------------------- querying

    def rank(self, *, now: datetime | None = None) -> list[tuple[Object, Any]]:
        return priority_mod.persist_scores(
            self.graph, self.store, self.weights, now=now, identity=self.identity()
        )

    def crm(self) -> CrmReport:
        return self.crm_projector.run()

    def triage(self, limit: int = 20) -> list[tuple[Object, Any]]:
        """The ranked list of open work — the system's primary answer."""
        return priority_mod.rank_tasks(
            self.store, self.weights, identity=self.identity()
        )[:limit]

    def explain_priority(self, task_id: str) -> Any:
        return priority_mod.explain(
            task_id, self.store, self.weights, identity=self.identity()
        )

    def search_tasks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        vector = self.embedder.embed_one(query) if self.embedder else None
        hits = self.search.hybrid(query, vector, k=limit, object_type=ObjectType.TASK.value)
        out = []
        for hit in hits:
            task = self.store.get_object(hit.object_id)
            if task is None:
                continue
            out.append(
                {
                    "id": task.id,
                    "title": task.data.get("title"),
                    "state": task.data.get("state"),
                    "priority": task.data.get("priority"),
                    "score": round(hit.score, 6),
                    "matched": sorted(hit.ranks),
                    "snippet": hit.snippet,
                }
            )
        return out

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_object(task_id)
        if task is None or task.type != ObjectType.TASK.value:
            return None
        sources = self.ingestor.sources_for_task(task_id)
        return {
            "id": task.id,
            "data": task.data,
            "sources": [
                {
                    "id": s.id,
                    "source": s.data.get("source"),
                    "source_uri": s.data.get("source_uri"),
                    "title": s.data.get("title"),
                    "url": s.data.get("url"),
                    "state": s.data.get("source_state"),
                }
                for s in sources
            ],
        }

    def task_graph(self, task_id: str, depth: int = 2) -> dict[str, Any]:
        objects, relations = self.store.neighborhood(task_id, depth)
        return {
            "objects": [{"id": o.id, "type": o.type, "data": o.data} for o in objects],
            "relations": [
                {"id": r.id, "source": r.source, "target": r.target, "type": r.type}
                for r in relations
            ],
        }

    def identity(self) -> set[str]:
        """Who "me" is, for owner and direct-ask detection."""
        import os

        return priority_mod.build_identity(
            os.environ.get("TASK_GRAPH_IDENTITY"),
            os.environ.get("USERNAME"),
            os.environ.get("USERPRINCIPALNAME"),
        )

    # -------------------------------------------------------------- approvals

    def pending_approvals(self) -> list[Object]:
        return self.approvals.pending()

    def pending_merges(self) -> list[dict[str, Any]]:
        out = []
        for patch in self.deduper.pending_merges():
            canonical = self.store.get_object(patch.value.get("merge_into", ""))
            absorbed = self.store.get_object(patch.target)
            out.append(
                {
                    "patch_id": patch.id,
                    "canonical": {"id": getattr(canonical, "id", None),
                                  "title": (canonical.data.get("title") if canonical else None)},
                    "absorbed": {"id": getattr(absorbed, "id", None),
                                 "title": (absorbed.data.get("title") if absorbed else None)},
                    "score": patch.value.get("merge_score"),
                    "rationale": patch.rationale,
                    "evidence": list(patch.evidence),
                }
            )
        return out

    def approve_merge(self, patch_id: str, actor: str = "user") -> str:
        patch = self.store.get_patch(patch_id)
        canonical_id = self.deduper.apply_merge(patch_id, approved_by=actor)
        if patch is not None:
            self.corrector.confirm_merge(patch, actor=actor)
        return canonical_id

    def reject_merge(self, patch_id: str, reason: str, actor: str = "user") -> None:
        patch = self.store.get_patch(patch_id)
        self.deduper.reject_merge(patch_id, reason, actor=actor)
        if patch is not None:
            self.corrector.reject_merge(patch, reason, actor=actor)

    def propose_for(self, task_id: str) -> list[Object]:
        task = self.store.get_object(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        return propose_remediations(self.graph, task)

    # ------------------------------------------------------------- learning

    def learn(self) -> LearningReport:
        report = self.corrector.learn()
        self.save_weights()
        return report

    # -------------------------------------------------------------- rebuild

    def rebuild(self, reporter: Reporter | None = None) -> dict[str, int]:
        """Reconstruct the projection from the event log.

        The projection is disposable by design; this is what makes that claim
        true. The old file is kept alongside as ``.bak`` until the rebuild
        succeeds.

        Embeddings are *not* in the event log — they are derived from object
        text — so they are regenerated afterwards. Skipping that would leave
        semantic search silently degraded until the next sync.
        """
        report = reporter or null_reporter()
        run_id = self.graph.run_id
        report.phase("read")
        events = list(self.events.iter_events())

        self.store.close()
        graph_db = self.settings.graph_db
        backup = graph_db.with_suffix(".db.bak")
        if graph_db.exists():
            backup.unlink(missing_ok=True)
            shutil.move(str(graph_db), str(backup))

        fresh_store = SqliteGraphStore(graph_db)
        fresh_store.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (_RUN_ID_KEY, run_id)
        )
        fresh_graph = Graph(graph_store=fresh_store, run_id=run_id)
        report.phase("replay", total=len(events))
        with fresh_store.bulk_writes():
            for event in events:
                fresh_graph.emit(event)
                report.advance()
        # Replay re-emits existing ids; without reseeding, the next add_object
        # would restart the counters and collide with what was just replayed.
        fresh_graph.ids.reseed_from_events(events)

        self.store = fresh_store
        self.graph = fresh_graph
        self.search = SearchIndex(fresh_store.connection)
        self.ingestor = Ingestor(self.graph, self.store, self.embedder, self.search)
        self.deduper = Deduper(
            self.graph, self.store, self.weights, self.search, self.embedder
        )
        self.crm_projector = CrmProjector(self.graph, self.store)
        self.corrector = Corrector(self.graph, self.store, self.weights)
        self.approvals = ApprovalQueue(self.graph)

        report.phase("embed")
        re_embedded = self.ingestor.embed_stale() if self.embedder is not None else 0

        counts = self.store.counts()
        counts["events_replayed"] = len(events)
        counts["re_embedded"] = re_embedded
        report.phase("done", message=f"{len(events)} events replayed")
        return counts

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        counts = self.store.counts()
        open_tasks = [
            t
            for t in self.store.find_objects(ObjectType.TASK.value)
            if t.data.get("state") not in CLOSED_STATES
        ]
        return {
            "home": str(self.settings.home),
            "run_id": self.graph.run_id,
            "events": self.events.count(),
            "objects": counts["objects"],
            "relations": counts["relations"],
            "open_tasks": len(open_tasks),
            "pending_merges": len(self.deduper.pending_merges()),
            "pending_approvals": len(self.approvals.pending()),
            "embeddings": counts["embeddings"],
            "embedder": describe_embedder(self.settings),
            "corrections_applied": self.weights.corrections_applied,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def preflight(self, reporter: Reporter | None = None) -> list[dict[str, Any]]:
        """Per-dependency health, backing ``tg doctor``.

        Each connector probe spawns a server and waits up to its probe timeout,
        so this reports per check: a doctor run that says nothing for half a
        minute is the thing that made the CLI feel hung.
        """
        report = reporter or null_reporter()
        checks: list[dict[str, Any]] = []
        report.log("listing connectors")
        kinds = registered_connectors()
        report.phase("checks", total=len(kinds) + 2)

        results = self._probe_all(kinds, report)
        checks.extend(results[kind] for kind in kinds if kind in results)

        checks.append(
            {
                "check": "agency",
                "ok": shutil.which("agency") is not None,
                "detail": "Agency CLI provides ambient-auth MCP servers (mail, ado, teams...)",
                "remediation": None if shutil.which("agency") else "Install the Agency CLI",
            }
        )
        report.advance(message="agency checked")
        checks.append(
            {
                "check": "embeddings",
                "ok": True,
                "detail": describe_embedder(self.settings),
                "remediation": (
                    None
                    if shutil.which("foundry")
                    else "winget install Microsoft.FoundryLocal for on-device embeddings"
                ),
            }
        )
        report.advance(message="embeddings checked")
        return checks
