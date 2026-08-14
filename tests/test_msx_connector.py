from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from task_graph.connectors.base import ConnectorStatus
from task_graph.connectors.msx import (
    MsxConnector,
    msx_activity_uri,
    msx_milestone_uri,
    msx_opportunity_uri,
)
from task_graph.ontology.types import SourceKind


class FakeAgency:
    def __init__(
        self,
        tools: list[Any],
        payload: Any = None,
        *,
        fail: Exception | None = None,
    ) -> None:
        self.tools = tools
        self.payload = payload if payload is not None else {}
        self.fail = fail
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> FakeAgency:
        if self.fail is not None:
            raise self.fail
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def list_tools(self) -> list[Any]:
        return self.tools

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return self.responses.get(name, self.payload)


class RoutingFakeAgency(FakeAgency):
    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        entity = args.get("entity")
        return self.responses[f"{name}:{entity}"]


def text_block(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_msx_source_uri_builders_are_distinct_and_stable() -> None:
    assert msx_opportunity_uri("abc") == "msx:opportunity:abc"
    assert msx_milestone_uri("abc") == "msx:milestone:abc"
    assert msx_activity_uri("abc") == "msx:activity:abc"
    assert len({msx_opportunity_uri("1"), msx_milestone_uri("1"), msx_activity_uri("1")}) == 3


def test_msx_connector_maps_preferred_tools_text_and_structured_payloads() -> None:
    fake = FakeAgency(
        [
            {"name": "get_my_deals"},
            {"name": "get_my_milestones"},
            {"name": "get_my_hok_activities"},
            {"name": "update_opportunity"},
        ]
    )
    fake.responses = {
        "get_my_deals": text_block(
            {
                "deals": [
                    {
                        "opportunityId": "opp-1",
                        "opportunityName": "Fabricam Copilot expansion AB#42",
                        "description": "Align with https://example.com/plan",
                        "salesStage": "Qualify",
                        "estimatedRevenue": 2500000,
                        "estimatedCloseDate": "2026-09-30T00:00:00Z",
                        "accountName": "Fabrikam",
                        "dealTeam": [{"displayName": "Adele"}, {"email": "taylor@example.com"}],
                        "owner": {"displayName": "Taylor"},
                    }
                ]
            }
        ),
        "get_my_milestones": {
            "structuredContent": {
                "milestones": [
                    {
                        "milestoneId": "ms-1",
                        "name": "Business decision",
                        "status": "In Progress",
                        "dueDate": "2026-08-20T12:00:00Z",
                        "accountName": "Fabrikam",
                        "dealTeam": ["Adele"],
                    }
                ]
            }
        },
        "get_my_hok_activities": {
            "activities": [
                {
                    "activityId": "act-1",
                    "subject": "Customer follow-up #77",
                    "notes": "Send recap for AB#77",
                    "activityStatus": "Open",
                    "scheduledEnd": "2026-08-18T17:00:00Z",
                    "customerName": "Fabrikam",
                    "assignedTo": [{"name": "Taylor"}],
                }
            ]
        },
    }

    items = list(MsxConnector(lambda: fake).fetch())

    assert [call[0] for call in fake.calls] == [
        "get_my_deals",
        "get_my_milestones",
        "get_my_hok_activities",
    ]
    opportunity, milestone, activity = items
    assert opportunity.source == SourceKind.MSX
    assert opportunity.source_uri == "msx:opportunity:opp-1"
    assert opportunity.source_state == "Qualify"
    assert opportunity.due_at == datetime.fromisoformat("2026-09-30T00:00:00+00:00")
    assert "value:2500000" in opportunity.labels
    assert "Estimated value: 2500000" in opportunity.body
    assert opportunity.raw["accountName"] == "Fabrikam"
    assert opportunity.raw["dealTeam"][0]["displayName"] == "Adele"
    assert opportunity.assignees == ["Adele", "taylor@example.com"]
    assert opportunity.external_refs == ["AB#42", "https://example.com/plan"]
    assert milestone.source_uri == "msx:milestone:ms-1"
    assert milestone.source_state == "In Progress"
    assert milestone.raw["accountName"] == "Fabrikam"
    assert activity.source_uri == "msx:activity:act-1"
    assert activity.source_state == "Open"
    assert activity.external_refs == ["AB#77", "#77"]


def test_msx_connector_falls_back_to_search_and_dataverse_query() -> None:
    fake = RoutingFakeAgency([{"name": "search_opportunities"}, {"name": "dataverse_query"}])
    fake.responses = {
        "search_opportunities:None": {"opportunities": [{"id": "opp-2", "name": "Renewal"}]},
        "dataverse_query:milestones": {
            "records": [{"id": "ms-2", "title": "Proposal", "state": "Open"}]
        },
        "dataverse_query:activities": {
            "value": [{"id": "act-2", "title": "Call", "state": "Completed"}]
        },
    }

    items = list(MsxConnector(lambda: fake).fetch())

    assert [call[0] for call in fake.calls] == [
        "search_opportunities",
        "dataverse_query",
        "dataverse_query",
    ]
    assert [item.source_uri for item in items] == [
        "msx:opportunity:opp-2",
        "msx:milestone:ms-2",
        "msx:activity:act-2",
    ]
    assert fake.calls[1][1]["entity"] == "milestones"
    assert fake.calls[2][1]["entity"] == "activities"


def test_msx_connector_does_not_select_mutating_tools() -> None:
    fake = FakeAgency(
        [
            {"name": "get_my_deals_write"},
            {"name": "search_opportunities_create"},
            {"name": "get_my_milestones_update"},
            {"name": "get_my_hok_activities_delete"},
        ]
    )

    with pytest.raises(RuntimeError, match="no supported read path"):
        list(MsxConnector(lambda: fake).fetch())

    assert fake.calls == []


def test_msx_is_available_success_and_login_only() -> None:
    available = MsxConnector(lambda: FakeAgency([{"name": "get_my_deals"}])).is_available()
    assert available.available
    assert "get_my_deals" in available.detail

    login_only = MsxConnector(lambda: FakeAgency([{"name": "msx_login"}])).is_available()
    assert not login_only.available
    assert "no read tools" in login_only.detail
    assert login_only.remediation is not None
    assert "msx_login" in login_only.remediation


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("error: unrecognized subcommand 'msx'", "not installed"),
        ("401 unauthorized: auth required", "not authenticated"),
        ("Timed out reaching microsoftsales.crm.dynamics.com", "could not reach"),
    ],
)
def test_msx_is_available_distinguishes_unavailability_causes(
    message: str, expected: str
) -> None:
    status = MsxConnector(lambda: FakeAgency([], fail=RuntimeError(message))).is_available()

    assert isinstance(status, ConnectorStatus)
    assert not status.available
    assert expected in status.detail
    assert status.remediation
