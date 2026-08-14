from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from task_graph.connectors.teams import (
    TeamsConnector,
    is_actionable_teams_message,
    teams_uri,
)
from task_graph.ontology.types import SourceKind


class FakeAgency:
    def __init__(self, tools: list[Any], payload: Any, *, fail: bool = False) -> None:
        self.tools = tools
        self.payload = payload
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Optional per-tool responses; falls back to ``payload``.
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
        if name in self.responses:
            response = self.responses[name]
            if callable(response):
                return response(args)
            return response
        return self.payload


READ_TOOLS = [
    {
        "name": "ListChats",
        "description": "Get the signed-in user's recent Teams chats.",
        "inputSchema": {"properties": {}, "required": [], "type": "object"},
    },
    {
        "name": "ListChatMessages",
        "description": "List messages in a Teams chat.",
        "inputSchema": {
            "properties": {"chatId": {"type": "string"}, "top": {"type": "integer"}},
            "required": ["chatId"],
            "type": "object",
        },
    },
]


def test_teams_connector_discovers_runtime_tools_and_maps_recorded_payload() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {
            "structuredContent": {
                "chats": [
                    {
                        "id": "19:chat-thread@thread.v2",
                        "topic": "Project Falcon",
                        "chatType": "group",
                        "hasUnreadMessages": True,
                    }
                ]
            }
        },
        "ListChatMessages": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "messages": [
                                {
                                    "id": "1700000000000",
                                    "createdDateTime": "2026-08-14T15:00:00Z",
                                    "lastModifiedDateTime": "2026-08-14T15:02:00Z",
                                    "from": {"user": {"displayName": "Adele Vance"}},
                                    "body": {
                                        "contentType": "html",
                                        "content": (
                                            "<p>@Taylor can you review AB#42 and "
                                            "https://github.com/o/r/pull/7?</p>"
                                        ),
                                    },
                                    "mentions": [{"id": 0, "mentionText": "Taylor"}],
                                    "importance": "normal",
                                    "webUrl": "https://teams.microsoft.com/l/message/1",
                                },
                                {
                                    "id": "1700000000001",
                                    "createdDateTime": "2026-08-14T15:01:00Z",
                                    "from": {"user": {"displayName": "Adele Vance"}},
                                    "body": {"content": "FYI the deployment finished."},
                                },
                            ]
                        }
                    ),
                }
            ]
        },
    }

    connector = TeamsConnector(lambda: fake)
    items = list(connector.fetch())

    assert connector.discovered_tools == ["ListChats", "ListChatMessages"]
    assert fake.calls == [
        ("ListChats", {}),
        ("ListChatMessages", {"chatId": "19:chat-thread@thread.v2", "top": 50}),
    ]
    assert len(items) == 1
    item = items[0]
    assert item.source == SourceKind.TEAMS
    assert item.source_uri == "teams:message:19:chat-thread@thread.v2:1700000000000"
    assert item.title.startswith("Project Falcon:")
    assert item.owner == "Adele Vance"
    assert item.source_state == '{"chatHasUnreadMessages":true,"importance":"normal"}'
    assert item.external_refs == ["AB#42", "https://github.com/o/r/pull/7"]
    assert item.created_at == datetime(2026, 8, 14, 15, 0, tzinfo=UTC)


def test_teams_connector_accepts_structured_message_content() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {"chats": [{"id": "chat-1", "chatType": "oneOnOne"}]},
        "ListChatMessages": {
            "structuredContent": {
                "messages": [
                    {
                        "id": "msg-1",
                        "from": {"user": {"displayName": "Diego"}},
                        "body": "Can you take a look?",
                    }
                ]
            }
        },
    }

    items = list(TeamsConnector(lambda: fake, now=_now).fetch())

    assert [item.source_uri for item in items] == ["teams:message:chat-1:msg-1"]
    assert items[0].body == "Can you take a look?"


def test_teams_uri_is_stable_for_identical_inputs() -> None:
    assert teams_uri(" chat-1 ", " msg-1 ") == "teams:message:chat-1:msg-1"
    assert teams_uri("chat-1", "msg-1") == teams_uri("chat-1", "msg-1")


def test_teams_actionability_heuristic_positive_and_negative_cases() -> None:
    assert is_actionable_teams_message(
        {"body": {"content": "@Taylor please review this"}, "mentions": [{"id": 0}]}
    )
    assert is_actionable_teams_message(
        {
            "body": "Can you follow up?",
            "_task_graph_chat": {"chatType": "oneOnOne"},
        }
    )
    assert is_actionable_teams_message(
        {
            "body": "Latest message in an unread direct chat",
            "_task_graph_chat": {"chatType": "oneOnOne", "hasUnreadMessages": True},
        }
    )
    assert not is_actionable_teams_message({"body": "FYI broad channel announcement"})
    assert not is_actionable_teams_message(
        {"body": "Can you check?", "from": {"user": {"isMe": True}}}
    )
    assert not is_actionable_teams_message({"body": "The build is green."})


