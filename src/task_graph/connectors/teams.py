"""Teams ingestion through Agency's ambient Microsoft 365 auth."""

from __future__ import annotations

import json
import logging
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

LIST_CHATS = "ListChats"
LIST_CHAT_MESSAGES = "ListChatMessages"
READ_TOOLS = frozenset({LIST_CHATS, LIST_CHAT_MESSAGES})
MAX_CHATS_PER_SYNC = 25
ENV_TEAMS_LOOKBACK_DAYS = "TASK_GRAPH_TEAMS_LOOKBACK_DAYS"
ENV_TEAMS_MAX_ITEMS = "TASK_GRAPH_TEAMS_MAX_ITEMS"
ENV_TEAMS_MESSAGES_PER_CHAT = "TASK_GRAPH_TEAMS_MESSAGES_PER_CHAT"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MAX_ITEMS = 50
DEFAULT_MESSAGES_PER_CHAT = 50
LOGGER = logging.getLogger(__name__)


class TeamsConnector:
    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        lookback_days: int | None = None,
        max_items: int | None = None,
        messages_per_chat: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda: AgencyMcpClient("teams"))
        self.lookback_days = lookback_days if lookback_days is not None else _env_int(
            ENV_TEAMS_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS
        )
        self.max_items = max_items if max_items is not None else _env_int(
            ENV_TEAMS_MAX_ITEMS, DEFAULT_MAX_ITEMS
        )
        self.messages_per_chat = (
            messages_per_chat
            if messages_per_chat is not None
            else _env_int(ENV_TEAMS_MESSAGES_PER_CHAT, DEFAULT_MESSAGES_PER_CHAT)
        )
        self._now = now or (lambda: datetime.now(UTC))
        self.discovered_tools: list[str] = []
        self.truncated_count = 0

    @property
    def kind(self) -> SourceKind:
        return SourceKind.TEAMS

    @property
    def name(self) -> str:
        return "Microsoft Teams"

    def is_available(self) -> ConnectorStatus:
        try:
            client = self._client_factory()
            if hasattr(client, "is_available"):
                status = client.is_available()
                if not status.available:
                    return status
            with client as opened:
                tools = _tool_names(opened.list_tools())
            missing = [tool for tool in (LIST_CHATS, LIST_CHAT_MESSAGES) if tool not in tools]
            if missing:
                return ConnectorStatus(
                    False,
                    f"Teams MCP server is missing required read tools: {', '.join(missing)}.",
                    "Update Agency CLI or run `agency mcp teams` to inspect available tools.",
                )
            return ConnectorStatus(True, f"Found {len(tools)} Teams MCP tools.")
        except Exception as exc:
            return ConnectorStatus(
                False,
                f"Teams MCP server is not available: {exc}",
                "Run `agency mcp teams` to inspect local Agency configuration.",
            )

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        window_start = self._window_start(since)
        with self._client_factory() as client:
            tools = _tool_names(client.list_tools())
            self.discovered_tools = tools
            _require_tools(tools, LIST_CHATS, LIST_CHAT_MESSAGES)

            chat_payload = _normalise_payload(_call_read_tool(client, LIST_CHATS, {}))
            chats = _records(chat_payload, "chats", "items", "value", "result")
            records: list[dict[str, Any]] = []
            for chat in chats[:MAX_CHATS_PER_SYNC]:
                chat_id = str(chat.get("id") or "")
                if not chat_id:
                    continue
                args: dict[str, Any] = {"chatId": chat_id, "top": self.messages_per_chat}
                messages = _records(
                    _normalise_payload(_call_read_tool(client, LIST_CHAT_MESSAGES, args)),
                    "messages",
                    "items",
                    "value",
                    "result",
                )
                for message in messages:
                    if not (message.get("id") or message.get("messageId")):
                        continue
                    message["_task_graph_chat"] = chat
                    records.append(message)

        items = [
            self._map_conversation(candidate)
            for candidate in _conversation_candidates(records, window_start)
        ]
        items.sort(
            key=lambda item: item.updated_at or item.created_at or datetime.min,
            reverse=True,
        )
        max_items = max(self.max_items, 1)
        self.truncated_count = max(len(items) - max_items, 0)
        if self.truncated_count:
            LOGGER.warning("Teams connector truncated %s items at cap %s.", len(items), max_items)
        yield from items[:max_items]

    def _window_start(self, since: datetime | None) -> datetime:
        cutoff = _as_utc(self._now()) - timedelta(days=max(self.lookback_days, 1))
        if since is None:
            return cutoff
        return max(_as_utc(since), cutoff)

    def _map_conversation(self, candidate: _ConversationCandidate) -> SourceItem:
        record = candidate.message
        raw_chat = record.get("_task_graph_chat")
        chat = raw_chat if isinstance(raw_chat, dict) else {}
        body = _body_text(record)
        sender = record.get("from") or record.get("sender")
        topic = chat.get("topic") or chat.get("chatType") or "Teams"
        title = _title(record, body, str(topic))
        source_state = _source_state(record, chat)
        return SourceItem(
            source=SourceKind.TEAMS,
            source_uri=teams_uri(candidate.conversation_id, candidate.trigger_message_id),
            title=title,
            body=body,
            url=record.get("webUrl") or record.get("url") or chat.get("webUrl"),
            source_state=source_state,
            owner=_identity(sender),
            assignees=[],
            created_at=parse_datetime(record.get("createdDateTime")),
            updated_at=parse_datetime(
                record.get("lastModifiedDateTime") or record.get("createdDateTime")
            ),
            labels=_labels(record, chat),
            external_refs=extract_external_refs(f"{title}\n{body}"),
            raw=_raw_message(record),
        )


