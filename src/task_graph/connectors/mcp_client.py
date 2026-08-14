"""Synchronous facade for Agency's ambient-auth MCP servers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import Container, Mapping
from types import TracebackType
from typing import Any, ClassVar

from task_graph.connectors.base import ConnectorError, ConnectorStatus


class AgencyMcpClient:
    """Spawn ``agency mcp <server>`` so connectors never handle credentials."""

    #: Availability probes get a much shorter budget than real calls. A probe
    #: that takes 30s makes `tg doctor` unusable, and "slow to answer" is
    #: indistinguishable from "unavailable" for preflight purposes.
    PROBE_TIMEOUT_SECONDS = 6.0

    #: Probes are cached per server for the process lifetime. `preflight()`
    #: asks every connector at once, and spawning the same server repeatedly
    #: to ask the same question is pure latency.
    _probe_cache: ClassVar[dict[str, ConnectorStatus]] = {}

    def __init__(self, server_name: str, *, timeout_seconds: float = 30.0) -> None:
        self.server_name = server_name
        self.timeout_seconds = timeout_seconds
        self._portal_cm: Any = None
        self._portal: Any = None
        self._stdio_cm: Any = None
        self._session_cm: Any = None
        self._session: Any = None
        self._errlog: Any = None

    @classmethod
    def clear_probe_cache(cls) -> None:
        cls._probe_cache.clear()

    def __enter__(self) -> AgencyMcpClient:
        if self._session is not None:
            return self
        if shutil.which("agency") is None:
            raise ConnectorError(
                "Agency CLI was not found on PATH. Install Agency or add it to PATH."
            )
        try:
            self._start_session()
        except Exception as exc:
            detail = self._drain_stderr()
            self._teardown()
            if isinstance(exc, ConnectorError):
                raise
            message = f"Failed to start agency mcp {self.server_name}: {exc}"
            if detail:
                message = f"{message} ({detail})"
            raise ConnectorError(message) from exc
        return self

    def _start_session(self) -> None:
        """Open the MCP session inside a single anyio task.

        The SDK's context managers own anyio cancel scopes, which must be
        entered and exited from the *same* task. Driving them with separate
        ``run_until_complete`` calls puts enter and exit in different tasks and
        fails teardown with "attempted to exit cancel scope in a different
        task". A blocking portal keeps the whole lifecycle on one task in a
        dedicated thread, while still presenting a synchronous API here.

        The child's stderr is captured to a temp file rather than inherited.
        Agency writes banners and its own usage errors there, and letting them
        through turns a cleanly-reported connector failure into console noise;
        capturing it also lets the real reason go into the raised error.
        """
        try:
            from anyio.from_thread import start_blocking_portal
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:
            raise ConnectorError(f"MCP Python SDK could not be imported: {exc}") from exc

        self._errlog = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in _teardown
            mode="w+", encoding="utf-8", suffix=".stderr", delete=False
        )

        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()

        params = StdioServerParameters(command="agency", args=["mcp", self.server_name])
        self._stdio_cm = self._portal.wrap_async_context_manager(
            stdio_client(params, errlog=self._errlog)
        )
        read_stream, write_stream = self._stdio_cm.__enter__()

        self._session_cm = self._portal.wrap_async_context_manager(
            ClientSession(read_stream, write_stream)
        )
        self._session = self._session_cm.__enter__()
        self._run(self._session.initialize)

    def _drain_stderr(self, limit: int = 400) -> str:
        """Tail of what the child process complained about, for error messages."""
        if self._errlog is None:
            return ""
        try:
            self._errlog.flush()
            self._errlog.seek(0)
            text = self._errlog.read().strip()
        except (OSError, ValueError):
            return ""
        return text[-limit:]

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._teardown()

    def _teardown(self) -> None:
        """Best-effort shutdown. A failure to stop must not mask a real error."""
        for cm in (self._session_cm, self._stdio_cm, self._portal_cm):
            if cm is None:
                continue
            try:
                cm.__exit__(None, None, None)
            except BaseException:  # noqa: BLE001 - cleanup is best effort
                pass
        if self._errlog is not None:
            path = self._errlog.name
            try:
                self._errlog.close()
                os.unlink(path)
            except OSError:
                pass
        self._session = None
        self._session_cm = None
        self._stdio_cm = None
        self._portal = None
        self._portal_cm = None
        self._errlog = None

    def is_available(self) -> ConnectorStatus:
        cached = self._probe_cache.get(self.server_name)
        if cached is not None:
            return cached

        status = self._probe()
        self._probe_cache[self.server_name] = status
        return status

    def _probe(self) -> ConnectorStatus:
        if shutil.which("agency") is None:
            return ConnectorStatus(
                False,
                "Agency CLI was not found on PATH.",
                "Install Agency CLI and ensure `agency` is on PATH.",
            )
        probe = AgencyMcpClient(self.server_name, timeout_seconds=self.PROBE_TIMEOUT_SECONDS)
        try:
            with probe as client:
                tools = client.list_tools()
        except Exception as exc:
            return ConnectorStatus(
                False,
                f"agency mcp {self.server_name} is not available: {exc}",
                f"Run `agency mcp {self.server_name}` to inspect the server.",
            )
        return ConnectorStatus(
            True, f"Found {len(tools)} tools on agency mcp {self.server_name}."
        )

    def list_tools(self) -> list[dict[str, Any]]:
        if self._session is None:
            with self as client:
                return client.list_tools()
        return self._run(self._list_tools_async)

    def call_tool(self, name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            with self as client:
                return client.call_tool(name, args)
        result = self._run(self._call_tool_async, name, dict(args or {}))
        parsed = extract_json_from_mcp_result(result)
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}

    async def _list_tools_async(self) -> list[dict[str, Any]]:
        result = await self._session.list_tools()
        tools = getattr(result, "tools", result)
        return [_tool_to_dict(tool) for tool in tools]

    async def _call_tool_async(self, name: str, args: dict[str, Any]) -> Any:
        tool_names = {tool.get("name") for tool in await self._list_tools_async()}
        if name not in tool_names:
            raise ConnectorError(f"MCP tool `{name}` not found on agency mcp {self.server_name}.")
        result = await self._session.call_tool(name, args)
        if bool(getattr(result, "isError", False) or getattr(result, "is_error", False)):
            raise ConnectorError(f"MCP tool `{name}` returned an error: {result}")
        return result

    def _run(self, async_fn: Any, *args: Any) -> Any:
        if self._portal is None:
            raise ConnectorError("MCP client is not open.")
        try:
            return self._portal.call(
                lambda: asyncio.wait_for(async_fn(*args), timeout=self.timeout_seconds)
            )
        except TimeoutError as exc:
            raise ConnectorError(
                f"Timed out calling agency mcp {self.server_name} after "
                f"{self.timeout_seconds:g}s."
            ) from exc
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(f"agency mcp {self.server_name} failed: {exc}") from exc


#: Verbs that mark an MCP tool as mutating. Ingest is read-only, so calling one
#: is always a bug — and a dangerous one, since it would mean a routine sync
#: writing to a source system.
#:
#: Matching is token-based rather than substring-based because MCP servers do
#: not agree on naming: Agency's ADO server uses ``wit_work_item_write`` while
#: its Planner server uses ``CreateTask``. A substring check for ``_write``
#: silently allows every PascalCase mutation, which is precisely the hole this
#: closes.
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        # Generic mutations.
        "create", "update", "delete", "remove", "write", "upsert", "upload",
        "add", "set", "post", "put", "patch", "send", "close", "archive",
        "assign", "move", "rename", "edit", "modify", "publish", "submit",
        "approve", "reject", "merge", "revert", "reset", "clear", "purge",
        "drop", "insert", "save", "link", "unlink", "complete", "start",
        # Calendar and mail verbs that mutate without looking like it: replying
        # to an invite changes state on other people's calendars.
        "cancel", "accept", "decline", "tentatively", "forward", "invite",
        "schedule", "book", "reply", "respond", "dismiss", "snooze", "flag",
        "mark", "share",
    }
)

_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")


def tool_name_tokens(name: str) -> list[str]:
    """Split a tool name into lowercase words.

    Handles ``snake_case``, ``camelCase``, ``PascalCase`` and acronyms, so
    ``wit_work_item_write`` and ``CreateTask`` both tokenise usefully.
    """
    return [token.lower() for token in _TOKEN.findall(name.replace("_", " "))]


def is_mutating_tool(name: str) -> bool:
    return any(token in MUTATING_VERBS for token in tool_name_tokens(name))


def assert_read_only(name: str, *, allow: Container[str] = frozenset()) -> None:
    """Refuse to call ``name`` if it looks like a mutation.

    Deliberately errs towards refusing: a false refusal is loud and easy to
    override via ``allow``, whereas a false permit means an unattended sync
    writing to ADO, Planner or MSX.
    """
    if name in allow:
        return
    if is_mutating_tool(name):
        raise RuntimeError(f"Refusing to call mutating tool {name!r} during a read-only sync.")


def extract_json_from_mcp_result(result: Any) -> Any:
    """Accept structured content or JSON embedded in MCP text blocks."""

    for attr in ("structured_content", "structuredContent"):
        structured = _get_value(result, attr)
        if structured is not None:
            return structured

    content = _get_value(result, "content")
    if content is None and isinstance(result, (dict, list)):
        return result
    if not isinstance(content, list):
        raise ConnectorError("MCP result did not include structured content or text blocks.")

    texts: list[str] = []
    for block in content:
        block_type = _get_value(block, "type")
        text = _get_value(block, "text")
        if (block_type in (None, "text")) and isinstance(text, str):
            texts.append(text)
    for text in texts:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    joined = "\n".join(texts).strip()
    if joined:
        return {"text": joined}
    raise ConnectorError("MCP result contained no parseable JSON content.")


def _get_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    if isinstance(tool, Mapping):
        return dict(tool)
    if hasattr(tool, "model_dump"):
        return tool.model_dump()
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", None),
        "inputSchema": getattr(tool, "inputSchema", None),
    }
