"""Explainable priority scoring for canonical tasks."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from activegraph import Graph, Object

from task_graph.learning.weights import Weights
from task_graph.ontology.models import PriorityBreakdown
from task_graph.ontology.types import CLOSED_STATES, ObjectType, RelationType
from task_graph.store.sqlite_graph_store import SqliteGraphStore

FACTOR_NAMES = (
    "urgency",
    "source_importance",
    "blocking",
    "staleness",
    "explicit_ask",
    "owner_is_me",
)

NO_DUE_BASELINE = 0.15
MISSING_STALENESS_BASELINE = 0.10
MAX_BLOCKING_DEPTH = 6

DIRECT_ASK_PATTERNS = (
    re.compile(r"\b(can you|could you|please|need you to|please review|follow up)\b", re.I),
    re.compile(r"\?", re.I),
)


def build_identity(*values: str | Iterable[str] | None) -> set[str]:
    """Normalise user identifiers for ownership checks.

    Email addresses, UPNs and aliases are accepted. Email local parts are added
    as aliases so either ``ada`` or ``ada@example.com`` can match.
    """

    out: set[str] = set()
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, Iterable) and not isinstance(value, str) else [value]
        for item in items:
            text = str(item).strip().lower()
            if not text:
                continue
            out.add(text)
            if "@" in text:
                out.add(text.split("@", 1)[0])
    return out


def urgency(task: Object, now: datetime) -> float:
    """Priority pressure from ``due_at``.

    Overdue items are 1.0. Future due dates decay exponentially toward the
    undated baseline so far-future dates do not dominate the queue.
    """

    due = _parse_datetime(task.data.get("due_at"))
    if due is None:
        return NO_DUE_BASELINE
    now = _aware(now)
    if due <= now:
        return 1.0
    days = max((due - now).total_seconds() / 86_400, 0.0)
    return _clamp(NO_DUE_BASELINE + (1.0 - NO_DUE_BASELINE) * math.exp(-days / 14.0))


def source_importance(task: Object, sources: Iterable[Object]) -> float:
    """Importance inferred from backing source records.

    Rules are intentionally data-shaped and readable so corrections can later
    point to one source rule rather than a hidden heuristic.
    """

    candidates = [_source_rule_score(source) for source in sources]
    if not candidates:
        return 0.20
    return max(candidates)


def blocking(
    task: Object, store: SqliteGraphStore, *, max_depth: int = MAX_BLOCKING_DEPTH
) -> float:
    """How much open work is transitively waiting on this task."""

    visited = {task.id}
    frontier = {task.id}
    blocked_open: set[str] = set()

    for _ in range(max(max_depth, 0)):
        next_frontier: set[str] = set()
        for node_id in sorted(frontier):
            for dependent_id in _waiting_on(node_id, store):
                if dependent_id in visited:
                    continue
                visited.add(dependent_id)
                dependent = store.get_object(dependent_id)
                if dependent is None or dependent.type != ObjectType.TASK.value:
                    continue
                if _is_open(dependent):
                    blocked_open.add(dependent_id)
                    next_frontier.add(dependent_id)
        if not next_frontier:
            break
        frontier = next_frontier

    return _clamp(len(blocked_open) / 5.0)


def staleness(task: Object, now: datetime) -> float:
    """Raise work that has not been seen or updated for a long time."""

    seen = _parse_datetime(task.data.get("last_seen_at")) or _parse_datetime(
        task.data.get("updated_at")
    )
    if seen is None:
        return MISSING_STALENESS_BASELINE
    age_days = max((_aware(now) - seen).total_seconds() / 86_400, 0.0)
    return _clamp(0.80 * (1.0 - math.exp(-age_days / 30.0)))


def explicit_ask(task: Object, sources: Iterable[Object]) -> float:
    """Whether the backing records directly ask the user to act."""

    scores = [_source_explicit_ask(source) for source in sources]
    if task.data.get("assignees"):
        scores.append(0.85)
    return max(scores, default=0.0)


def owner_is_me(task: Object, identity: set[str] | None) -> float:
    """Whether this task is owned by or assigned to the current user."""

    if not identity:
        return 0.0
    values = [task.data.get("owner"), *(task.data.get("assignees") or [])]
    for value in values:
        if _identity_matches(value, identity):
            return 1.0
    return 0.0


def score_task(
    task: Object,
    *,
    store: SqliteGraphStore,
    weights: Weights,
    now: datetime | None = None,
    identity: set[str] | Iterable[str] | None = None,
) -> PriorityBreakdown:
    """Compute a task's priority and plain-language explanation."""

    now = _aware(now or datetime.now(UTC))
    user_identity = _coerce_identity(identity)
    sources = _sources_for_task(task, store)
    factors = {
        "urgency": urgency(task, now),
        "source_importance": source_importance(task, sources),
        "blocking": blocking(task, store),
        "staleness": staleness(task, now),
        "explicit_ask": explicit_ask(task, sources),
        "owner_is_me": owner_is_me(task, user_identity),
    }
    raw = weights.score("priority", factors)
    normalizer = sum(abs(weights.priority.get(name, 0.0)) for name in FACTOR_NAMES) or 1.0
    score = _clamp(raw / normalizer)
    return PriorityBreakdown(
        score=score,
        factors={name: _clamp(factors[name]) for name in FACTOR_NAMES},
        explanation=_explanation(task, sources, factors, weights, store, now, user_identity),
    )


