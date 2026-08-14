"""Stdio MCP server for task-graph.

This is the primary interface: the user drives the whole system conversationally
from GitHub Copilot CLI or Microsoft Agency CLI.

Transport is stdio only. The MCP SDK is pinned below 1.20 because from 1.20 it
depends on ``pyjwt[crypto]`` -> ``cryptography``, which publishes no win_arm64
wheel and is imported unconditionally at package import — making the whole SDK
unimportable on Windows on ARM.

Approval and execution are deliberately separate tools. No single tool call can
both authorise and perform a change to a source system.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from task_graph.app import TaskGraphApp
from task_graph.mcpserver.tools import TOOLS, TOOLS_BY_NAME, ToolSpec

SDK_API = "lowlevel.Server + stdio_server"

INSTRUCTIONS = (
    "Local-first unified task graph over mail, GitHub, ADO, Teams and MSX. "
    "It ingests work from every source, unifies items that track the same work, "
    "ranks them, and proposes remediation actions. "
    "Approval and execution are separate: approve_action only grants permission, "
    "and execute_action requires a prior grant. Never assume an approved action "
    "has already run."
)


def registered_tools() -> tuple[ToolSpec, ...]:
    return TOOLS


@asynccontextmanager
async def _lifespan(_server: Any):
    """Open one app for the server's lifetime; weights are saved on close."""
    app = await asyncio.to_thread(TaskGraphApp)
    try:
        yield app
    finally:
        await asyncio.to_thread(app.close)


def _invoke(
    handler: Callable[..., dict[str, Any]], app: TaskGraphApp, arguments: dict[str, Any]
) -> dict[str, Any]:
    return handler(app, **arguments)


def create_server() -> Server:
    """Build the server. Opening the app is deferred to the lifespan."""
    server: Server = Server(
        "task-graph",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=_lifespan,
    )

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
            )
            for tool in TOOLS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> mcp_types.CallToolResult:
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            result: dict[str, Any] = {
                "ok": False,
                "error": {"type": "unknown_tool", "message": f"Unknown tool: {name}"},
            }
        else:
            app = server.request_context.lifespan_context
            # TaskGraphApp is synchronous and a sync can take seconds; running
            # it inline would stall the protocol loop.
            result = await asyncio.to_thread(_invoke, tool.handler, app, arguments or {})

        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps(result, sort_keys=True, default=str)
                )
            ],
            structuredContent=result,
            isError=not bool(result.get("ok", False)),
        )

    return server


async def _run_stdio() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Synchronous console entry point for ``task-graph-mcp``."""
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
