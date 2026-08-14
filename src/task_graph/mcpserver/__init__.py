"""MCP server entry point for task-graph."""

from __future__ import annotations

from typing import Any


def main(*args: Any, **kwargs: Any) -> None:
    from task_graph.mcpserver.server import main as server_main

    return server_main(*args, **kwargs)

__all__ = ["main"]
