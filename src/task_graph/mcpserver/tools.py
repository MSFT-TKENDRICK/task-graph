"""Plain Python tool bodies for the task-graph MCP server."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from task_graph.app import TaskGraphApp
from task_graph.ontology.models import PriorityBreakdown
from task_graph.ontology.types import CorrectionKind

JsonDict = dict[str, Any]
ToolHandler = Callable[..., JsonDict]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JsonDict
    handler: ToolHandler


def to_jsonable(value: Any) -> Any:
    """Convert activegraph, pydantic, enum and datetime values to JSON-native data."""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(to_jsonable(k)): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]

    if all(hasattr(value, attr) for attr in ("id", "type", "data")):
        return {
            "id": to_jsonable(value.id),
            "type": to_jsonable(value.type),
            "data": to_jsonable(value.data),
        }
    if all(hasattr(value, attr) for attr in ("id", "source", "target", "type")):
        return {
            "id": to_jsonable(value.id),
            "source": to_jsonable(value.source),
            "target": to_jsonable(value.target),
            "type": to_jsonable(value.type),
            "data": to_jsonable(getattr(value, "data", None)),
        }
    if all(hasattr(value, attr) for attr in ("id", "target", "value")):
        return {
            "id": to_jsonable(value.id),
            "target": to_jsonable(value.target),
            "op": to_jsonable(getattr(value, "op", None)),
            "value": to_jsonable(value.value),
            "rationale": to_jsonable(getattr(value, "rationale", None)),
            "evidence": to_jsonable(getattr(value, "evidence", None)),
        }
    return str(value)


def ok(**payload: Any) -> JsonDict:
    return {"ok": True, **to_jsonable(payload)}


def error(message: str, *, error_type: str = "tool_error", **details: Any) -> JsonDict:
    return {
        "ok": False,
        "error": {"type": error_type, "message": message, **to_jsonable(details)},
    }


def _guard(fn: Callable[[], JsonDict]) -> JsonDict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - protocol boundary returns structured errors
        return error(str(exc), error_type=type(exc).__name__)


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    return datetime.fromisoformat(since.replace("Z", "+00:00"))


def sync_sources(
    app: TaskGraphApp,
    sources: list[str] | None = None,
    since: str | None = None,
    propose: bool = False,
) -> JsonDict:
    """Pull source systems into the graph, deduplicate, rank, and optionally propose actions."""

    def run() -> JsonDict:
        report = app.sync(sources=sources, since=_parse_since(since), propose=propose)
        return ok(report=report, summary=report.summary())

    return _guard(run)


def list_tasks(app: TaskGraphApp, limit: int = 20, state: str | None = None) -> JsonDict:
    """Return ranked open work with priority score, factors, and prose explanation."""

    def run() -> JsonDict:
        rows = app.triage(limit=max(limit, 1))
        tasks = []
        for task, priority in rows:
            if state and str(task.data.get("state")) != state:
                continue
            tasks.append(
                {
                    "task": to_jsonable(task),
                    "priority": to_jsonable(priority),
                    "score": priority.score,
                    "explanation": priority.explanation,
                }
            )
        return ok(tasks=tasks, count=len(tasks))

    return _guard(run)


def get_task(app: TaskGraphApp, task_id: str) -> JsonDict:
    """Return task detail including backing source records and source URLs."""

    def run() -> JsonDict:
        task = app.get_task(task_id)
        if task is None:
            return error(f"Task not found: {task_id}", error_type="not_found", task_id=task_id)
        return ok(task=task)

    return _guard(run)


def search_tasks(app: TaskGraphApp, query: str, limit: int = 20) -> JsonDict:
    """Search tasks using the project's hybrid lexical and semantic index."""

    return _guard(lambda: ok(results=app.search_tasks(query, limit=max(limit, 1))))


def get_task_graph(app: TaskGraphApp, task_id: str, depth: int = 2) -> JsonDict:
    """Return the local task graph neighbourhood as nodes and edges."""

    def run() -> JsonDict:
        graph = app.task_graph(task_id, depth=max(depth, 0))
        if not graph.get("objects"):
            return error(
                f"Task graph is empty or task not found: {task_id}",
                error_type="not_found",
            )
        return ok(nodes=graph["objects"], edges=graph["relations"])

    return _guard(run)


def explain_priority(app: TaskGraphApp, task_id: str) -> JsonDict:
    """Explain the per-factor priority score and provide a prose rationale."""

    def run() -> JsonDict:
        breakdown = app.explain_priority(task_id)
        return ok(priority=breakdown)

    return _guard(run)


