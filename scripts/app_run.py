"""Open an interactive `tg` shell -- the GitHub Copilot app's Run button.

The app runs a project's Run script through the platform shell with a handful of
``COPILOT_*`` variables set. That contract is easy to get wrong from a shell
one-liner -- ``.venv`` is not on ``PATH``, and the app's shell is nobody's shell
-- so the configured command is just ``python scripts/app_run.py`` and every
decision that differs per platform is made here, in one place, in Python.

**Where this runs decides whether a prompt can be answered**, and getting it
wrong strands the script at a ``tg>`` nobody can see:

- A real terminal (the app's terminal canvas, an IDE terminal, any shell) gives
  every stream a tty, and you get the interactive shell.
- The app's script runner pipes stdout into its log pane. Its *stdin* still
  answers ``isatty()``, which is why checking stdin alone reports a typist who
  does not exist; stdout being a pipe is the honest signal. There, this prints
  a triage summary instead -- the same code path, one process, and a log that
  proves the whole chain works -- plus the one command that gets you the real
  shell.

Set ``TASK_GRAPH_RUN_INTERACTIVE=1`` to insist on the prompt anyway, or ``0`` to
force the summary.

Stdlib only, and no imports from ``task_graph``: this has to run correctly
before anything is installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from app_setup import ensure_environment, step  # noqa: E402

#: What the log pane shows instead of a prompt. Cheap, offline, and enough to
#: tell you whether the graph is healthy and what it thinks you should do.
SUMMARY_COMMANDS = ("doctor", "triage")


def _tty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except ValueError:  # closed stream
        return False


def is_interactive() -> bool:
    """Decide whether a ``tg>`` prompt would ever be answered."""
    override = os.environ.get("TASK_GRAPH_RUN_INTERACTIVE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return _tty(sys.stdin) and _tty(sys.stdout)


def shell_hint() -> str:
    if os.name == "nt":
        return ".\\run.ps1 shell"
    return "./.venv/bin/tg shell"


def main() -> int:
    python = ensure_environment()
    base = [str(python), "-m", "task_graph.cli", "shell"]

    if is_interactive():
        step("Starting interactive tg shell")
        return subprocess.call(base, cwd=str(ROOT))

    step("This pane cannot take input, so here is the summary instead")
    print(
        f"For the interactive shell, run `{shell_hint()}` in a terminal "
        "(the app's Terminal panel works).\n",
        flush=True,
    )
    command = base + [arg for name in SUMMARY_COMMANDS for arg in ("-c", name)]
    return subprocess.call(command, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
