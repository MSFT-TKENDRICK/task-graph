"""Learned weights that steer modelling and prioritisation.

Every number the system uses to make a judgement call lives here rather than
being hard-coded at its call site, because all of them are things the user is
entitled to disagree with. A correction is ultimately just a nudge to one of
these values, and persisting them as plain JSON keeps that auditable and
hand-editable.

Defaults are chosen to be conservative: dedupe would rather ask than merge
wrongly, because an incorrect merge hides work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Contribution of each dedupe feature to the pair score. Features are computed
#: in ``pipeline.dedupe`` and are all normalised to 0..1 before weighting.
DEFAULT_DEDUPE_WEIGHTS: dict[str, float] = {
    # A record explicitly naming the other's identifier is near-proof, and is
    # weighted heavily enough to clear the auto-link threshold on its own.
    "cross_reference": 2.0,
    "shared_external_ref": 0.7,
    "title_similarity": 0.5,
    "embedding_similarity": 0.45,
    "same_owner": 0.15,
    "temporal_proximity": 0.1,
    # Unifying work across systems is the point; same-source pairs are usually
    # genuinely distinct items, so they are penalised.
    "same_source_penalty": -0.35,
}

#: Contribution of each priority factor. See ``pipeline.priority``.
DEFAULT_PRIORITY_WEIGHTS: dict[str, float] = {
    "urgency": 1.0,
    "source_importance": 0.6,
    "blocking": 0.8,
    "staleness": 0.3,
    "explicit_ask": 0.5,
    "owner_is_me": 0.4,
}

#: Score at or above which a link is created without asking. Set high: only
#: effectively-certain evidence (an explicit cross-reference) should clear it.
DEFAULT_AUTO_LINK_THRESHOLD = 0.9

#: Score at or above which a merge is proposed for approval.
DEFAULT_PROPOSE_THRESHOLD = 0.45

#: How far a single correction moves a weight. Small, so one atypical
#: correction cannot swamp the model.
DEFAULT_LEARNING_RATE = 0.08


@dataclass
class Weights:
    """Mutable, persisted tuning parameters."""

    dedupe: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_DEDUPE_WEIGHTS)
    )
    priority: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PRIORITY_WEIGHTS)
    )
    auto_link_threshold: float = DEFAULT_AUTO_LINK_THRESHOLD
    propose_threshold: float = DEFAULT_PROPOSE_THRESHOLD
    learning_rate: float = DEFAULT_LEARNING_RATE
    #: Count of corrections folded in, so `tg doctor` can show how trained the
    #: model is and tests can assert learning actually happened.
    corrections_applied: int = 0

    # ------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: str | Path) -> Weights:
        """Load weights, falling back to defaults when absent or corrupt.

        A damaged weights file must never block a sync; defaults are always a
        usable model.
        """
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()

        weights = cls()
        weights.dedupe.update(raw.get("dedupe") or {})
        weights.priority.update(raw.get("priority") or {})
        weights.auto_link_threshold = float(
            raw.get("auto_link_threshold", DEFAULT_AUTO_LINK_THRESHOLD)
        )
        weights.propose_threshold = float(
            raw.get("propose_threshold", DEFAULT_PROPOSE_THRESHOLD)
        )
        weights.learning_rate = float(raw.get("learning_rate", DEFAULT_LEARNING_RATE))
        weights.corrections_applied = int(raw.get("corrections_applied", 0))
        return weights

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dedupe": self.dedupe,
            "priority": self.priority,
            "auto_link_threshold": self.auto_link_threshold,
            "propose_threshold": self.propose_threshold,
            "learning_rate": self.learning_rate,
            "corrections_applied": self.corrections_applied,
        }

    # -------------------------------------------------------------- learning

    def nudge(self, section: str, feature: str, direction: float) -> float:
        """Move one weight by ``direction * learning_rate``.

        ``direction`` is +1 when the feature argued for the outcome the user
        confirmed, -1 when it argued for the outcome the user rejected. Weights
        are clamped to [-2, 2] so a run of corrections cannot make one feature
        dominate everything else.
        """
        table = getattr(self, section, None)
        if not isinstance(table, dict) or feature not in table:
            raise KeyError(f"unknown weight: {section}.{feature}")
        updated = table[feature] + direction * self.learning_rate
        table[feature] = max(-3.0, min(3.0, updated))
        return table[feature]

    def score(self, section: str, features: dict[str, float]) -> float:
        """Weighted sum of ``features`` using the weights in ``section``."""
        table: dict[str, float] = getattr(self, section)
        return sum(value * table.get(name, 0.0) for name, value in features.items())