def list_pending_merges(app: TaskGraphApp) -> JsonDict:
    """List proposed duplicate merges awaiting user approval or rejection."""

    return _guard(lambda: ok(merges=app.pending_merges(), count=len(app.pending_merges())))


def approve_merge(app: TaskGraphApp, patch_id: str) -> JsonDict:
    """Approve a duplicate-merge patch only; this records the user's merge decision."""

    return _guard(lambda: ok(canonical_task_id=app.approve_merge(patch_id, actor="mcp-user")))


def reject_merge(app: TaskGraphApp, patch_id: str, reason: str) -> JsonDict:
    """Reject a proposed duplicate merge and record the reason as a correction."""

    def run() -> JsonDict:
        app.reject_merge(patch_id, reason, actor="mcp-user")
        return ok(rejected_patch_id=patch_id)

    return _guard(run)


def propose_remediations(app: TaskGraphApp, task_id: str) -> JsonDict:
    """Generate pending remediation proposals for a task without touching source systems."""

    return _guard(lambda: ok(remediations=app.propose_for(task_id)))


def list_pending_approvals(app: TaskGraphApp) -> JsonDict:
    """List proposed actions requiring explicit approval, including rendered previews."""

    def run() -> JsonDict:
        approvals = []
        for remediation in app.pending_approvals():
            item = to_jsonable(remediation)
            item["preview"] = (
                remediation.data.get("preview") or app.approvals.dry_run(remediation.id)
            )
            approvals.append(item)
        return ok(approvals=approvals, count=len(approvals))

    return _guard(run)


def preview_action(app: TaskGraphApp, remediation_id: str) -> JsonDict:
    """Dry-run a proposed action and render its preview; this never executes anything."""

    return _guard(
        lambda: ok(remediation_id=remediation_id, preview=app.approvals.dry_run(remediation_id))
    )


def approve_action(app: TaskGraphApp, remediation_id: str) -> JsonDict:
    """Grant approval only for a proposed action; this explicitly does not execute it."""

    return _guard(
        lambda: ok(remediation=app.approvals.grant(remediation_id, approved_by="mcp-user"))
    )


def execute_action(app: TaskGraphApp, remediation_id: str) -> JsonDict:
    """Execute an already-granted action; fails loudly unless prior explicit approval exists."""

    return _guard(
        lambda: ok(remediation=app.approvals.execute_approved(remediation_id, actor="mcp-user"))
    )


def record_correction(
    app: TaskGraphApp,
    task_id: str,
    kind: str,
    reason: str,
    direction: float | None = None,
) -> JsonDict:
    """Capture a user correction about task priority, dedupe, remediation, or task validity."""

    def run() -> JsonDict:
        task = app.store.get_object(task_id)
        if task is None:
            return error(f"Task not found: {task_id}", error_type="not_found", task_id=task_id)
        correction_kind = CorrectionKind(kind)
        if correction_kind == CorrectionKind.PRIORITY:
            breakdown: PriorityBreakdown = app.explain_priority(task_id)
            correction = app.corrector.record(
                CorrectionKind.PRIORITY,
                [task_id],
                before={"factors": breakdown.factors, "score": breakdown.score},
                after={"direction": 1.0 if direction is None else direction},
                rationale=reason,
                actor="mcp-user",
            )
        elif correction_kind == CorrectionKind.NOT_A_TASK:
            correction = app.corrector.not_a_task(task, rationale=reason, actor="mcp-user")
        else:
            correction = app.corrector.record(
                correction_kind, [task_id], rationale=reason, actor="mcp-user"
            )
        return ok(correction=correction)

    return _guard(run)


def learn_from_corrections(app: TaskGraphApp) -> JsonDict:
    """Fold recorded corrections into learned weights and report the movements."""

    def run() -> JsonDict:
        report = app.learn()
        return ok(report=report, summary=report.summary())

    return _guard(run)


def get_status(app: TaskGraphApp) -> JsonDict:
    """Return graph health, counts, pending approvals, and embedding status."""

    return _guard(lambda: ok(status=app.status()))


def run_doctor(app: TaskGraphApp) -> JsonDict:
    """Run preflight checks for configured connectors and local dependencies."""

    return _guard(lambda: ok(checks=app.preflight()))