def teams_uri(chat_or_channel_id: str, message_id: str) -> str:
    """Build the idempotency key for one unresolved reply obligation.

    The message id is the oldest actionable message in the currently unanswered
    conversation segment. Newer follow-ups update the same item, while a user
    reply clears it and a later ask starts a new stable task.
    """

    return f"teams:message:{chat_or_channel_id.strip()}:{message_id.strip()}"


def is_actionable_teams_message(message: dict[str, Any]) -> bool:
    """Conservative heuristic kept isolated for later user-steered tuning."""

    body = _body_text(message)
    text = body.lower()
    if not text:
        return False
    if any(token in text for token in ("announcement:", "fyi", "no action required", "newsletter")):
        return False
    if _is_from_me(message):
        return False
    mention = _has_at_mention(message)
    if mention:
        return True
    raw_chat = message.get("_task_graph_chat")
    chat = raw_chat if isinstance(raw_chat, dict) else {}
    chat_type = str(chat.get("chatType") or "").lower()
    one_on_one = chat_type == "oneonone"
    group_chat = chat_type == "group"
    direct_to_me = bool(
        message.get("directToMe")
        or message.get("isDirectToMe")
        or message.get("mentionsMe")
    )
    explicit_request = _has_request_phrase(text)
    question = "?" in text
    if explicit_request and (one_on_one or group_chat or direct_to_me):
        return True
    if question and (one_on_one or direct_to_me):
        return True
    if direct_to_me and explicit_request:
        return True
    if one_on_one and chat.get("hasUnreadMessages") and _last_message_from_other(message, chat):
        return True
    return False


class _ConversationCandidate:
    def __init__(
        self,
        *,
        conversation_id: str,
        trigger_message_id: str,
        message: dict[str, Any],
    ) -> None:
        self.conversation_id = conversation_id
        self.trigger_message_id = trigger_message_id
        self.message = message


def _conversation_candidates(
    records: list[dict[str, Any]], window_start: datetime
) -> list[_ConversationCandidate]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        conversation_id = _conversation_id(record)
        if conversation_id:
            grouped.setdefault(conversation_id, []).append(record)

    candidates: list[_ConversationCandidate] = []
    for conversation_id, messages in grouped.items():
        current_segment: list[dict[str, Any]] = []
        for message in sorted(messages, key=_sort_time, reverse=True):
            timestamp = _message_time(message)
            if timestamp and _as_utc(timestamp) < window_start:
                break
            if _is_from_me(message):
                break
            if is_actionable_teams_message(message):
                current_segment.append(message)
        if not current_segment:
            continue
        newest = current_segment[0]
        trigger = current_segment[-1]
        candidates.append(
            _ConversationCandidate(
                conversation_id=conversation_id,
                trigger_message_id=str(trigger.get("id") or trigger.get("messageId")),
                message=newest,
            )
        )
    return candidates