def test_teams_connector_filters_since_and_extracts_external_refs() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {"chats": [{"id": "chat-1", "chatType": "oneOnOne"}]},
        "ListChatMessages": {
            "messages": [
                {
                    "id": "new",
                    "createdDateTime": "2026-08-14T15:30:00Z",
                    "from": {"user": {"displayName": "Adele"}},
                    "body": "Could you look at #99?",
                },
                {
                    "id": "old",
                    "createdDateTime": "2026-08-13T15:30:00Z",
                    "from": {"user": {"displayName": "Adele"}},
                    "body": "Could you look at AB#98?",
                },
            ]
        },
    }

    items = list(TeamsConnector(lambda: fake, now=_now).fetch(datetime(2026, 8, 14, tzinfo=UTC)))

    assert [item.source_uri for item in items] == ["teams:message:chat-1:new"]
    assert items[0].external_refs == ["#99"]


def test_teams_time_window_excludes_old_messages() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {"chats": [{"id": "chat-1", "chatType": "oneOnOne"}]},
        "ListChatMessages": {
            "messages": [
                {
                    "id": "old",
                    "createdDateTime": "2026-07-01T12:00:00Z",
                    "from": {"user": {"displayName": "Adele"}},
                    "body": "Can you review this?",
                }
            ]
        },
    }

    items = list(TeamsConnector(lambda: fake, now=_now, lookback_days=14).fetch())

    assert items == []


def test_teams_conversation_is_excluded_after_user_reply() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {"chats": [{"id": "chat-1", "chatType": "oneOnOne"}]},
        "ListChatMessages": {
            "messages": [
                {
                    "id": "reply",
                    "createdDateTime": "2026-08-14T15:10:00Z",
                    "from": {"user": {"displayName": "Taylor", "isMe": True}},
                    "body": "I sent it.",
                },
                {
                    "id": "ask",
                    "createdDateTime": "2026-08-14T15:00:00Z",
                    "from": {"user": {"displayName": "Adele"}},
                    "body": "Can you send the deck?",
                },
            ]
        },
    }

    items = list(TeamsConnector(lambda: fake, now=_now).fetch())

    assert items == []


def test_teams_group_question_mark_alone_is_not_actionable_but_mention_is() -> None:
    assert not is_actionable_teams_message(
        {"body": "Does anyone know the room?", "_task_graph_chat": {"chatType": "group"}}
    )
    assert is_actionable_teams_message(
        {
            "body": "@Taylor does this look right?",
            "mentions": [{"id": 0}],
            "_task_graph_chat": {"chatType": "group"},
        }
    )


def test_teams_collapses_multiple_actionable_messages_to_one_conversation_item() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {"chats": [{"id": "chat-1", "chatType": "oneOnOne"}]},
        "ListChatMessages": {
            "messages": [
                {
                    "id": f"ask-{index}",
                    "createdDateTime": f"2026-08-14T15:0{index}:00Z",
                    "from": {"user": {"displayName": "Adele"}},
                    "body": f"Can you review part {index}?",
                }
                for index in range(5, 0, -1)
            ]
        },
    }

    items = list(TeamsConnector(lambda: fake, now=_now).fetch())

    assert len(items) == 1
    assert items[0].source_uri == "teams:message:chat-1:ask-1"
    assert items[0].body == "Can you review part 5?"


def test_teams_cap_truncates_deterministically_newest_first() -> None:
    fake = FakeAgency(READ_TOOLS, None)
    fake.responses = {
        "ListChats": {
            "chats": [
                {"id": "chat-1", "chatType": "oneOnOne"},
                {"id": "chat-2", "chatType": "oneOnOne"},
                {"id": "chat-3", "chatType": "oneOnOne"},
            ]
        },
        "ListChatMessages": lambda args: {
            "messages": [
                {
                    "id": "ask",
                    "createdDateTime": f"2026-08-14T15:0{args['chatId'][-1]}:00Z",
                    "from": {"user": {"displayName": "Adele"}},
                    "body": f"Can you review {args['chatId']}?",
                }
            ]
        },
    }

    connector = TeamsConnector(lambda: fake, now=_now, max_items=2)
    items = list(connector.fetch())

    assert [item.source_uri for item in items] == [
        "teams:message:chat-3:ask",
        "teams:message:chat-2:ask",
    ]
    assert connector.truncated_count == 1


def test_teams_is_available_returns_clean_status_when_tool_missing() -> None:
    fake = FakeAgency([{"name": "ListChats"}], None)

    status = TeamsConnector(lambda: fake).is_available()

    assert not status.available
    assert "ListChatMessages" in status.detail
    assert status.remediation


def test_teams_connector_never_selects_a_mutating_tool() -> None:
    fake = FakeAgency(
        [
            {"name": "CreateChat"},
            {"name": "SendMessageToChat"},
            {"name": "DeleteChat"},
        ],
        {"messages": []},
    )

    with pytest.raises(RuntimeError, match="required read tool"):
        list(TeamsConnector(lambda: fake).fetch())
    assert fake.calls == []


def _now() -> datetime:
    return datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
