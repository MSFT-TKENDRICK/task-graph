"""MSX ingestion through the read-only MCP surface, when it is locally available."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from task_graph.connectors.ado import _assert_read_only, _normalise_payload, _records, _tool_names
from task_graph.connectors.base import (
    ConnectorStatus,
    SourceItem,
    extract_external_refs,
    parse_datetime,
)
from task_graph.connectors.mcp_client import AgencyMcpClient
from task_graph.ontology.types import SourceKind

ENV_MSX_MCP_SERVER = "TASK_GRAPH_MSX_MCP_SERVER"
DEFAULT_MSX_MCP_SERVER = "msx"


def msx_opportunity_uri(record_id: str | int) -> str:
    return f"msx:opportunity:{str(record_id).strip()}"


def msx_milestone_uri(record_id: str | int) -> str:
    return f"msx:milestone:{str(record_id).strip()}"


def msx_activity_uri(record_id: str | int) -> str:
    return f"msx:activity:{str(record_id).strip()}"


class MsxConnector:
    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        server_name: str | None = None,
    ) -> None:
        self.server_name = (
            server_name or os.environ.get(ENV_MSX_MCP_SERVER) or DEFAULT_MSX_MCP_SERVER
        )
        self._client_factory = client_factory or (lambda: AgencyMcpClient(self.server_name))
        self.discovered_tools: list[str] = []

    @property
    def kind(self) -> SourceKind:
        return SourceKind.MSX

    @property
    def name(self) -> str:
        return "MSX"

    def is_available(self) -> ConnectorStatus:
        try:
            with self._client_factory() as client:
                tools = _tool_names(client.list_tools())
        except Exception as exc:  # noqa: BLE001 - availability must never raise
            return _unavailable_from_exception(self.server_name, exc)
        self.discovered_tools = tools
        read_tools = _supported_read_tools(tools)
        if read_tools:
            return ConnectorStatus(True, f"Found MSX MCP tools: {', '.join(read_tools)}.")
        login_tools = [tool for tool in tools if "login" in tool.lower()]
        if login_tools:
            found = ", ".join(sorted(tools))
            return ConnectorStatus(
                False,
                f"MSX MCP is installed but exposes no read tools. Found: {found}.",
                "Run the MSX login flow (likely `msx_login`) with your @microsoft.com account, "
                "ensure corporate VPN/SSE is connected, then retry.",
            )
        found = ", ".join(sorted(tools)) or "none"
        return ConnectorStatus(
            False,
            f"MSX MCP exposed no supported read tools. Found: {found}.",
            "Install/enable the `msx-mcp` Copilot plugin or point "
            f"{ENV_MSX_MCP_SERVER} at the configured MCP server.",
        )

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        with self._client_factory() as client:
            tools = _tool_names(client.list_tools())
            self.discovered_tools = tools
            records = self._fetch_records(client, tools, since)
        seen: set[str] = set()
        for item in (
            *(self._map_opportunity(record) for record in records["opportunities"]),
            *(self._map_milestone(record) for record in records["milestones"]),
            *(self._map_activity(record) for record in records["activities"]),
        ):
            if item.source_uri in seen:
                continue
            seen.add(item.source_uri)
            yield item

    def _fetch_records(
        self, client: Any, tools: list[str], since: datetime | None
    ) -> dict[str, list[dict[str, Any]]]:
        records: dict[str, list[dict[str, Any]]] = {
            "opportunities": [],
            "milestones": [],
            "activities": [],
        }
        records["opportunities"] = _call_records(
            client,
            tools,
            preferred=("get_my_deals",),
            fallbacks=("search_opportunities", "dataverse_query"),
            args=_query_args("opportunities", since),
            keys=("deals", "opportunities", "records", "items", "value", "result"),
        )
        records["milestones"] = _call_records(
            client,
            tools,
            preferred=("get_my_milestones",),
            fallbacks=("dataverse_query",),
            args=_query_args("milestones", since),
            keys=("milestones", "records", "items", "value", "result"),
        )
        records["activities"] = _call_records(
            client,
            tools,
            preferred=("get_my_hok_activities",),
            fallbacks=("dataverse_query",),
            args=_query_args("activities", since),
            keys=("activities", "hokActivities", "tasks", "records", "items", "value", "result"),
        )
        if not _supported_read_tools(tools):
            raise RuntimeError(
                "MSX MCP exposes no supported read path. "
                f"Discovered tools: {', '.join(sorted(tools)) or 'none'}"
            )
        return records

    def _map_opportunity(self, record: dict[str, Any]) -> SourceItem:
        record_id = _first(record, "opportunityid", "opportunityId", "msxId", "id")
        title = _text(
            _first(record, "name", "topic", "opportunityName", "title"),
            "MSX opportunity",
        )
        body = _body(
            record,
            "description",
            "summary",
            value=_first(record, "estimatedValue", "estimatedRevenue", "estimatedvalue", "revenue"),
            date=_first(record, "closeDate", "estimatedCloseDate", "estimatedclosedate", "dueDate"),
        )
        return SourceItem(
            source=SourceKind.MSX,
            source_uri=msx_opportunity_uri(_required_id(record_id, title)),
            title=title,
            body=body,
            url=_string(_first(record, "url", "webUrl", "msxUrl")),
            source_state=_string(
                _first(
                    record,
                    "salesStage",
                    "stage",
                    "stageName",
                    "status",
                    "state",
                    "statusReason",
                )
            ),
            owner=_identity(_first(record, "owner", "ownerid", "seller", "primarySeller")),
            assignees=_people(_first(record, "dealTeam", "teamMembers", "salesTeam")),
            created_at=parse_datetime(
                _first(record, "createdOn", "createdDate", "createdDateTime")
            ),
            updated_at=parse_datetime(
                _first(record, "modifiedOn", "updatedDate", "lastModifiedDateTime")
            ),
            due_at=parse_datetime(
                _first(record, "closeDate", "estimatedCloseDate", "estimatedclosedate", "dueDate")
            ),
            labels=_labels(record, "opportunity"),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=record,
        )

    def _map_milestone(self, record: dict[str, Any]) -> SourceItem:
        record_id = _first(record, "milestoneid", "milestoneId", "msxId", "id")
        title = _text(
            _first(record, "name", "title", "milestoneName", "subject"),
            "MSX milestone",
        )
        body = _body(record, "description", "summary", "notes")
        return SourceItem(
            source=SourceKind.MSX,
            source_uri=msx_milestone_uri(_required_id(record_id, title)),
            title=title,
            body=body,
            url=_string(_first(record, "url", "webUrl", "msxUrl")),
            source_state=_string(
                _first(record, "status", "state", "milestoneStatus", "statusReason")
            ),
            owner=_identity(_first(record, "owner", "ownerid", "seller")),
            assignees=_people(_first(record, "assignedTo", "owners", "dealTeam", "teamMembers")),
            created_at=parse_datetime(
                _first(record, "createdOn", "createdDate", "createdDateTime")
            ),
            updated_at=parse_datetime(
                _first(record, "modifiedOn", "updatedDate", "lastModifiedDateTime")
            ),
            due_at=parse_datetime(_first(record, "dueDate", "targetDate", "milestoneDate")),
            labels=_labels(record, "milestone"),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=record,
        )

    def _map_activity(self, record: dict[str, Any]) -> SourceItem:
        record_id = _first(record, "activityid", "activityId", "taskId", "msxId", "id")
        title = _text(
            _first(record, "subject", "title", "name", "activityName"),
            "MSX activity",
        )
        body = _body(record, "description", "summary", "notes", "body")
        return SourceItem(
            source=SourceKind.MSX,
            source_uri=msx_activity_uri(_required_id(record_id, title)),
            title=title,
            body=body,
            url=_string(_first(record, "url", "webUrl", "msxUrl")),
            source_state=_string(
                _first(record, "status", "state", "activityStatus", "statusReason")
            ),
            owner=_identity(_first(record, "owner", "ownerid", "seller")),
            assignees=_people(_first(record, "assignedTo", "owners", "participants", "dealTeam")),
            created_at=parse_datetime(
                _first(record, "createdOn", "createdDate", "createdDateTime")
            ),
            updated_at=parse_datetime(
                _first(record, "modifiedOn", "updatedDate", "lastModifiedDateTime")
            ),
            due_at=parse_datetime(
                _first(record, "dueDate", "scheduledEnd", "endDate", "targetDate")
            ),
            labels=_labels(record, "activity"),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=record,
        )


def _call_records(
    client: Any,
    tools: list[str],
    *,
    preferred: tuple[str, ...],
    fallbacks: tuple[str, ...],
    args: dict[str, Any],
    keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    tool = _pick_explicit_tool(tools, (*preferred, *fallbacks))
    if tool is None:
        return []
    _assert_read_only(tool)
    call_args = args if tool == "dataverse_query" else args["params"]
    payload = _normalise_payload(client.call_tool(tool, call_args))
    return _records(payload, *keys)


def _pick_explicit_tool(tools: list[str], candidates: tuple[str, ...]) -> str | None:
    safe_tools = [tool for tool in tools if _is_safe_read_tool(tool)]
    for candidate in candidates:
        if candidate in safe_tools:
            return candidate
    return None


def _is_safe_read_tool(tool: str) -> bool:
    try:
        _assert_read_only(tool)
    except RuntimeError:
        return False
    lowered = tool.lower()
    return not any(term in lowered for term in ("login", "write", "create", "update", "delete"))


def _supported_read_tools(tools: list[str]) -> list[str]:
    return [
        tool
        for tool in tools
        if tool
        in {
            "get_my_deals",
            "search_opportunities",
            "get_my_milestones",
            "get_my_hok_activities",
            "dataverse_query",
        }
        and _is_safe_read_tool(tool)
    ]


def _query_args(kind: str, since: datetime | None) -> dict[str, Any]:
    params: dict[str, Any] = {"top": 100}
    if since:
        params["since"] = since.isoformat()
    query = f"read my MSX {kind}"
    return {"params": params, "query": query, "entity": kind}


def _first(record: dict[str, Any], *keys: str) -> Any:
    lowered = {key.lower(): key for key in record}
    for key in keys:
        actual = lowered.get(key.lower())
        if actual is not None and record.get(actual) not in (None, ""):
            return record[actual]
    return None


def _required_id(value: Any, title: str) -> str:
    if value in (None, ""):
        raise RuntimeError(f"MSX record {title!r} did not include a stable id.")
    return str(value)


def _body(record: dict[str, Any], *text_keys: str, value: Any = None, date: Any = None) -> str:
    parts = [_string(_first(record, *text_keys)) or ""]
    account = _string(_first(record, "accountName", "customerName", "account", "customer"))
    if account:
        parts.append(f"Account: {account}")
    if value not in (None, ""):
        parts.append(f"Estimated value: {value}")
    if date not in (None, ""):
        parts.append(f"Close date: {date}")
    return "\n".join(part for part in parts if part)


def _labels(record: dict[str, Any], kind: str) -> list[str]:
    labels = [f"msx:{kind}"]
    value = _first(record, "estimatedValue", "estimatedRevenue", "estimatedvalue", "revenue")
    if value not in (None, ""):
        labels.append(f"value:{value}")
    account = _string(_first(record, "accountName", "customerName"))
    if account:
        labels.append(f"account:{account}")
    return labels


def _people(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [person for person in (_identity(item) for item in value) if person]
    person = _identity(value)
    return [person] if person else []


def _identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("displayName", "name", "fullName", "email", "mail", "upn", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return str(value)


def _text(value: Any, fallback: str) -> str:
    return _string(value) or fallback


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _unavailable_from_exception(server_name: str, exc: Exception) -> ConnectorStatus:
    message = str(exc)
    lowered = message.lower()
    if (
        "unrecognized subcommand" in lowered
        or "not found" in lowered
        or "connection closed" in lowered
    ):
        return ConnectorStatus(
            False,
            f"MSX MCP server `{server_name}` is not installed or not registered: {message}",
            "Install/enable the `msx-mcp` Copilot plugin, or configure an Agency MCP server named "
            f"`{server_name}`. If it is registered under another name, set {ENV_MSX_MCP_SERVER}.",
        )
    if any(term in lowered for term in ("login", "auth", "unauthorized", "forbidden", "consent")):
        return ConnectorStatus(
            False,
            f"MSX MCP is installed but not authenticated: {message}",
            "Run `msx_login` with your @microsoft.com account, then retry.",
        )
    if any(term in lowered for term in ("vpn", "sse", "crm.dynamics", "timeout", "network")):
        return ConnectorStatus(
            False,
            f"MSX MCP could not reach Dynamics/Dataverse: {message}",
            "Connect to Microsoft corporate VPN/SSE and verify access to "
            "https://microsoftsales.crm.dynamics.com.",
        )
    return ConnectorStatus(
        False,
        f"MSX MCP server `{server_name}` is not available: {message}",
        "Install `msx-mcp`, run `msx_login`, and connect to corporate VPN/SSE before retrying.",
    )
