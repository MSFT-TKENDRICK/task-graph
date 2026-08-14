"""Calendar ingestion through Agency's ambient Microsoft 365 auth."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
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

ENV_CALENDAR_LOOKAHEAD_DAYS = "TASK_GRAPH_CALENDAR_LOOKAHEAD_DAYS"
ENV_CALENDAR_TOP = "TASK_GRAPH_CALENDAR_TOP"
DEFAULT_LOOKAHEAD_DAYS = 14
DEFAULT_TOP = 150
CALENDAR_VIEW_TOOL = "ListCalendarView"


class CalendarConnector:
    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        lookahead_days: int | None = None,
        top: int | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda: AgencyMcpClient("calendar"))
        self.lookahead_days = lookahead_days if lookahead_days is not None else _env_int(
            ENV_CALENDAR_LOOKAHEAD_DAYS, DEFAULT_LOOKAHEAD_DAYS
        )
        self.top = top if top is not None else _env_int(ENV_CALENDAR_TOP, DEFAULT_TOP)
        self.discovered_tools: list[str] = []

    @property
    def kind(self) -> SourceKind:
        return SourceKind.CALENDAR

    @property
    def name(self) -> str:
        return "Calendar"

    def is_available(self) -> ConnectorStatus:
        try:
            client = self._client_factory()
            if hasattr(client, "is_available"):
                return client.is_available()
            with client as opened:
                tools = opened.list_tools()
            return ConnectorStatus(True, f"Found {len(tools)} calendar MCP tools.")
        except Exception as exc:
            return ConnectorStatus(
                False,
                f"Calendar MCP server is not available: {exc}",
                "Run `agency mcp calendar` to inspect local Agency configuration.",
            )

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        start, end = self._window(since)
        with self._client_factory() as client:
            tools = _tool_names(client.list_tools())
            self.discovered_tools = tools
            tool = _calendar_view_tool(tools)
            args: dict[str, Any] = {
                "userIdentifier": "me",
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "orderby": "start/dateTime",
                "top": self.top,
            }
            _assert_read_only(tool)
            payload = _normalise_payload(client.call_tool(tool, args))

        seen: set[str] = set()
        for record in _records(payload, "events", "items", "value", "result"):
            if not is_actionable_event(record):
                continue
            item = self._map_event(record)
            if item.source_uri in seen:
                continue
            seen.add(item.source_uri)
            yield item

    def _window(self, since: datetime | None) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        start = since or now
        end_base = start if _as_utc(start) > now else now
        return start, end_base + timedelta(days=max(self.lookahead_days, 1))

    def _map_event(self, record: dict[str, Any]) -> SourceItem:
        body = _body_text(record)
        title = str(record.get("subject") or record.get("title") or "(no subject)")
        start = _event_datetime(record.get("start") or record.get("startDateTime"))
        event_id = str(record.get("id") or record.get("eventId") or record.get("iCalUId") or title)
        source_uri = calendar_uri(
            event_id,
            occurrence_id=_occurrence_id(record),
            series_id=_series_id(record),
            start=_event_datetime_text(record.get("start") or record.get("startDateTime")),
        )
        return SourceItem(
            source=SourceKind.CALENDAR,
            source_uri=source_uri,
            title=title,
            body=body,
            url=_event_url(record),
            source_state=_source_state(record),
            owner=_identity(record.get("organizer") or record.get("owner")),
            assignees=_attendees(record),
            created_at=parse_datetime(record.get("createdDateTime") or record.get("created")),
            updated_at=parse_datetime(
                record.get("lastModifiedDateTime") or record.get("updatedDateTime")
            ),
            due_at=start,
            labels=_categories(record),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=record,
        )


def calendar_uri(
    event_id: str,
    *,
    occurrence_id: str | None = None,
    series_id: str | None = None,
    start: str | None = None,
) -> str:
    """Build the idempotency key; recurring occurrences need their own identity."""

    base_id = _clean_id(event_id)
    series = _clean_id(series_id)
    occurrence = _clean_id(occurrence_id) or base_id
    if series:
        occurrence = occurrence if occurrence != series else _clean_id(start) or occurrence
        return f"calendar:event:{series}:occurrence:{occurrence}"
    if occurrence_id and occurrence != base_id:
        return f"calendar:event:{base_id}:occurrence:{occurrence}"
    return f"calendar:event:{base_id}"


def is_actionable_event(event: dict[str, Any]) -> bool:
    """Conservative, isolated heuristic for meetings that imply user work."""

    if event.get("isCancelled") is True:
        return False

    subject = str(event.get("subject") or event.get("title") or "")
    body = _body_text(event)
    text = f"{subject}\n{body}".lower()
    if not text.strip():
        return False
    if "no action required" in text or subject.lower().startswith(("fyi", "hold:")):
        return False

    explicit_work = re.search(
        r"\b(pre[- ]?read|prep(?:are|aration)?|come prepared|please review|"
        r"review before|action item|follow up|next steps|can you|could you|"
        r"need you to|please send|please share|assigned to (me|you))\b",
        text,
    )
    if explicit_work is not None:
        return True

    customer_meeting = re.search(r"\b(customer|client|qbr|ebr|account team|exec review)\b", text)
    needs_follow_up = re.search(r"\b(follow[- ]?up|next steps|action items?|recap)\b", text)
    if customer_meeting is not None and needs_follow_up is not None:
        return True

    if _is_organizer(event) and _has_no_agenda(subject, body) and not _is_routine_recurrence(event):
        return True

    return False


def _calendar_view_tool(tools: list[str]) -> str:
    if CALENDAR_VIEW_TOOL in tools:
        return CALENDAR_VIEW_TOOL
    raise RuntimeError(
        f"Calendar MCP exposes no `{CALENDAR_VIEW_TOOL}` tool. Discovered: {sorted(tools)}"
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _body_text(event: dict[str, Any]) -> str:
    body = event.get("body") or event.get("bodyPreview") or event.get("description") or ""
    if isinstance(body, dict):
        return str(body.get("content") or body.get("text") or body.get("preview") or "")
    return str(body)


def _event_datetime(value: Any) -> datetime | None:
    return parse_datetime(_event_datetime_text(value))


def _event_datetime_text(value: Any) -> str | None:
    if isinstance(value, dict):
        raw = value.get("dateTime") or value.get("date") or value.get("time")
        return str(raw) if raw else None
    if isinstance(value, str):
        return value
    return None


def _identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        email = value.get("emailAddress")
        if isinstance(email, dict):
            return email.get("address") or email.get("name")
        return value.get("address") or value.get("email") or value.get("name")
    return str(value)


def _attendees(event: dict[str, Any]) -> list[str]:
    attendees = event.get("attendees") or []
    if not isinstance(attendees, list):
        return []
    identities = [_identity(attendee) for attendee in attendees]
    return [identity for identity in identities if identity]


def _categories(event: dict[str, Any]) -> list[str]:
    categories = event.get("categories") or []
    return [str(category) for category in categories] if isinstance(categories, list) else []


def _source_state(event: dict[str, Any]) -> str | None:
    if event.get("isCancelled") is True:
        return "cancelled"
    for key in ("status", "showAs"):
        value = event.get(key)
        if value:
            return str(value)
    response = event.get("responseStatus")
    if isinstance(response, dict) and response.get("response"):
        return str(response["response"])
    return str(response) if isinstance(response, str) else None


def _event_url(event: dict[str, Any]) -> str | None:
    online = event.get("onlineMeeting")
    if isinstance(online, dict) and online.get("joinUrl"):
        return str(online["joinUrl"])
    return event.get("webLink") or event.get("url") or event.get("joinUrl")


def _is_organizer(event: dict[str, Any]) -> bool:
    return bool(event.get("isOrganizer") or event.get("isOrganizedByMe"))


def _has_no_agenda(subject: str, body: str) -> bool:
    text = _strip_boilerplate(f"{subject}\n{body}").lower()
    if "agenda" in text or "objectives" in text:
        return False
    return len(text.split()) <= 18


def _strip_boilerplate(text: str) -> str:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and "microsoft teams" not in line.lower()
        and "join the meeting" not in line.lower()
        and not line.lower().startswith(("https://", "http://"))
    ]
    return " ".join(lines)


def _is_routine_recurrence(event: dict[str, Any]) -> bool:
    if not _is_recurring(event):
        return False
    text = f"{event.get('subject') or event.get('title') or ''}\n{_body_text(event)}".lower()
    routine = r"\b(stand[- ]?up|daily sync|weekly sync|status meeting|scrum)\b"
    return re.search(routine, text) is not None


def _is_recurring(event: dict[str, Any]) -> bool:
    return bool(event.get("recurrence") or event.get("seriesMasterId") or _series_id(event))


def _series_id(event: dict[str, Any]) -> str | None:
    for key in ("seriesMasterId", "seriesId", "recurringEventId"):
        if event.get(key):
            return str(event[key])
    return None


def _occurrence_id(event: dict[str, Any]) -> str | None:
    for key in ("occurrenceId", "instanceId", "originalStart", "iCalUId"):
        if event.get(key):
            return str(event[key])
    event_type = str(event.get("type") or "").lower()
    if event_type in {"occurrence", "exception"}:
        return str(event.get("id") or "") or _event_datetime_text(event.get("start"))
    return None


def _clean_id(value: str | None) -> str:
    return str(value or "").strip()