def _conversation_id(message: dict[str, Any]) -> str | None:
    raw_chat = message.get("_task_graph_chat")
    chat = raw_chat if isinstance(raw_chat, dict) else {}
    value = (
        message.get("chatId")
        or message.get("conversationId")
        or chat.get("id")
        or message.get("channelId")
    )
    return str(value) if value else None


def _has_request_phrase(text: str) -> bool:
    return (
        re.search(
            r"\b(can you|could you|please|pls|need you to|please review|"
            r"take a look|follow up|wdyt|thoughts)\b",
            text,
        )
        is not None
    )


def _call_read_tool(client: Any, name: str, args: dict[str, Any]) -> Any:
    if name not in READ_TOOLS:
        raise RuntimeError(f"Refusing to call unapproved Teams MCP tool {name!r}.")
    _assert_read_only(name)
    return client.call_tool(name, args)


def _require_tools(tools: list[str], *required: str) -> None:
    missing = [tool for tool in required if tool not in tools]
    if missing:
        raise RuntimeError(
            f"Teams MCP exposes no required read tool(s): {', '.join(missing)}. "
            f"Discovered: {sorted(tools)}"
        )
    for tool in required:
        _assert_read_only(tool)


def _message_time(message: dict[str, Any]) -> datetime | None:
    return parse_datetime(message.get("lastModifiedDateTime") or message.get("createdDateTime"))


def _sort_time(message: dict[str, Any]) -> datetime:
    return _as_utc(_message_time(message) or datetime.min)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or message.get("bodyPreview") or message.get("content") or ""
    if isinstance(body, dict):
        return _strip_html(str(body.get("content") or body.get("text") or ""))
    return _strip_html(str(body))


def _strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _has_at_mention(message: dict[str, Any]) -> bool:
    mentions = message.get("mentions") or message.get("mentioned") or []
    if isinstance(mentions, list) and mentions:
        return True
    return bool(re.search(r"@\w+", _body_text(message)))


def _is_from_me(message: dict[str, Any]) -> bool:
    if bool(message.get("isFromMe") or message.get("fromMe")):
        return True
    sender = message.get("from") or message.get("sender") or {}
    if isinstance(sender, dict):
        user = sender.get("user") if isinstance(sender.get("user"), dict) else sender
        return bool(user.get("isMe") or user.get("me") or user.get("isCurrentUser"))
    return False


def _last_message_from_other(message: dict[str, Any], chat: dict[str, Any]) -> bool:
    preview = chat.get("lastMessagePreview")
    if not isinstance(preview, dict):
        return True
    sender = preview.get("from") or preview.get("sender")
    if isinstance(sender, dict):
        user = sender.get("user") if isinstance(sender.get("user"), dict) else sender
        return not bool(user.get("isMe") or user.get("me"))
    return True


def _title(message: dict[str, Any], body: str, topic: str) -> str:
    subject = message.get("subject") or message.get("summary")
    if subject:
        return str(subject)
    preview = body[:80].strip()
    return f"{topic}: {preview}" if preview else f"{topic}: Teams message"


def _source_state(message: dict[str, Any], chat: dict[str, Any]) -> str | None:
    state: dict[str, Any] = {}
    for key in ("importance", "policyViolation", "deletedDateTime"):
        if key in message:
            state[key] = message[key]
    if "hasUnreadMessages" in chat:
        state["chatHasUnreadMessages"] = chat["hasUnreadMessages"]
    if not state:
        return None
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        user = value.get("user") if isinstance(value.get("user"), dict) else value
        return (
            user.get("displayName")
            or user.get("email")
            or user.get("userPrincipalName")
            or user.get("id")
        )
    return str(value)


def _labels(message: dict[str, Any], chat: dict[str, Any]) -> list[str]:
    labels: list[str] = ["teams"]
    chat_type = chat.get("chatType")
    if chat_type:
        labels.append(str(chat_type))
    importance = message.get("importance")
    if importance:
        labels.append(str(importance))
    return labels


def _raw_message(record: dict[str, Any]) -> dict[str, Any]:
    raw = dict(record)
    raw.pop("_task_graph_chat", None)
    return raw
