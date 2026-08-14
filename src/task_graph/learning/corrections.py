"""Turning user corrections into changed behaviour.

A correction that is merely recorded is a complaint; a correction that moves a
weight is learning. Every correction here does three things: it is stored as a
first-class ``correction`` object so the history is auditable, it nudges the
weights that were responsible for the mistake, and it can be replayed against
past decisions to show what would have changed.

Blame assignment is deliberately simple. When the user rejects a merge, the
features that argued loudest for it are nudged down in proportion to how loudly
they argued. Over many corrections this converges on the user's actual notion of
"the same work" without ever needing a training pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from activegraph import Graph, Object, Patch

from task_graph.learning.weights import Weights
from task_graph.ontology.types import (
    CorrectionKind,
    ObjectType,
    RelationType,
)
from task_graph.store.sqlite_graph_store import SqliteGraphStore

#: ``name=0.812`` entries written onto a merge proposal's evidence list.
_EVIDENCE = re.compile(r"^([a-z_]+)=(-?\d+(?:\.\d+)?)$")

#: Marks a correction as already folded into the weights, so re-running the
#: learner is idempotent.
APPLIED_KEY = "applied"


def parse_evidence(evidence: list[str]) -> dict[str, float]:
    """Recover the feature vector recorded on a proposal."""
    out: dict[str, float] = {}
    for entry in evidence or []:
        match = _EVIDENCE.match(str(entry).strip())
        if match:
            out[match.group(1)] = float(match.group(2))
    return out


@dataclass
class LearningReport:
    corrections_applied: int = 0
    weights_changed: dict[str, float] = field(default_factory=dict)
    skipped: int = 0

    def summary(self) -> str:
        if not self.corrections_applied:
            return "no new corrections to learn from"
        moved = ", ".join(f"{k} -> {v:+.3f}" for k, v in sorted(self.weights_changed.items()))
        return f"learned from {self.corrections_applied} correction(s): {moved or 'no net change'}"


@dataclass
class SimulatedChange:
    patch_id: str
    was: str
    now: str


class Corrector:
    """Records corrections and folds them into the weights."""

    def __init__(
        self,
        graph: Graph,
        store: SqliteGraphStore,
        weights: Weights | None = None,
    ) -> None:
        self.graph = graph
        self.store = store
        self.weights = weights if weights is not None else Weights()

    # ------------------------------------------------------------ recording

    def record(
        self,
        kind: CorrectionKind | str,
        subject_ids: list[str],
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        rationale: str = "",
        actor: str = "user",
    ) -> Object:
        """Store a correction and link it to what it corrects."""
        correction = self.graph.add_object(
            ObjectType.CORRECTION.value,
            {
                "kind": CorrectionKind(kind).value,
                "subject_ids": list(subject_ids),
                "before": before or {},
                "after": after or {},
                "rationale": rationale,
                "created_at": datetime.now(UTC).isoformat(),
                APPLIED_KEY: False,
            },
            actor=actor,
        )
        for subject_id in subject_ids:
            if self.store.get_object(subject_id) is not None:
                self.graph.add_relation(
                    correction.id, subject_id, RelationType.CORRECTS.value, actor=actor
                )
        return correction

    def pending(self) -> list[Object]:
        return [
            c
            for c in self.store.find_objects(ObjectType.CORRECTION.value)
            if not c.data.get(APPLIED_KEY)
        ]

    # ------------------------------------------------------------- learning

    def learn(self, actor: str = "learner") -> LearningReport:
        """Fold every unapplied correction into the weights.

        Idempotent: corrections are marked applied, so running this after each
        sync never double-counts.
        """
        report = LearningReport()
        before = {**self.weights.dedupe, **self.weights.priority}

        for correction in self.pending():
            if self._apply_one(correction):
                report.corrections_applied += 1
                self.weights.corrections_applied += 1
            else:
                report.skipped += 1
            self.graph.patch_object(correction.id, {APPLIED_KEY: True}, actor=actor)

        after = {**self.weights.dedupe, **self.weights.priority}
        report.weights_changed = {
            name: round(after[name] - before[name], 6)
            for name in after
            if abs(after[name] - before.get(name, 0.0)) > 1e-9
        }
        return report

    def _apply_one(self, correction: Object) -> bool:
        kind = correction.data.get("kind")
        if kind == CorrectionKind.DEDUPE.value:
            return self._learn_dedupe(correction)
        if kind == CorrectionKind.PRIORITY.value:
            return self._learn_priority(correction)
        # Remediation, field-mapping and not-a-task corrections are recorded for
        # the proposal rules to consult directly; they have no scalar weight to
        # move, so there is nothing to learn here.
        return False

    def _learn_dedupe(self, correction: Object) -> bool:
        """Blame the features that argued for the rejected conclusion.

        ``direction`` is -1 when the user said "not the same" (the features that
        scored highly were wrong) and +1 when they confirmed a merge the system
        was unsure about.
        """
        features = correction.data.get("before", {}).get("features") or {}
        if not features:
            return False
        agreed = bool(correction.data.get("after", {}).get("same_work"))
        direction = 1.0 if agreed else -1.0

        peak = max((abs(v) for v in features.values()), default=0.0)
        if peak <= 0:
            return False

        changed = False
        for name, value in features.items():
            if name not in self.weights.dedupe or not value:
                continue
            # Scale by how loudly this feature argued, so a feature that barely
            # contributed is barely blamed.
            self.weights.nudge("dedupe", name, direction * (value / peak))
            changed = True
        return changed

    def _learn_priority(self, correction: Object) -> bool:
        """Raise or lower the factors behind a mis-ranked task."""
        factors = correction.data.get("before", {}).get("factors") or {}
        after = correction.data.get("after", {})
        if not factors or "direction" not in after:
            return False
        try:
            direction = float(after["direction"])
        except (TypeError, ValueError):
            return False
        if direction == 0:
            return False

        peak = max((abs(v) for v in factors.values()), default=0.0)
        if peak <= 0:
            return False

        changed = False
        for name, value in factors.items():
            if name not in self.weights.priority or not value:
                continue
            self.weights.nudge(
                "priority", name, (1.0 if direction > 0 else -1.0) * (value / peak)
            )
            changed = True
        return changed

    # ------------------------------------------------- convenience entrypoints

    def reject_merge(
        self, patch: Patch, reason: str, actor: str = "user"
    ) -> Object:
        """Record "these are not the same work" from a rejected merge proposal.

        Call *after* the patch has been rejected; the feature vector is
        recovered from the proposal's evidence so the learner knows what to
        blame.
        """
        return self.record(
            CorrectionKind.DEDUPE,
            [patch.target, str(patch.value.get("merge_into", ""))],
            before={"features": parse_evidence(patch.evidence), "patch_id": patch.id},
            after={"same_work": False},
            rationale=reason,
            actor=actor,
        )

    def confirm_merge(self, patch: Patch, actor: str = "user") -> Object:
        return self.record(
            CorrectionKind.DEDUPE,
            [patch.target, str(patch.value.get("merge_into", ""))],
            before={"features": parse_evidence(patch.evidence), "patch_id": patch.id},
            after={"same_work": True},
            rationale="confirmed by user",
            actor=actor,
        )

    def reprioritize_task(
        self, task: Object, direction: float, rationale: str = "", actor: str = "user"
    ) -> Object:
        """Record "this should rank higher/lower than you ranked it".

        ``direction`` is positive to raise the task, negative to lower it; the
        factors that drove the original score are nudged accordingly.
        """
        return self.record(
            CorrectionKind.PRIORITY,
            [task.id],
            before={"factors": task.data.get("priority_factors") or {},
                    "score": task.data.get("priority")},
            after={"direction": direction},
            rationale=rationale,
            actor=actor,
        )

    #: Kept so earlier call sites keep working.
    repriorit_task = reprioritize_task

    def not_a_task(self, task: Object, rationale: str = "", actor: str = "user") -> Object:
        """Record that something ingested is not actually work.

        Also drops the task, so the correction has an immediate effect rather
        than only a future one.
        """
        correction = self.record(
            CorrectionKind.NOT_A_TASK,
            [task.id],
            before={"state": task.data.get("state"), "title": task.data.get("title")},
            after={"state": "dropped"},
            rationale=rationale,
            actor=actor,
        )
        self.graph.patch_object(task.id, {"state": "dropped"}, actor=actor)
        return correction

    # ------------------------------------------------------------ evaluation

    def simulate(self, candidate: Weights) -> list[SimulatedChange]:
        """Show which past merge decisions ``candidate`` weights would change.

        This is the "would learning from this correction have helped?" check —
        answered against real history rather than intuition, without mutating
        anything.
        """
        from task_graph.pipeline.dedupe import MERGE_KEY, Decision, squash

        def decide(weights: Weights, features: dict[str, float]) -> Decision:
            score = squash(weights.score("dedupe", features))
            if score >= weights.auto_link_threshold:
                return Decision.AUTO_LINK
            if score >= weights.propose_threshold:
                return Decision.PROPOSE
            return Decision.IGNORE

        changes: list[SimulatedChange] = []
        for patch in self.store.all_patches():
            if MERGE_KEY not in patch.value:
                continue
            features = parse_evidence(patch.evidence)
            if not features:
                continue
            was = decide(self.weights, features)
            now = decide(candidate, features)
            if was is not now:
                changes.append(SimulatedChange(patch_id=patch.id, was=was.value, now=now.value))
        return changes
