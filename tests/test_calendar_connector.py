from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from task_graph.connectors.calendar import (
    CALENDAR_VIEW_TOOL,
    CalendarConnector,
    calendar_uri,
    is_actionable_event,
)
from task_graph.ontology.types import SourceKind


class FakeAgency:
    def __init__(self, tools: list[Any], payload: Any, *, fail: bool = False) -> None:
        self.tools = tools
        self.payload = payload
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

    def __enter__(self) -> FakeAgency:
        if self.fail:
            raise RuntimeError("server unavailable")
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def list_tools(self) -> list[Any]:
        return self.tools

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return self.responses.get(name, self.payload)


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": "evt-1",
        "subject": "Customer QBR follow-up",
        "body": {"content": "Please prepare next steps for AB#42. https://example.com/deck"},
        "start": {"dateTime": "2026-08-20T09:30:00-07:00", "timeZone": "Pacific Standard Time"},
        "end": {"dateTime": "2026-08-20T10:00:00-07:00", "timeZone": "Pacific Standard Time"},
        "organizer": {"emailAddress": {"address": "lead@example.com", "name": "Lead"}},
        "attendees": [{"emailAddress": {"address": "me@example.com", "name": "Me"}}],
        "showAs": "busy",
        "webLink": "https://outlook.office.com/calendar/item/evt-1",
        "categories": ["Customer"],
        "createdDateTime": "2026-08-01T12:00:00Z",
        "lastModifiedDateTime": "2026-08-10T12:00:00Z",
    }
    event.update(overrides)
    return event


def test_calendar_connector_discovers_runtime_tool_and_maps_structured_content() -> None:
    fake = FakeAgency(
        [{"name": CALENDAR_VIEW_TOOL}, {"name": "CreateEvent"}],
        {"structuredContent": {"events": [_event()]}},
    )
    since = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)

    items = list(CalendarConnector(lambda: fake, lookahead_days=14).fetch(since=since))

    assert fake.calls[0][0] == CALENDAR_VIEW_TOOL
    assert fake.calls[0][1]["userIdentifier"] == "me"
    assert fake.calls[0][1]["startDateTime"] == "2026-08-14T20:00:00+00:00"
    assert fake.calls[0][1]["orderby"] == "start/dateTime"
    assert items[0].source == SourceKind.CALENDAR
    assert items[0].source_uri == "calendar:event:evt-1"
    assert items[0].title == "Customer QBR follow-up"
    assert items[0].owner == "lead@example.com"
    assert items[0].assignees == ["me@example.com"]
    assert items[0].source_state == "busy"
    assert items[0].labels == ["Customer"]
    assert items[0].external_refs == ["AB#42", "https://example.com/deck"]
    assert fake.calls[0][0] not in {"CreateEvent", "UpdateEvent", "DeleteEventById"}


def test_calendar_connector_reads_events_from_text_block_json() -> None:
    fake = FakeAgency(
        [{"name": CALENDAR_VIEW_TOOL}],
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"value": [_event(id="evt-text", subject="Please review plan")]}
                    ),
                }
            ]
        },
    )

    items = list(CalendarConnector(lambda: fake).fetch())

    assert fake.calls[0][0] == CALENDAR_VIEW_TOOL
    assert items[0].source_uri == "calendar:event:evt-text"


def test_due_at_is_set_from_start_time_with_timezone() -> None:
    fake = FakeAgency(
        [{"name": CALENDAR_VIEW_TOOL}],
        {"items": [_event(start={"dateTime": "2026-08-21T15:45:00+05:30"})]},
    )

    item = list(CalendarConnector(lambda: fake).fetch())[0]

    assert item.due_at == datetime(
        2026, 8, 21, 15, 45, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    assert item.due_at and item.due_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_calendar_uri_is_stable_and_distinct_for_recurring_occurrences() -> None:
    first = calendar_uri("occ-1", series_id="series-1", occurrence_id="2026-08-21T09:00:00Z")
    second = calendar_uri("occ-2", series_id="series-1", occurrence_id="2026-08-22T09:00:00Z")

    assert calendar_uri("evt-1") == calendar_uri("evt-1")
    assert first == calendar_uri(
        "occ-1", series_id="series-1", occurrence_id="2026-08-21T09:00:00Z"
    )
    assert first != second


def test_recurring_occurrences_with_same_series_are_distinct_when_mapped() -> None:
    fake = FakeAgency(
        [{"name": CALENDAR_VIEW_TOOL}],
        {
            "events": [
                _event(
                    id="occ-1",
                    seriesMasterId="series-1",
                    occurrenceId="2026-08-21T09:00:00Z",
                    subject="Prep for design review",
                ),
                _event(
                    id="occ-2",
                    seriesMasterId="series-1",
                    occurrenceId="2026-08-22T09:00:00Z",
                    subject="Prep for design review",
                ),
            ]
        },
    )

    uris = [item.source_uri for item in CalendarConnector(lambda: fake).fetch()]

    assert uris == [
        "calendar:event:series-1:occurrence:2026-08-21T09:00:00Z",
        "calendar:event:series-1:occurrence:2026-08-22T09:00:00Z",
    ]


def test_calendar_actionability_heuristic_positive_and_negative_cases() -> None:
    assert is_actionable_event(_event(subject="Prep for customer review"))
    assert is_actionable_event(
        _event(subject="Planning", body="", isOrganizer=True, attendees=[])
    )
    assert is_actionable_event(_event(subject="Client EBR", body="Follow-up and next steps"))
    assert is_actionable_event(_event(subject="Design review", body="Can you review before?"))

    assert not is_actionable_event(
        _event(
            subject="Daily standup",
            body="",
            recurrence={"pattern": {"type": "daily"}},
            isOrganizer=False,
            seriesMasterId="standup-series",
        )
    )
    assert not is_actionable_event(_event(subject="FYI team holiday", body="No action required"))
    assert not is_actionable_event(_event(subject="Cancelled prep", isCancelled=True))


def test_is_available_never_raises_on_calendar_failure() -> None:
    status = CalendarConnector(lambda: FakeAgency([], {}, fail=True)).is_available()

    assert not status.available
    assert "Calendar MCP server is not available" in status.detail
    assert status.remediation


def test_no_mutating_tool_is_ever_selected_or_called() -> None:
    fake = FakeAgency([{"name": "CreateEvent"}, {"name": "UpdateEvent"}], {"events": []})

    with pytest.raises(RuntimeError, match=CALENDAR_VIEW_TOOL):
        list(CalendarConnector(lambda: fake).fetch())

    assert fake.calls == []
