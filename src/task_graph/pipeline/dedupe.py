"""Deciding when two tasks track the same work.

This is where "an ADO work item and an MSX milestone are the same deliverable"
gets settled. Getting it wrong in either direction is costly: a false merge
hides work, a missed merge leaves the user reconciling by hand. So the module is
built around three ideas.

**Evidence beats similarity.** A GitHub issue whose body says ``AB#12345`` and
an ADO work item 12345 are the same thing regardless of how differently they are
worded. Textual and semantic similarity are only tie-breakers.

**Merges are proposals, not actions.** Anything short of an explicit
cross-reference is raised as an activegraph patch awaiting approval, so the user
stays in control of how their work is modelled.

**Every decision is explainable and reversible.** Scores decompose into named
features, and a rejection is retained with its reason so the learner can move
the weight that was responsible.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from activegraph import Graph, Object, Patch

from task_graph.learning.weights import Weights
from task_graph.ontology.types import (
    CLOSED_STATES,
    ObjectType,
    RelationType,
)
from task_graph.store.search import SearchIndex
from task_graph.store.sqlite_graph_store import SqliteGraphStore

#: Marker written by an approved merge patch. Read back when materialising.
MERGE_KEY = "merge_into"

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "with",
        "is", "are", "be", "we", "our", "this", "that", "it", "at", "by",
        "update", "fix", "add", "remove", "issue", "task", "bug", "work",
    }
)

#: Identifier shapes worth treating as cross-references between systems.
_ID_PATTERNS = (
    re.compile(r"\bab#(\d+)\b", re.I),            # ADO link syntax in GitHub
    re.compile(r"\bab\[(\d+)\]", re.I),
    re.compile(r"\bworkitems?/(\d+)\b", re.I),
    re.compile(r"\b_workitems/edit/(\d+)\b", re.I),
    re.compile(r"\bissues?/(\d+)\b", re.I),
    re.compile(r"\bpull/(\d+)\b", re.I),
    re.compile(r"#(\d+)\b"),
)


class Decision(StrEnum):
    AUTO_LINK = "auto_link"
    PROPOSE = "propose"
    IGNORE = "ignore"


@dataclass
class PairScore:
    """A scored candidate pair, decomposed so it can be explained and corrected."""

    canonical_id: str
    absorbed_id: str
    score: float
    features: dict[str, float]
    decision: Decision

    def explain(self) -> str:
        contributing = sorted(
            ((n, v) for n, v in self.features.items() if v),
            key=lambda kv: -abs(kv[1]),
        )
        if not contributing:
            return "no supporting evidence"
        readable = {
            "cross_reference": "one explicitly references the other",
            "shared_external_ref": "both reference the same item",
            "title_similarity": "similar titles",
            "embedding_similarity": "similar meaning",
            "same_owner": "same owner",
            "temporal_proximity": "created around the same time",
            "same_source_penalty": "both from the same system",
        }
        return "; ".join(readable.get(name, name) for name, _ in contributing[:3])


@dataclass
class DedupeReport:
    linked: list[PairScore] = field(default_factory=list)
    proposed: list[PairScore] = field(default_factory=list)
    considered: int = 0
    skipped_existing: int = 0

    def summary(self) -> str:
        return (
            f"{self.considered} pairs considered, "
            f"{len(self.linked)} auto-linked, {len(self.proposed)} proposed for review"
        )


# --------------------------------------------------------------- features


def tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 2}


def title_similarity(a: str, b: str) -> float:
    """Blend of token overlap and character-level ratio.

    Jaccard alone misses "billing pipeline" vs "billing pipelines"; the sequence
    ratio alone over-rewards two long, generic, differently-worded titles.
    """
    ta, tb = tokens(a), tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    ratio = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    return round(0.6 * jaccard + 0.4 * ratio, 6)


def identifiers(source: Object) -> set[str]:
    """Identifiers a source record *is*, for cross-reference matching."""
    out: set[str] = set()
    uri = source.data.get("source_uri") or ""
    trailing = re.search(r"(\d+)\s*$", uri)
    if trailing:
        out.add(trailing.group(1))
    return out


def referenced_identifiers(source: Object) -> set[str]:
    """Identifiers a source record *mentions* in its text or explicit refs."""
    haystack = " ".join(
        str(part)
        for part in (
            source.data.get("body") or "",
            source.data.get("title") or "",
            *(source.data.get("external_refs") or []),
        )
    )
    out: set[str] = set()
    for pattern in _ID_PATTERNS:
        out.update(match.group(1) for match in pattern.finditer(haystack))
    return out


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def temporal_proximity(a: Object, b: Object, window_days: float = 30.0) -> float:
    """1.0 for same-day, decaying to 0 across ``window_days``."""
    da = _parse_dt(a.data.get("first_seen_at")) or _parse_dt(a.data.get("created_at"))
    db = _parse_dt(b.data.get("first_seen_at")) or _parse_dt(b.data.get("created_at"))
    if da is None or db is None:
        return 0.0
    if da.tzinfo is None or db.tzinfo is None:
        da, db = da.replace(tzinfo=None), db.replace(tzinfo=None)
    gap_days = abs((da - db).total_seconds()) / 86400.0
    return round(max(0.0, 1.0 - gap_days / window_days), 6)


#: Raw weighted score treated as the decision boundary, and how sharply the
#: curve turns there.
SQUASH_MIDPOINT = 0.8
SQUASH_STEEPNESS = 4.0


def squash(raw: float) -> float:
    """Map an unbounded weighted score onto 0..1.

    Dividing by the sum of all positive weights would assume every feature
    fires at once, which never happens, and would push even decisive evidence
    into the middle of the range. A logistic curve instead keeps the thresholds
    meaningful however corrections move the underlying weights.
    """
    return 1.0 / (1.0 + math.exp(-(raw - SQUASH_MIDPOINT) * SQUASH_STEEPNESS))


class Deduper:
    """Finds, scores and acts on duplicate-task candidates."""

    def __init__(
        self,
        graph: Graph,
        store: SqliteGraphStore,
        weights: Weights | None = None,
        search: SearchIndex | None = None,
        embedder: Any = None,
    ) -> None:
        self.graph = graph
        self.store = store
        self.weights = weights if weights is not None else Weights()
        self.search = search if search is not None else SearchIndex(store.connection)
        self.embedder = embedder

    # ---------------------------------------------------------- inspection

    def sources_for(self, task_id: str) -> list[Object]:
        out = []
        for rel in self.store.find_relations(
            target=task_id, type=RelationType.EVIDENCE_OF.value
        ):
            source = self.store.get_object(rel.source)
            if source is not None:
                out.append(source)
        return out

    def features(self, a: Object, b: Object) -> dict[str, float]:
        """Decompose the evidence that ``a`` and ``b`` are the same work."""
        sources_a, sources_b = self.sources_for(a.id), self.sources_for(b.id)

        ids_a = set().union(*(identifiers(s) for s in sources_a)) if sources_a else set()
        ids_b = set().union(*(identifiers(s) for s in sources_b)) if sources_b else set()
        refs_a = (
            set().union(*(referenced_identifiers(s) for s in sources_a)) if sources_a else set()
        )
        refs_b = (
            set().union(*(referenced_identifiers(s) for s in sources_b)) if sources_b else set()
        )

        cross = bool((refs_a & ids_b) or (refs_b & ids_a))
        # Shared third-party references only count once the direct link is ruled
        # out, otherwise a cross-reference double-counts.
        shared = bool((refs_a & refs_b) - ids_a - ids_b) and not cross

        systems_a = {s.data.get("source") for s in sources_a}
        systems_b = {s.data.get("source") for s in sources_b}
        same_system = bool(systems_a and systems_b and systems_a == systems_b)

        owner_a, owner_b = a.data.get("owner"), b.data.get("owner")

        return {
            "cross_reference": 1.0 if cross else 0.0,
            "shared_external_ref": 1.0 if shared else 0.0,
            "title_similarity": title_similarity(
                a.data.get("title", ""), b.data.get("title", "")
            ),
            "embedding_similarity": self._embedding_similarity(a.id, b.id),
            "same_owner": 1.0 if owner_a and owner_a == owner_b else 0.0,
            "temporal_proximity": temporal_proximity(a, b),
            "same_source_penalty": 1.0 if same_system else 0.0,
        }

    def _embedding_similarity(self, a_id: str, b_id: str) -> float:
        import numpy as np

        from task_graph.store.vectors import unpack_vector

        rows = self.store.connection.execute(
            "SELECT object_id, vec FROM embeddings WHERE object_id IN (?, ?)", (a_id, b_id)
        ).fetchall()
        vectors = {r["object_id"]: unpack_vector(r["vec"]) for r in rows}
        if len(vectors) != 2:
            return 0.0
        va, vb = vectors[a_id], vectors[b_id]
        if va.size != vb.size:
            return 0.0
        # Vectors are stored L2-normalised, so the dot product is the cosine.
        # Clamped to 0: negative similarity is not evidence of anything here.
        return round(max(0.0, float(np.dot(va, vb))), 6)

    # -------------------------------------------------------------- scoring

    @staticmethod
    def canonical_order(a: Object, b: Object) -> tuple[Object, Object]:
        """Pick which task survives a merge, deterministically.

        Prefer the task backed by more sources (it already unifies more), then
        the one seen first, then the lexicographically smaller id so the choice
        is reproducible across runs.
        """
        def rank(o: Object) -> tuple[int, str, str]:
            return (
                -len(o.data.get("source_uris") or []),
                str(o.data.get("first_seen_at") or "9999"),
                o.id,
            )

        return (a, b) if rank(a) <= rank(b) else (b, a)

    def score_pair(self, a: Object, b: Object) -> PairScore:
        canonical, absorbed = self.canonical_order(a, b)
        features = self.features(canonical, absorbed)
        raw = self.weights.score("dedupe", features)
        score = squash(raw)

        if score >= self.weights.auto_link_threshold:
            decision = Decision.AUTO_LINK
        elif score >= self.weights.propose_threshold:
            decision = Decision.PROPOSE
        else:
            decision = Decision.IGNORE

        return PairScore(
            canonical_id=canonical.id,
            absorbed_id=absorbed.id,
            score=round(score, 6),
            features=features,
            decision=decision,
        )

    # ------------------------------------------------------------ candidates

    def candidates_for(self, task: Object, k: int = 8) -> list[Object]:
        title = task.data.get("title") or ""
        summary = task.data.get("summary") or ""
        query = f"{title} {summary}".strip()
        if not query:
            return []

        vector = None
        if self.embedder is not None:
            vector = self.embedder.embed_one(query)

        hits = self.search.candidates_for(
            task.id, query, vector, k=k, object_type=ObjectType.TASK.value
        )
        out = []
        for hit in hits:
            other = self.store.get_object(hit.object_id)
            if other is not None and other.data.get("state") not in CLOSED_STATES:
                out.append(other)
        return out

    def already_related(self, a_id: str, b_id: str) -> bool:
        """True if the pair is already linked or already awaiting a decision.

        Without this, every sync would re-propose merges the user has already
        seen — the fastest way to make an approval queue useless.
        """
        for source, target in ((a_id, b_id), (b_id, a_id)):
            for rel_type in (RelationType.SAME_AS.value, RelationType.DUPLICATE_OF.value):
                if self.store.find_relations(source=source, target=target, type=rel_type):
                    return True
        for patch in self.store.all_patches():
            if patch.value.get(MERGE_KEY) in (a_id, b_id) and patch.target in (a_id, b_id):
                return True
        return False

    # ---------------------------------------------------------------- acting

    def run(self, k: int = 8, actor: str = "dedupe") -> DedupeReport:
        report = DedupeReport()
        tasks = [
            t
            for t in self.store.find_objects(ObjectType.TASK.value)
            if t.data.get("state") not in CLOSED_STATES
        ]
        seen_pairs: set[tuple[str, str]] = set()

        for task in tasks:
            for other in self.candidates_for(task, k=k):
                key = tuple(sorted((task.id, other.id)))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)

                if self.already_related(task.id, other.id):
                    report.skipped_existing += 1
                    continue

                report.considered += 1
                pair = self.score_pair(task, other)
                if pair.decision is Decision.AUTO_LINK:
                    self.link(pair, actor=actor)
                    report.linked.append(pair)
                elif pair.decision is Decision.PROPOSE:
                    self.propose_merge(pair, actor=actor)
                    report.proposed.append(pair)

        return report

    def link(self, pair: PairScore, actor: str = "dedupe") -> None:
        """Create the confirmed link and fold the absorbed task's evidence in."""
        self.graph.add_relation(
            pair.absorbed_id,
            pair.canonical_id,
            RelationType.DUPLICATE_OF.value,
            {"score": pair.score, "features": pair.features, "why": pair.explain()},
            actor=actor,
        )
        self._absorb(pair.canonical_id, pair.absorbed_id, actor=actor)

    def propose_merge(self, pair: PairScore, actor: str = "dedupe") -> Patch:
        """Raise the merge for approval rather than performing it.

        Expressed as a patch on the absorbed task so it rides activegraph's
        proposed -> applied|rejected lifecycle; the rejection reason is then
        retained automatically for the learner.
        """
        evidence = [
            f"{name}={value:.3f}" for name, value in sorted(pair.features.items()) if value
        ]
        return self.graph.propose_patch(
            pair.absorbed_id,
            "update",
            {MERGE_KEY: pair.canonical_id, "merge_score": pair.score},
            proposed_by=actor,
            rationale=f"Same work as {pair.canonical_id}: {pair.explain()}",
            evidence=evidence,
        )

    def apply_merge(self, patch_id: str, approved_by: str = "user") -> str:
        """Approve a proposed merge and materialise the link."""
        patch = self.store.get_patch(patch_id)
        if patch is None:
            raise KeyError(f"unknown patch: {patch_id}")
        canonical_id = patch.value.get(MERGE_KEY)
        if not canonical_id:
            raise ValueError(f"patch {patch_id} is not a merge proposal")

        self.graph.apply_patch(patch_id, approved_by=approved_by)
        self.graph.add_relation(
            patch.target,
            canonical_id,
            RelationType.DUPLICATE_OF.value,
            {"approved_by": approved_by},
            actor=approved_by,
        )
        self._absorb(canonical_id, patch.target, actor=approved_by)
        return canonical_id

    def reject_merge(self, patch_id: str, reason: str, actor: str = "user") -> None:
        """Decline a proposed merge, retaining why for the learner."""
        self.graph.reject_patch(patch_id, reason, actor=actor)

    def _absorb(self, canonical_id: str, absorbed_id: str, actor: str) -> None:
        """Move evidence onto the canonical task and retire the absorbed one."""
        canonical = self.store.get_object(canonical_id)
        absorbed = self.store.get_object(absorbed_id)
        if canonical is None or absorbed is None:
            return

        for rel in self.store.find_relations(
            target=absorbed_id, type=RelationType.EVIDENCE_OF.value
        ):
            if not self.store.find_relations(
                source=rel.source, target=canonical_id, type=RelationType.EVIDENCE_OF.value
            ):
                self.graph.add_relation(
                    rel.source, canonical_id, RelationType.EVIDENCE_OF.value, actor=actor
                )

        merged_uris = list(canonical.data.get("source_uris") or [])
        for uri in absorbed.data.get("source_uris") or []:
            if uri not in merged_uris:
                merged_uris.append(uri)

        self.graph.patch_object(canonical_id, {"source_uris": merged_uris}, actor=actor)
        # The absorbed task is retained rather than deleted so the merge stays
        # visible and reversible; it is simply no longer open work.
        self.graph.patch_object(
            absorbed_id, {"state": "dropped", "merged_into": canonical_id}, actor=actor
        )

    def pending_merges(self) -> list[Patch]:
        return [
            p
            for p in self.store.all_patches()
            if p.status == "proposed" and MERGE_KEY in p.value
        ]