def rank_tasks(
    store: SqliteGraphStore,
    weights: Weights,
    *,
    now: datetime | None = None,
    identity: set[str] | Iterable[str] | None = None,
) -> list[tuple[Object, PriorityBreakdown]]:
    """Return open tasks sorted by priority descending with stable tie breaks."""

    scored: list[tuple[Object, PriorityBreakdown]] = []
    for task in store.find_objects(ObjectType.TASK.value):
        if not _is_open(task):
            continue
        scored.append(
            (task, score_task(task, store=store, weights=weights, now=now, identity=identity))
        )
    return sorted(scored, key=lambda item: (-item[1].score, _title(item[0]), item[0].id))


def persist_scores(
    graph: Graph,
    store: SqliteGraphStore,
    weights: Weights,
    *,
    now: datetime | None = None,
    identity: set[str] | Iterable[str] | None = None,
    actor: str = "priority",
) -> list[tuple[Object, PriorityBreakdown]]:
    """Write cached priority fields back to open tasks."""

    ranked = rank_tasks(store, weights, now=now, identity=identity)
    with store.bulk_writes():
        for task, breakdown in ranked:
            graph.patch_object(
                task.id,
                {
                    "priority": breakdown.score,
                    "priority_factors": breakdown.factors,
                },
                actor=actor,
            )
    return ranked


def explain(
    task_id: str,
    store: SqliteGraphStore,
    weights: Weights,
    *,
    now: datetime | None = None,
    identity: set[str] | Iterable[str] | None = None,
) -> PriorityBreakdown:
    """Explain priority for one task."""

    task = store.get_object(task_id)
    if task is None:
        raise KeyError(f"unknown task: {task_id}")
    if task.type != ObjectType.TASK.value:
        raise ValueError(f"object is not a task: {task_id}")
    return score_task(task, store=store, weights=weights, now=now, identity=identity)


def _source_rule_score(source: Object) -> float:
    data = source.data
    source_kind = str(data.get("source") or "").lower()
    source_uri = str(data.get("source_uri") or "").lower()
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    labels = {str(label).lower() for label in data.get("labels") or []}
    title = str(data.get("title") or "")

    table = (
        (_is_fyi, 0.10),
        (lambda: source_kind == "msx" and _money_value(data, raw) > 0, 1.00),
        (lambda: source_kind == "msx" and _has_any(data, raw, "milestone", "closeDate"), 0.90),
        (lambda: source_kind == "github" and ":pr:" in source_uri and _review_requested(raw), 0.88),
        (lambda: source_kind == "mail" and _direct_to_me(raw), 0.78),
        (lambda: source_kind == "ado" and bool(data.get("assignees")), 0.70),
        (lambda: source_kind == "planner", 0.60),
        (lambda: source_kind == "github" and bool(data.get("assignees")), 0.58),
        (lambda: bool(labels & {"p0", "p1", "sev1", "critical", "urgent"}), 0.82),
        (lambda: source_kind in {"github", "ado", "mail", "teams", "calendar", "msx"}, 0.45),
    )
    for predicate, score in table:
        try:
            if predicate() if predicate is not _is_fyi else predicate(title, labels):
                return score
        except (TypeError, ValueError):
            continue
    return 0.30


def _source_explicit_ask(source: Object) -> float:
    data = source.data
    source_kind = str(data.get("source") or "").lower()
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    text = f"{data.get('title') or ''}\n{data.get('body') or ''}"
    labels = {str(label).lower() for label in data.get("labels") or []}

    if _review_requested(raw):
        return 1.0
    if bool(data.get("assignees")):
        return 0.90
    if source_kind == "mail" and _direct_to_me(raw) and _contains_ask(text):
        return 0.90
    if raw.get("mentionsMe") or raw.get("isMentioned"):
        return 0.75
    if labels & {"assigned", "review-requested", "needs-response"}:
        return 0.70
    return 0.0


def _waiting_on(task_id: str, store: SqliteGraphStore) -> set[str]:
    """Task ids directly waiting on ``task_id``."""

    waiting: set[str] = set()
    for rel in store.find_relations(source=task_id, type=RelationType.BLOCKS.value):
        waiting.add(rel.target)
    for rel in store.find_relations(target=task_id, type=RelationType.DEPENDS_ON.value):
        waiting.add(rel.source)
    return waiting


def _sources_for_task(task: Object, store: SqliteGraphStore) -> list[Object]:
    sources: dict[str, Object] = {}
    for rel in store.find_relations(target=task.id, type=RelationType.EVIDENCE_OF.value):
        source = store.get_object(rel.source)
        if source is not None:
            sources[source.id] = source
    for uri in task.data.get("source_uris") or []:
        source = store.get_object_by_source_uri(str(uri))
        if source is not None:
            sources[source.id] = source
    return [sources[key] for key in sorted(sources)]


