"""Tests for the read-only tool guard.

Ingest must never mutate a source system. The guard is the last line of defence
between a scheduled background sync and someone's real ADO board, Planner plan
or calendar — so it is tested against the **actual** tool names discovered on
live Agency MCP servers, not invented ones.
"""

from __future__ import annotations

import pytest

from task_graph.connectors.mcp_client import (
    assert_read_only,
    is_mutating_tool,
    tool_name_tokens,
)

# Verified live via `agency mcp <server>` list_tools.
ADO_READ = ["wit_work_item", "wit_query", "wit_backlog", "search_workitem",
            "core_list_orgs", "core_list_projects", "repo_pull_request", "advsec_alerts"]
ADO_WRITE = ["wit_work_item_write", "wit_work_item_comment_write", "wit_work_item_link_write",
             "wiki_upsert_page", "repo_create_branch", "repo_pull_request_write",
             "pipelines_write", "work_capacity_write", "wit_work_item_attachment_upload"]

PLANNER_READ = ["QueryPlans", "GetPlan", "QueryTasksInPlan", "GetTask", "GetGoal",
                "QueryGoalsInPlan", "GetUserGroups"]
PLANNER_WRITE = ["CreateTask", "UpdateTask", "CreatePlan", "UpdatePlan",
                 "CreateGoal", "UpdateGoal"]

CALENDAR_READ = ["ListEvents", "ListCalendarView", "FindMeetingTimes", "GetRooms",
                 "GetUserDateAndTimeZoneSettings"]
CALENDAR_WRITE = ["CreateEvent", "UpdateEvent", "DeleteEventById", "AcceptEvent",
                  "TentativelyAcceptEvent", "DeclineEvent", "CancelEvent", "ForwardEvent"]


@pytest.mark.parametrize("name", ADO_READ + PLANNER_READ + CALENDAR_READ)
def test_real_read_tools_are_allowed(name):
    assert_read_only(name)
    assert not is_mutating_tool(name)


@pytest.mark.parametrize("name", ADO_WRITE + PLANNER_WRITE + CALENDAR_WRITE)
def test_real_mutating_tools_are_blocked(name):
    assert is_mutating_tool(name)
    with pytest.raises(RuntimeError, match="Refusing to call mutating tool"):
        assert_read_only(name)


def test_pascal_case_mutations_are_caught():
    """Regression: a substring check for `_write` let every PascalCase
    mutation through, so `CreateTask` and `UpdateTask` were callable during a
    read-only sync."""
    for name in ("CreateTask", "UpdateTask", "DeleteEventById", "CancelEvent"):
        assert is_mutating_tool(name), name


def test_invite_responses_count_as_mutations():
    """Accepting or declining a meeting writes to other people's calendars."""
    for name in ("AcceptEvent", "DeclineEvent", "TentativelyAcceptEvent", "ForwardEvent"):
        assert is_mutating_tool(name), name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("wit_work_item_write", ["wit", "work", "item", "write"]),
        ("CreateTask", ["create", "task"]),
        ("QueryTasksInPlan", ["query", "tasks", "in", "plan"]),
        ("GetHTTPResponse", ["get", "http", "response"]),
        ("get_my_deals", ["get", "my", "deals"]),
        ("listEvents2", ["list", "events", "2"]),
    ],
)
def test_tokenizer_handles_every_naming_style(name, expected):
    assert tool_name_tokens(name) == expected


def test_plural_nouns_are_not_mistaken_for_verbs():
    """`GetReplies` reads; `Reply` writes. Only exact tokens match."""
    assert not is_mutating_tool("GetReplies")
    assert not is_mutating_tool("ListFlaggedMessages")
    assert is_mutating_tool("ReplyToMessage")
    assert is_mutating_tool("FlagMessage")


def test_allow_list_permits_a_known_false_positive():
    with pytest.raises(RuntimeError):
        assert_read_only("GetStartTime")
    assert_read_only("GetStartTime", allow={"GetStartTime"})


def test_guard_errs_towards_refusing():
    """A false refusal is loud and overridable; a false permit writes to prod."""
    assert is_mutating_tool("SomethingUpdateSomething")
    assert not is_mutating_tool("read_only_report")
