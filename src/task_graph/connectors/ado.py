"""Azure DevOps ingestion through Agency's ADO MCP server."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from task_graph.connectors.base import (
    ConnectorStatus,
    SourceItem,
    ado_workitem_uri,
    extract_external_refs,
    parse_datetime,
)
from task_graph.connectors.mcp_client import AgencyMcpClient, extract_json_from_mcp_result
from task_graph.ontology.types import SourceKind

#: Pin the organisation and projects to skip discovery, which costs an extra
#: round trip per sync and can otherwise fan out across every project the user
#: can see.
ENV_ADO_ORG = "TASK_GRAPH_ADO_ORG"
ENV_ADO_PROJECTS = "TASK_GRAPH_ADO_PROJECTS"

#: Upper bound on discovered projects to query. Without this, a user with
#: access to hundreds of projects would turn one sync into hundreds of calls.
MAX_DISCOVERED_PROJECTS = 10


def _env_projects() -> list[str]:
    raw = os.environ.get(ENV_ADO_PROJECTS, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


class AdoConnector:
    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        organization: str | None = None,
        projects: Iterable[str] | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda: AgencyMcpClient("ado"))
        self.organization = organization or os.environ.get(ENV_ADO_ORG) or None
        configured = projects if projects is not None else _env_projects()
        self.projects: list[str] = [p for p in (configured or []) if p]
        self.discovered_tools: list[str] = []

    @property
    def kind(self) -> SourceKind:
        return SourceKind.ADO

    @property
    def name(self) -> str:
        return "Azure DevOps"

    def is_available(self) -> ConnectorStatus:
        try:
            client = self._client_factory()
            if hasattr(client, "is_available"):
                return client.is_available()
            with client as opened:
                tools = opened.list_tools()
            return ConnectorStatus(True, f"Found {len(tools)} ADO MCP tools.")
        except Exception as exc:
            return ConnectorStatus(
                False,
                f"ADO MCP server is not available: {exc}",
                "Run `agency mcp ado` to inspect local Agency configuration.",
            )

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        with self._client_factory() as client:
            tools = _tool_names(client.list_tools())
            self.discovered_tools = tools
            records = self._fetch_records(client, tools)
        seen: set[str] = set()
        for record in records:
            item = self._map_work_item(record)
            if item.source_uri in seen:
                continue
            seen.add(item.source_uri)
            yield item

    def _fetch_records(self, client: Any, tools: list[str]) -> list[dict[str, Any]]:
        """Fetch work items assigned to the current user.

        The tool is named explicitly rather than matched fuzzily: name matching
        previously selected ``wit_work_item_write`` during a read-only sync,
        which is exactly the accident :func:`_assert_read_only` now prevents.

        ``wit_work_item`` requires both an organisation and a project. Large
        tenants expose hundreds of projects, so rather than fanning a sync out
        across arbitrary ones this asks the user to pin them once — and lists
        the real options in the error so the choice is easy.
        """
        if "wit_work_item" not in tools:
            raise RuntimeError(
                f"ADO MCP exposes no `wit_work_item` tool. Discovered: {sorted(tools)}"
            )
        _assert_read_only("wit_work_item")

        organization = self.organization or _detect_ado_org()
        if not organization:
            available = _discover_orgs(client, tools)
            raise RuntimeError(
                f"Set {ENV_ADO_ORG} to choose an Azure DevOps organization. "
                f"Available: {', '.join(available) if available else 'none discovered'}"
            )

        projects = self.projects
        if not projects:
            available = _discover_projects(client, tools, organization)
            raise RuntimeError(
                f"Set {ENV_ADO_PROJECTS} to a comma-separated list of projects in "
                f"{organization!r}. First few available: "
                f"{', '.join(available[:15]) if available else 'none discovered'}"
            )

        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for project in projects[:MAX_DISCOVERED_PROJECTS]:
            args = {
                "action": "my",
                "includeCompleted": False,
                "orgName": organization,
                "project": project,
            }
            try:
                payload = _normalise_payload(client.call_tool("wit_work_item", args))
            except Exception as exc:
                errors.append(f"{project}: {exc}")
                continue
            records.extend(_records(payload, "workItems", "items", "value", "result"))

        if not records and errors:
            raise RuntimeError(f"No ADO work items could be read. Tried: {errors}")
        return records

    def _map_work_item(self, record: dict[str, Any]) -> SourceItem:
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        work_item_id = record.get("id") or fields.get("System.Id")
        title = record.get("title") or fields.get("System.Title") or f"ADO work item {work_item_id}"
        state = record.get("state") or fields.get("System.State")
        board_column = (
            record.get("boardColumn")
            or fields.get("System.BoardColumn")
            or fields.get("Microsoft.VSTS.Common.BoardColumn")
        )
        raw_state = f"{state} / {board_column}" if state and board_column else state or board_column
        body = record.get("description") or fields.get("System.Description") or ""
        return SourceItem(
            source=SourceKind.ADO,
            source_uri=ado_workitem_uri(work_item_id),
            title=str(title),
            body=str(body),
            url=record.get("url") or record.get("_links", {}).get("html", {}).get("href"),
            source_state=str(raw_state) if raw_state else None,
            owner=_identity(record.get("assignedTo") or fields.get("System.AssignedTo")),
            assignees=_assignees(record, fields),
            created_at=parse_datetime(
                record.get("createdDate") or fields.get("System.CreatedDate")
            ),
            updated_at=parse_datetime(
                record.get("changedDate") or fields.get("System.ChangedDate")
            ),
            labels=_tags(record, fields),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=record,
        )


def _detect_ado_org() -> str | None:
    """Infer the organisation from the git remote, when there is one."""
    try:
        completed = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    remote = completed.stdout.strip()
    if not remote:
        return None
    for pattern in (r"dev\.azure\.com[:/]+([^/]+)/", r"([^/@:]+)@dev\.azure\.com"):
        match = re.search(pattern, remote, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


#: Keys an ADO ``core_list_*`` entry may use for its display name. Verified
#: against a live tenant, which returns ``orgName`` rather than ``name``.
_NAME_KEYS = ("name", "orgName", "projectName", "displayName", "id")


def _entry_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in _NAME_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _names_from_payload(payload: Any, *keys: str) -> list[str]:
    """Pull display names out of a ``core_list_*`` response."""
    data = _normalise_payload(payload)
    for key in (*keys, "result", "value", "items"):
        value = data.get(key)
        if isinstance(value, list):
            out = [name for name in (_entry_name(entry) for entry in value) if name]
            if out:
                return out
    return []


def _discover_orgs(client: Any, tools: list[str]) -> list[str]:
    if "core_list_orgs" not in tools:
        return []
    _assert_read_only("core_list_orgs")
    try:
        return _names_from_payload(
            client.call_tool("core_list_orgs", {}), "organizations", "orgs"
        )
    except Exception:
        return []


def _first_org(client: Any, tools: list[str]) -> str | None:
    orgs = _discover_orgs(client, tools)
    return orgs[0] if orgs else None


def _discover_projects(client: Any, tools: list[str], organization: str) -> list[str]:
    if "core_list_projects" not in tools:
        return []
    _assert_read_only("core_list_projects")
    try:
        payload = client.call_tool("core_list_projects", {"orgName": organization})
    except Exception:
        return []
    return _names_from_payload(payload, "projects")


def _tool_names(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, str):
            names.append(tool)
        elif isinstance(tool, dict) and tool.get("name"):
            names.append(str(tool["name"]))
        elif getattr(tool, "name", None):
            names.append(str(tool.name))
    return names


#: Name fragments that mark an MCP tool as mutating. Ingest is read-only, so
#: selecting one of these is always a bug — and a dangerous one, since it would
#: mean a routine sync writing to a source system.
_MUTATING_MARKERS = ("_write", "_upsert", "_create", "_delete", "_upload", "_remove")


def _assert_read_only(tool: str) -> None:
    lowered = tool.lower()
    if any(marker in lowered for marker in _MUTATING_MARKERS):
        raise RuntimeError(
            f"Refusing to call mutating tool {tool!r} during a read-only sync."
        )


def _pick_tool(tools: list[str], *, required: tuple[str, ...], preferred: tuple[str, ...]) -> str:
    candidates = [
        tool
        for tool in tools
        if all(part in tool.lower() for part in required)
        and not any(marker in tool.lower() for marker in _MUTATING_MARKERS)
    ]
    if not candidates:
        raise RuntimeError(
            f"No read-only MCP tool matched required terms {required}; found {tools}"
        )
    for term in preferred:
        for candidate in candidates:
            if term in candidate.lower():
                return candidate
    return candidates[0]


def _normalise_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and ("content" in payload or "structuredContent" in payload):
        parsed = extract_json_from_mcp_result(payload)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return payload if isinstance(payload, dict) else {"result": payload}


def _records(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if all(isinstance(value, dict) for value in payload.values()):
        return [value for value in payload.values() if isinstance(value, dict)]
    return []


def _identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName") or value.get("mailAddress")
    return str(value)


def _assignees(record: dict[str, Any], fields: dict[str, Any]) -> list[str]:
    identity = _identity(record.get("assignedTo") or fields.get("System.AssignedTo"))
    return [identity] if identity else []


def _tags(record: dict[str, Any], fields: dict[str, Any]) -> list[str]:
    tags = record.get("tags") or fields.get("System.Tags") or []
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(";") if tag.strip()]
    if isinstance(tags, list):
        return [str(tag) for tag in tags]
    return []
