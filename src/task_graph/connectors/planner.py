"""Microsoft Planner ingestion through Agency's ambient-auth MCP server."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from task_graph.connectors.ado import _assert_read_only, _normalise_payload, _tool_names
from task_graph.connectors.base import (
    ConnectorStatus,
    SourceItem,
    extract_external_refs,
    parse_datetime,
)
from task_graph.connectors.mcp_client import AgencyMcpClient
from task_graph.ontology.types import SourceKind

ENV_PLANNER_PLANS = "TASK_GRAPH_PLANNER_PLANS"
ENV_PLANNER_ASSIGNED_TO_USER = "TASK_GRAPH_PLANNER_ASSIGNED_TO_USER"
MAX_PLANS = 20
MAX_PAGES_PER_PLAN = 25


def planner_uri(task_id: int | str) -> str:
    return f"planner:task:{str(task_id).strip()}"


def _env_plans() -> list[str]:
    raw = os.environ.get(ENV_PLANNER_PLANS, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


class PlannerConnector:
    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        plans: Iterable[str] | None = None,
        assigned_to_user_id: str | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda: AgencyMcpClient("planner"))
        configured = plans if plans is not None else _env_plans()
        self.plans: list[str] = [plan for plan in configured if plan]
        self.assigned_to_user_id = (
            assigned_to_user_id or os.environ.get(ENV_PLANNER_ASSIGNED_TO_USER) or None
        )
        self.discovered_tools: list[str] = []

    @property
    def kind(self) -> SourceKind:
        return SourceKind.PLANNER

    @property
    def name(self) -> str:
        return "Microsoft Planner"

    def is_available(self) -> ConnectorStatus:
        try:
            client = self._client_factory()
            if hasattr(client, "is_available"):
                return client.is_available()
            with client as opened:
                tools = opened.list_tools()
            return ConnectorStatus(True, f"Found {len(tools)} Planner MCP tools.")
        except Exception as exc:
            return ConnectorStatus(
                False,
                f"Planner MCP server is not available: {exc}",
                "Run `agency mcp planner` to inspect local Agency configuration.",
            )

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        del since  # Planner's read tool has no reliable last-modified filter.
        with self._client_factory() as client:
            tools = _tool_names(client.list_tools())
            self.discovered_tools = tools
            records = self._fetch_records(client, tools)
        seen: set[str] = set()
        for record in records:
            item = self._map_task(record)
            if item.source_uri in seen:
                continue
            seen.add(item.source_uri)
            yield item

    def _fetch_records(self, client: Any, tools: list[str]) -> list[dict[str, Any]]:
        if "QueryTasksInPlan" not in tools:
            raise RuntimeError(
                f"Planner MCP exposes no `QueryTasksInPlan` tool. Discovered: {sorted(tools)}"
            )

        plans = self.plans
        if not plans:
            available = _discover_plans(client, tools)
            raise RuntimeError(
                f"Set {ENV_PLANNER_PLANS} to a comma-separated list of Planner plan IDs. "
                f"Available: {_format_plan_options(available)}"
            )

        records: list[dict[str, Any]] = []
        for plan_id in plans[:MAX_PLANS]:
            plan = _get_plan(client, tools, plan_id)
            args: dict[str, Any] = {"planId": plan_id}
            if self.assigned_to_user_id:
                args["assignedToUserId"] = self.assigned_to_user_id
            for page in range(MAX_PAGES_PER_PLAN):
                payload = _normalise_payload(_call_read_tool(client, "QueryTasksInPlan", args))
                for record in _task_records(payload):
                    merged = _merge_context(record, plan_id, plan)
                    if "GetTask" in tools and merged.get("id"):
                        detail = _normalise_payload(
                            _call_read_tool(client, "GetTask", {"taskId": str(merged["id"])})
                        )
                        merged = _merge_context(_first_record(detail) | merged, plan_id, plan)
                    records.append(merged)
                skip_token = _skip_token(payload)
                if not skip_token:
                    break
                args["skipToken"] = skip_token
                if page == MAX_PAGES_PER_PLAN - 1:
                    raise RuntimeError(f"Planner plan {plan_id!r} returned too many pages.")
        return records

    def _map_task(self, record: dict[str, Any]) -> SourceItem:
        task_id = record.get("id") or record.get("taskId")
        title = str(record.get("title") or record.get("displayName") or f"Planner task {task_id}")
        body = _body(record)
        context = _context_lines(record)
        body_with_context = "\n".join([*context, body]).strip()
        assignees = _assignees(record)
        owner = assignees[0] if assignees else _identity(
            record.get("createdBy") or record.get("owner")
        )
        return SourceItem(
            source=SourceKind.PLANNER,
            source_uri=planner_uri(str(task_id)),
            title=title,
            body=body_with_context,
            url=record.get("webUrl") or record.get("url"),
            source_state=_state_from_percent(record.get("percentComplete"), record.get("status")),
            owner=owner,
            assignees=assignees,
            created_at=parse_datetime(record.get("createdDateTime")),
            updated_at=parse_datetime(
                record.get("lastModifiedDateTime") or record.get("updatedDateTime")
            ),
            due_at=parse_datetime(record.get("dueDateTime")),
            labels=_labels(record),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=record,
        )


def _call_read_tool(client: Any, tool: str, args: dict[str, Any]) -> Any:
    _assert_read_only(tool)
    return client.call_tool(tool, args)


def _discover_plans(client: Any, tools: list[str]) -> list[dict[str, Any]]:
    if "QueryPlans" not in tools:
        return []
    try:
        payload = _normalise_payload(_call_read_tool(client, "QueryPlans", {}))
    except Exception:
        return []
    return _plan_records(payload)


def _get_plan(client: Any, tools: list[str], plan_id: str) -> dict[str, Any]:
    if "GetPlan" not in tools:
        return {"id": plan_id}
    try:
        payload = _normalise_payload(_call_read_tool(client, "GetPlan", {"planId": plan_id}))
    except Exception:
        return {"id": plan_id}
    plan = _first_record(payload)
    return plan if plan else {"id": plan_id}


def _plan_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _records_from_payload(payload, "plans", "items", "value", "result")


def _task_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _records_from_payload(payload, "tasks", "items", "value", "result")


def _records_from_payload(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _records_from_payload(value, *keys[:-1])
            if nested:
                return nested
    if all(isinstance(value, dict) for value in payload.values()):
        return [value for value in payload.values() if isinstance(value, dict)]
    return []


def _first_record(payload: dict[str, Any]) -> dict[str, Any]:
    records = _records_from_payload(payload, "task", "plan", "result")
    return records[0] if records else payload


def _skip_token(payload: dict[str, Any]) -> str | None:
    value = payload.get("skipToken")
    if isinstance(value, str) and value:
        return value
    result = payload.get("result")
    if isinstance(result, dict):
        token = result.get("skipToken")
        if isinstance(token, str) and token:
            return token
    return None


def _format_plan_options(plans: list[dict[str, Any]]) -> str:
    if not plans:
        return "none discovered"
    options: list[str] = []
    for plan in plans[:15]:
        plan_id = _plan_id(plan) or "<missing id>"
        title = _plan_title(plan) or "<untitled>"
        options.append(f"{title} ({plan_id})")
    return ", ".join(options)


def _merge_context(
    record: dict[str, Any], plan_id: str, plan: dict[str, Any] | None
) -> dict[str, Any]:
    merged = dict(record)
    merged.setdefault("planId", plan_id)
    if plan:
        merged.setdefault("planTitle", _plan_title(plan))
        merged.setdefault("plan", plan)
    return merged


def _plan_id(plan: dict[str, Any]) -> str | None:
    for key in ("id", "planId"):
        value = plan.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _plan_title(plan: dict[str, Any]) -> str | None:
    for key in ("title", "displayName", "name"):
        value = plan.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _body(record: dict[str, Any]) -> str:
    for key in ("notes", "description", "body", "details"):
        value = record.get(key)
        if isinstance(value, dict):
            text = value.get("content") or value.get("text") or value.get("description")
            if text:
                return str(text)
        if value:
            return str(value)
    return ""


def _context_lines(record: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    plan = record.get("planTitle") or record.get("planId")
    bucket = _bucket_name(record)
    percent = record.get("percentComplete")
    if plan:
        lines.append(f"Plan: {plan}")
    if bucket:
        lines.append(f"Bucket: {bucket}")
    if percent is not None:
        lines.append(f"Percent complete: {percent}")
    return lines


def _bucket_name(record: dict[str, Any]) -> str | None:
    bucket = record.get("bucket")
    if isinstance(bucket, dict):
        for key in ("name", "title", "displayName", "id", "bucketId"):
            value = bucket.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("bucketName", "bucketTitle", "bucketId"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _labels(record: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for prefix, value in (
        ("plan", record.get("planTitle") or record.get("planId")),
        ("bucket", _bucket_name(record)),
        ("priority", record.get("priority")),
    ):
        if value:
            labels.append(f"{prefix}:{value}")
    categories = record.get("categories") or record.get("labels")
    if isinstance(categories, list):
        labels.extend(str(category) for category in categories if category)
    return labels


def _state_from_percent(percent_complete: Any, status: Any = None) -> str:
    if percent_complete is None and isinstance(status, str) and status:
        if status in {"notStarted", "inProgress", "completed"}:
            return status
        return status.strip()
    try:
        percent = int(float(str(percent_complete)))
    except (TypeError, ValueError):
        return "notStarted"
    if percent <= 0:
        return "notStarted"
    if percent >= 100:
        return "completed"
    return "inProgress"


def _assignees(record: dict[str, Any]) -> list[str]:
    assignments = record.get("assignments") or record.get("assignees") or record.get("assignedTo")
    out: list[str] = []
    if isinstance(assignments, dict):
        for key, value in assignments.items():
            identity = _identity(value) or (str(key) if key else None)
            if identity and identity not in out:
                out.append(identity)
    elif isinstance(assignments, list):
        for value in assignments:
            identity = _identity(value)
            if identity and identity not in out:
                out.append(identity)
    else:
        identity = _identity(assignments)
        if identity:
            out.append(identity)
    return out


def _identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("displayName", "name", "email", "mail", "userPrincipalName", "id", "userId"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return text
        user = value.get("user") or value.get("assignedTo")
        if isinstance(user, dict):
            return _identity(user)
    return str(value)
