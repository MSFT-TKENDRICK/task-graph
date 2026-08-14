"""Mail ingestion through Agency's ambient Microsoft 365 auth."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from task_graph.connectors.ado import _normalise_payload, _pick_tool, _records, _tool_names
from task_graph.connectors.base import (
    ConnectorStatus,
    SourceItem,
    extract_external_refs,
    mail_uri,
    parse_datetime,
)
from task_graph.connectors.mcp_client import AgencyMcpClient
from task_graph.ontology.types import SourceKind


class MailConnector:
    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory or (lambda: AgencyMcpClient("mail"))
        self.discovered_tools: list[str] = []

    @property
    def kind(self) -> SourceKind:
        return SourceKind.MAIL

    @property
    def name(self) -> str:
        return "Mail"

    def is_available(self) -> ConnectorStatus:
        try:
            client = self._client_factory()
            if hasattr(client, "is_available"):
                return client.is_available()
            with client as opened:
                tools = opened.list_tools()
            return ConnectorStatus(True, f"Found {len(tools)} mail MCP tools.")
        except Exception as exc:
            return ConnectorStatus(
                False,
                f"Mail MCP server is not available: {exc}",
                "Run `agency mcp mail` to inspect local Agency configuration.",
            )

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        with self._client_factory() as client:
            tools = _tool_names(client.list_tools())
            self.discovered_tools = tools
            tool = _pick_tool(tools, required=("mail",), preferred=("flag", "message", "list"))
            args: dict[str, Any] = {
                "filter": "flagged or actionable messages",
                "top": 100,
            }
            if since:
                args["since"] = since.isoformat()
            payload = _normalise_payload(client.call_tool(tool, args))
        for record in _records(payload, "messages", "items", "value", "result"):
            if is_actionable_message(record):
                yield self._map_message(record)

    def _map_message(self, record: dict[str, Any]) -> SourceItem:
        body = _body_text(record)
        message_id = (
            record.get("internetMessageId")
            or record.get("messageId")
            or record.get("id")
            or record.get("conversationId")
        )
        sender = record.get("from") or record.get("sender")
        return SourceItem(
            source=SourceKind.MAIL,
            source_uri=mail_uri(str(message_id)),
            title=str(record.get("subject") or "(no subject)"),
            body=body,
            url=record.get("webLink") or record.get("url"),
            source_state=_flag_state(record),
            owner=_email_identity(sender),
            assignees=[],
            created_at=parse_datetime(
                record.get("createdDateTime") or record.get("receivedDateTime")
            ),
            updated_at=parse_datetime(
                record.get("lastModifiedDateTime") or record.get("receivedDateTime")
            ),
            due_at=parse_datetime(record.get("dueDateTime")),
            labels=_categories(record),
            external_refs=extract_external_refs(f"{record.get('subject') or ''}\n{body}"),
            raw=record,
        )


def is_actionable_message(message: dict[str, Any]) -> bool:
    """Conservative heuristic kept isolated for later user-steered tuning."""

    subject = str(message.get("subject") or "")
    body = _body_text(message)
    text = f"{subject}\n{body}".lower()
    sender = str(message.get("from") or message.get("sender") or "").lower()
    if any(token in text for token in ("newsletter", "unsubscribe", "digest")):
        return False
    if subject.lower().startswith(("fyi", "newsletter", "digest")):
        return False
    if "no action required" in text:
        return False
    if _is_flagged(message):
        return True
    direct_to_me = bool(message.get("directToMe") or message.get("isDirectToMe"))
    asks = (
        "?" in text
        or re.search(r"\b(can you|could you|please|need you to|please review|follow up)\b", text)
        is not None
    )
    if direct_to_me and asks and "noreply" not in sender and "no-reply" not in sender:
        return True
    return False


def _is_flagged(message: dict[str, Any]) -> bool:
    flag = message.get("flag") or {}
    if isinstance(flag, dict):
        return str(flag.get("flagStatus") or "").lower() in {"flagged", "complete"}
    return bool(message.get("flagged") or message.get("isFlagged"))


def _flag_state(message: dict[str, Any]) -> str | None:
    flag = message.get("flag")
    if isinstance(flag, dict) and flag.get("flagStatus"):
        return str(flag["flagStatus"])
    if message.get("flagged") or message.get("isFlagged"):
        return "flagged"
    return None


def _body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or message.get("bodyPreview") or ""
    if isinstance(body, dict):
        return str(body.get("content") or body.get("text") or "")
    return str(body)


def _email_identity(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        email = value.get("emailAddress")
        if isinstance(email, dict):
            return email.get("address") or email.get("name")
        return value.get("address") or value.get("name")
    return str(value)


def _categories(message: dict[str, Any]) -> list[str]:
    categories = message.get("categories") or []
    return [str(category) for category in categories] if isinstance(categories, list) else []
