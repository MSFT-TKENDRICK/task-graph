from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from task_graph.connectors.planner import (
    ENV_PLANNER_PLANS,
    PlannerConnector,
    _state_from_percent,
    planner_uri,
)
from task_graph.ontology.types import SourceKind
from task_graph.pipeline.ingest import STATE_MAP


class FakeAgency:
    def __init__(self, tools: list[Any], payload: Any = None, *, fail: bool = False) -> None:
        self.tools = tools
        self.payload = payload if payload is not None else {"tasks": []}
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

    def __enter__(self) -> FakeAgency:
        if self.fail:
            raise RuntimeError("planner unavailable")
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def list_tools(self) -> list[Any]:
        return self.tools

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return self.responses.get(name, self.payload)


READ_TOOLS = [
    {
        "name": "QueryPlans",
        "description": "Lists the most recently used plans by the current user.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "GetPlan",
        "description": "Retrieves details of a Planner plan by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"planId": {"type": "string"}},
            "required": ["planId"],
        },
    },
    {
        "name": "GetTask",
        "description": "Retrieves a Planner task by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"taskId": {"type": "string"}},
            "required": ["taskId"],
        },
    },
    {
        "name": "QueryTasksInPlan",
        "description": "Retrieves tasks from a Planner plan.",
        "inputSchema": {
            "type": "object",
            "properties": {"planId": {"type": "string"}},
            "required": ["planId"],
        },
    },
]


def realistic_task(percent: int = 50) -> dict[str, Any]:
    return {
        "id": "task-1",
        "title": "Review launch plan AB#42",
        "notes": "See https://example.com/launch",
        "percentComplete": percent,
        "dueDateTime": "2026-08-20T17:00:00Z",
        "createdDateTime": "2026-08-01T12:00:00Z",
        "lastModifiedDateTime": "2026-08-10T12:00:00Z",
        "priority": "important",
        "bucket": {"id": "bucket-1", "name": "Sprint"},
        "assignments": [{"displayName": "Taylor", "email": "taylor@example.com"}],
        "webUrl": "https://planner.example/tasks/task-1",
    }


def test_planner_connector_discovers_runtime_tools_and_maps_text_block_json() -> None:
    fake = FakeAgency(READ_TOOLS)
    fake.responses = {
        "GetPlan": {"plan": {"id": "plan-1", "title": "Team Plan"}},
        "QueryTasksInPlan": {
            "content": [{"type": "text", "text": json.dumps({"tasks": [realistic_task()]})}]
        },
        "GetTask": {"task": {"id": "task-1", "checklist": []}},
    }

    connector = PlannerConnector(lambda: fake, plans=["plan-1"], assigned_to_user_id="me-id")
    items = list(connector.fetch())

    assert connector.discovered_tools == [tool["name"] for tool in READ_TOOLS]
    assert fake.calls[0] == ("GetPlan", {"planId": "plan-1"})
    assert fake.calls[1] == (
        "QueryTasksInPlan",
        {"planId": "plan-1", "assignedToUserId": "me-id"},
    )
    item = items[0]
    assert item.source == SourceKind.PLANNER
    assert item.source_uri == "planner:task:task-1"
    assert item.title == "Review launch plan AB#42"
    assert item.source_state == "inProgress"
    assert item.due_at == datetime.fromisoformat("2026-08-20T17:00:00+00:00")
    assert "Plan: Team Plan" in item.body
    assert "Bucket: Sprint" in item.body
    assert "plan:Team Plan" in item.labels
    assert "bucket:Sprint" in item.labels
    assert item.assignees == ["Taylor"]
    assert item.external_refs == ["AB#42", "https://example.com/launch"]


def test_planner_connector_maps_structured_content_shape() -> None:
    fake = FakeAgency(
        [{"name": "QueryTasksInPlan"}, {"name": "GetPlan"}],
        {"structuredContent": {"tasks": [realistic_task(0)]}},
    )
    fake.responses = {"GetPlan": {"result": {"id": "plan-1", "title": "Backlog"}}}

    items = list(PlannerConnector(lambda: fake, plans=["plan-1"]).fetch())

    assert items[0].source_state == "notStarted"
    assert "Plan: Backlog" in items[0].body


@pytest.mark.parametrize(
    ("percent", "state"),
    [
        (0, "notStarted"),
        (1, "inProgress"),
        (50, "inProgress"),
        (99, "inProgress"),
        (100, "completed"),
    ],
)
def test_percent_complete_boundaries_match_planner_states(percent: int, state: str) -> None:
    assert _state_from_percent(percent) == state


def test_planner_state_strings_are_ingest_contract_keys() -> None:
    planner_states = STATE_MAP[SourceKind.PLANNER]
    for percent in (0, 1, 50, 99, 100):
        assert _state_from_percent(percent).lower() in planner_states


def test_planner_source_uri_is_stable() -> None:
    assert planner_uri(" task-1 ") == "planner:task:task-1"
    assert planner_uri("task-1") == planner_uri("task-1")


def test_is_available_never_raises_on_clean_failure() -> None:
    status = PlannerConnector(lambda: FakeAgency([], fail=True)).is_available()

    assert not status.available
    assert "Planner MCP server is not available" in status.detail
    assert status.remediation


def test_planner_asks_user_to_pin_plans_and_lists_real_options() -> None:
    fake = FakeAgency([{"name": "QueryPlans"}, {"name": "QueryTasksInPlan"}])
    fake.responses = {
        "QueryPlans": {
            "result": [
                {"id": "plan-a", "title": "Alpha"},
                {"planId": "plan-b", "displayName": "Beta"},
            ]
        }
    }

    with pytest.raises(RuntimeError) as exc_info:
        list(PlannerConnector(lambda: fake).fetch())

    message = str(exc_info.value)
    assert ENV_PLANNER_PLANS in message
    assert "Alpha (plan-a)" in message
    assert "Beta (plan-b)" in message
    assert fake.calls == [("QueryPlans", {})]


def test_planner_connector_never_selects_mutating_tools() -> None:
    fake = FakeAgency(
        [{"name": "CreateTask"}, {"name": "UpdateTask"}, {"name": "QueryTasksInPlan"}],
        {"tasks": []},
    )

    list(PlannerConnector(lambda: fake, plans=["plan-1"]).fetch())

    assert fake.calls == [("QueryTasksInPlan", {"planId": "plan-1"})]


def test_planner_errors_before_calling_when_only_mutating_task_tools_exist() -> None:
    fake = FakeAgency([{"name": "CreateTask"}, {"name": "UpdateTask"}], {"tasks": []})

    with pytest.raises(RuntimeError, match="QueryTasksInPlan"):
        list(PlannerConnector(lambda: fake, plans=["plan-1"]).fetch())

    assert fake.calls == []