def _explanation(
    task: Object,
    sources: list[Object],
    factors: dict[str, float],
    weights: Weights,
    store: SqliteGraphStore,
    now: datetime,
    identity: set[str],
) -> str:
    contributions = sorted(
        (
            (name, factors[name] * weights.priority.get(name, 0.0))
            for name in FACTOR_NAMES
            if factors[name] > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    phrases: list[str] = []
    for name, contribution in contributions:
        if contribution <= 0:
            continue
        phrase = _factor_phrase(name, task, sources, factors[name], store, now, identity)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) == 3:
            break
    if not phrases:
        return (
            "Low priority because no strong urgency, blocker, source or ownership signal "
            "was found."
        )
    if len(phrases) == 1:
        return f"Prioritised because {phrases[0]}."
    return "Prioritised because " + "; ".join(phrases[:-1]) + f"; and {phrases[-1]}."


def _factor_phrase(
    name: str,
    task: Object,
    sources: list[Object],
    value: float,
    store: SqliteGraphStore,
    now: datetime,
    identity: set[str],
) -> str | None:
    if name == "urgency":
        due = _parse_datetime(task.data.get("due_at"))
        if due is None:
            return "undated work still has a small baseline"
        days = int(abs((_aware(now) - due).total_seconds()) // 86_400)
        if due <= _aware(now):
            return f"overdue {days} day{'s' if days != 1 else ''}" if days else "due now"
        return f"due in {days} day{'s' if days != 1 else ''}" if days else "due soon"
    if name == "blocking":
        count = round(value * 5)
        if count:
            return f"{count} open item{'s' if count != 1 else ''} blocked on this"
    if name == "explicit_ask":
        if value >= 0.7:
            return "you were asked directly"
    if name == "owner_is_me":
        if identity:
            return "you own it"
    if name == "source_importance":
        return _source_phrase(sources)
    if name == "staleness":
        seen = _parse_datetime(task.data.get("last_seen_at")) or _parse_datetime(
            task.data.get("updated_at")
        )
        if seen is not None:
            days = int(max((_aware(now) - seen).total_seconds() / 86_400, 0.0))
            return f"it has been stale for {days} day{'s' if days != 1 else ''}"
    return None


def _source_phrase(sources: list[Object]) -> str:
    if not sources:
        return "it has a low source baseline"
    best = max(sources, key=_source_rule_score)
    data = best.data
    source_kind = str(data.get("source") or "source").lower()
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    if source_kind == "msx" and _money_value(data, raw) > 0:
        return "it is tied to a valued MSX opportunity"
    if source_kind == "github" and _review_requested(raw):
        return "a pull request is awaiting your review"
    if source_kind == "mail" and _direct_to_me(raw):
        return "a mail was addressed directly to you"
    return f"the backing {source_kind} source is important"


def _is_fyi(title: str, labels: set[str]) -> bool:
    return title.strip().lower().startswith("fyi") or "fyi" in labels


def _money_value(data: dict[str, Any], raw: dict[str, Any]) -> float:
    for key in ("estimated_value", "estimatedValue", "value", "amount", "revenue"):
        value = data.get(key, raw.get(key))
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _has_any(data: dict[str, Any], raw: dict[str, Any], *needles: str) -> bool:
    blob = " ".join([str(data), str(raw)]).lower()
    return any(needle.lower() in blob for needle in needles)


def _review_requested(raw: dict[str, Any]) -> bool:
    keys = (
        "reviewRequested",
        "review_requested",
        "isReviewRequested",
        "viewerCanReview",
    )
    if any(bool(raw.get(key)) for key in keys):
        return True
    reviewers = raw.get("reviewRequests") or raw.get("requestedReviewers")
    if isinstance(reviewers, list):
        return bool(reviewers)
    if isinstance(reviewers, dict):
        return bool(reviewers.get("nodes") or reviewers.get("totalCount"))
    return False


def _direct_to_me(raw: dict[str, Any]) -> bool:
    keys = (
        "directToMe",
        "isDirectToMe",
        "toMe",
        "addressedToMe",
        "assignedToMe",
    )
    return any(bool(raw.get(key)) for key in keys)


def _contains_ask(text: str) -> bool:
    return any(pattern.search(text) for pattern in DIRECT_ASK_PATTERNS)


def _is_open(task: Object) -> bool:
    return str(task.data.get("state") or "").lower() not in {str(s).lower() for s in CLOSED_STATES}


def _identity_matches(value: Any, identity: set[str]) -> bool:
    if value is None:
        return False
    return bool(build_identity(str(value)) & identity)


def _coerce_identity(identity: set[str] | Iterable[str] | None) -> set[str]:
    if identity is None:
        return set()
    if isinstance(identity, set):
        return build_identity(identity)
    return build_identity(identity)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _title(task: Object) -> str:
    return str(task.data.get("title") or "").lower()