def _schema(properties: JsonDict | None = None, required: list[str] | None = None) -> JsonDict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "sync_sources",
        (
            "Pull configured source systems into the local graph, deduplicate, rank, "
            "and optionally propose read-only remediation drafts. Does not execute "
            "source-system actions."
        ),
        _schema(
            {
                "sources": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string", "description": "ISO-8601 timestamp"},
                "propose": {"type": "boolean", "default": False},
            }
        ),
        sync_sources,
    ),
    ToolSpec(
        "list_tasks",
        "List ranked open work with priority factors and explanations; use this for triage.",
        _schema(
            {
                "limit": {"type": "integer", "minimum": 1, "default": 20},
                "state": {"type": "string"},
            }
        ),
        list_tasks,
    ),
    ToolSpec(
        "get_task",
        "Get one task with its backing source items and URLs.",
        _schema({"task_id": {"type": "string"}}, ["task_id"]),
        get_task,
    ),
    ToolSpec(
        "search_tasks",
        "Hybrid lexical plus semantic search over tasks.",
        _schema(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "default": 20},
            },
            ["query"],
        ),
        search_tasks,
    ),
    ToolSpec(
        "get_task_graph",
        "Get a task neighbourhood as graph nodes and edges for dependency/context inspection.",
        _schema(
            {
                "task_id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 0, "default": 2},
            },
            ["task_id"],
        ),
        get_task_graph,
    ),
    ToolSpec(
        "explain_priority",
        "Explain a task priority score with per-factor values and prose rationale.",
        _schema({"task_id": {"type": "string"}}, ["task_id"]),
        explain_priority,
    ),
    ToolSpec(
        "list_pending_merges",
        "List proposed duplicate-task merges with scores and rationale; does not apply them.",
        _schema(),
        list_pending_merges,
    ),
    ToolSpec(
        "approve_merge",
        "Approve a pending duplicate merge patch and record that merge decision.",
        _schema({"patch_id": {"type": "string"}}, ["patch_id"]),
        approve_merge,
    ),
    ToolSpec(
        "reject_merge",
        "Reject a pending duplicate merge patch and record the reason as a correction.",
        _schema(
            {"patch_id": {"type": "string"}, "reason": {"type": "string"}},
            ["patch_id", "reason"],
        ),
        reject_merge,
    ),
    ToolSpec(
        "propose_remediations",
        (
            "Generate remediation proposals for a task. Read-only with respect to "
            "source systems: proposals require separate approval and execution tools."
        ),
        _schema({"task_id": {"type": "string"}}, ["task_id"]),
        propose_remediations,
    ),
    ToolSpec(
        "list_pending_approvals",
        (
            "List proposed actions awaiting explicit user approval, including rendered "
            "previews. Nothing is executed."
        ),
        _schema(),
        list_pending_approvals,
    ),
    ToolSpec(
        "preview_action",
        "Dry-run/render a proposed action preview. This never executes source-system changes.",
        _schema({"remediation_id": {"type": "string"}}, ["remediation_id"]),
        preview_action,
    ),
    ToolSpec(
        "approve_action",
        (
            "Grant approval only for a proposed action; this explicitly does not "
            "execute it. Call execute_action separately after approval."
        ),
        _schema({"remediation_id": {"type": "string"}}, ["remediation_id"]),
        approve_action,
    ),
    ToolSpec(
        "execute_action",
        (
            "Execute an already-granted action against its source system. Requires a "
            "prior explicit grant from approve_action and fails if not granted."
        ),
        _schema({"remediation_id": {"type": "string"}}, ["remediation_id"]),
        execute_action,
    ),
    ToolSpec(
        "record_correction",
        (
            "Record a user correction for a task. For priority corrections, direction "
            "> 0 raises and < 0 lowers future ranking weight."
        ),
        _schema(
            {
                "task_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [kind.value for kind in CorrectionKind],
                },
                "reason": {"type": "string"},
                "direction": {"type": "number"},
            },
            ["task_id", "kind", "reason"],
        ),
        record_correction,
    ),
    ToolSpec(
        "learn_from_corrections",
        "Fold recorded corrections into learned weights and report what moved.",
        _schema(),
        learn_from_corrections,
    ),
    ToolSpec(
        "get_status",
        "Get local graph status, counts, pending decisions, and embedding configuration.",
        _schema(),
        get_status,
    ),
    ToolSpec(
        "run_doctor",
        "Run preflight checks for source connectors, local dependencies, and embeddings.",
        _schema(),
        run_doctor,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
