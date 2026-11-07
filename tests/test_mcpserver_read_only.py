from __future__ import annotations

import mcp.types as mcp_types
import pytest

from task_graph.mcpserver import server, tools

MUTATING = {
    "sync_sources",
    "approve_merge",
    "reject_merge",
    "propose_remediations",
    "preview_action",
    "approve_action",
    "execute_action",
    "record_correction",
    "learn_from_corrections",
    "run_doctor",
}


def test_read_only_set_is_a_strict_subset_of_tools():
    names = {tool.name for tool in tools.TOOLS}
    assert tools.READ_ONLY_TOOL_NAMES < names
    assert {tool.name for tool in tools.READ_ONLY_TOOLS} == tools.READ_ONLY_TOOL_NAMES


def test_read_only_set_excludes_every_mutating_tool():
    assert not (tools.READ_ONLY_TOOL_NAMES & MUTATING)
    # Every tool is classified, so a new tool cannot silently become read-only.
    assert {tool.name for tool in tools.TOOLS} == tools.READ_ONLY_TOOL_NAMES | MUTATING


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), (" on ", True), ("0", False), ("", False)],
)
def test_read_only_from_env(value, expected):
    assert server.read_only_from_env({server.READ_ONLY_ENV: value}) is expected
    assert server.read_only_from_env({}) is False


def test_registered_tools_respects_mode():
    assert server.registered_tools() == tools.TOOLS
    assert server.registered_tools(read_only=True) == tools.READ_ONLY_TOOLS


async def _list(srv) -> list[str]:
    handler = srv.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    return [tool.name for tool in result.root.tools]


async def _call(srv, name: str) -> mcp_types.CallToolResult:
    handler = srv.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments={}),
    )
    return (await handler(request)).root


async def test_read_only_server_lists_only_read_only_tools():
    names = await _list(server.create_server(read_only=True))
    assert set(names) == tools.READ_ONLY_TOOL_NAMES


async def test_default_server_lists_all_tools(monkeypatch):
    monkeypatch.delenv(server.READ_ONLY_ENV, raising=False)
    names = await _list(server.create_server())
    assert set(names) == {tool.name for tool in tools.TOOLS}


async def test_env_enables_read_only(monkeypatch):
    monkeypatch.setenv(server.READ_ONLY_ENV, "1")
    names = await _list(server.create_server())
    assert set(names) == tools.READ_ONLY_TOOL_NAMES


@pytest.mark.parametrize("name", sorted(MUTATING))
async def test_read_only_server_refuses_mutating_calls(name):
    result = await _call(server.create_server(read_only=True), name)
    assert result.isError is True
    assert result.structuredContent == {
        "ok": False,
        "error": {"type": "read_only", "message": f"Tool {name} is disabled in read-only mode"},
    }


async def test_unknown_tool_is_still_unknown_in_read_only_mode():
    result = await _call(server.create_server(read_only=True), "nope")
    assert result.isError is True
    assert result.structuredContent["error"]["type"] == "unknown_tool"
