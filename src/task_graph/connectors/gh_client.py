"""Write-side client for GitHub, backed by the ``gh`` CLI.

Most remediation targets are reached through ``agency mcp <source>``, but there
is no Agency MCP server for GitHub — the read connector uses the already
authenticated ``gh`` CLI, and so must the write path. This adapter presents the
same duck-typed ``call_tool(tool, args)`` surface the executors expect, so
``pipeline.remediation`` needs no special case.

Nothing here runs unless an approval has already been granted; see
``pipeline.approval.ApprovalQueue.execute_approved``.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from task_graph.connectors.base import ConnectorError

_TIMEOUT_SECONDS = 60


class GitHubCliClient:
    """Executes GitHub mutations via ``gh``."""

    name = "github-cli"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or shutil.which("gh") or "gh"

    def _run(self, args: list[str]) -> str:
        try:
            completed = subprocess.run(
                [self._executable, *args],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConnectorError(
                "gh CLI not found; install GitHub CLI and run `gh auth login`."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ConnectorError(
                f"gh timed out after {_TIMEOUT_SECONDS}s: {' '.join(args)}"
            ) from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ConnectorError(f"gh {' '.join(args)} failed: {detail}")
        return (completed.stdout or "").strip()

    def call_tool(self, tool: str, args: dict[str, Any]) -> str:
        handler = _TOOLS.get(tool)
        if handler is None:
            raise ConnectorError(f"GitHubCliClient cannot perform {tool!r}")
        return handler(self, args)

    # ------------------------------------------------------------- mutations

    def comment_github(self, owner_repo: str, number: int | str, body: str, **_: Any) -> str:
        self._run(
            ["issue", "comment", str(number), "--repo", owner_repo, "--body", body]
        )
        return f"Commented on {owner_repo}#{number}"

    def close_github_issue(
        self, owner_repo: str, number: int | str, reason: str | None = None, **_: Any
    ) -> str:
        args = ["issue", "close", str(number), "--repo", owner_repo]
        if reason:
            args += ["--comment", reason]
        self._run(args)
        return f"Closed {owner_repo}#{number}"


_TOOLS = {
    "github_comment": lambda c, a: c.comment_github(**a),
    "github_close_issue": lambda c, a: c.close_github_issue(**a),
}
