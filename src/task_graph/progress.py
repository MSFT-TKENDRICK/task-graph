"""Progress reporting for work that takes long enough to need it.

A sync spends most of its time waiting on `agency mcp` subprocesses, one source
at a time. Without reporting that is indistinguishable from a hang, which is
exactly how it felt: the prompt stopped responding and nothing said why.

The contract is deliberately one-way and cheap. Long-running code calls
:meth:`Reporter.phase`, :meth:`Reporter.advance` and :meth:`Reporter.log`
without knowing or caring whether anything is listening; the default sink
discards everything, so library and test use pay almost nothing and no code path
has to branch on "is anyone watching".

Events cross a process boundary as NDJSON, one object per line, appended and
flushed immediately. A job runs in a child process (see :mod:`task_graph.jobs`)
and the follower tails the file, so a partially written last line is normal --
readers skip anything that does not parse rather than treating it as corruption.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Set by the job child so any command it runs reports without being asked to.
ENV_PROGRESS_FILE = "TASK_GRAPH_PROGRESS_FILE"

PHASE = "phase"
ADVANCE = "advance"
LOG = "log"


@dataclass(frozen=True)
class ProgressEvent:
    """One thing that happened, flat enough to survive JSON round-tripping."""

    kind: str
    phase: str = ""
    message: str = ""
    current: int = 0
    total: int = 0
    at: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "phase": self.phase,
                "message": self.message,
                "current": self.current,
                "total": self.total,
                "at": self.at or datetime.now(UTC).isoformat(),
            },
            default=str,
        )

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> ProgressEvent:
        return cls(
            kind=str(data.get("kind", LOG)),
            phase=str(data.get("phase", "")),
            message=str(data.get("message", "")),
            current=int(data.get("current", 0) or 0),
            total=int(data.get("total", 0) or 0),
            at=str(data.get("at", "")),
        )


Sink = Callable[[ProgressEvent], None]


def null_sink(event: ProgressEvent) -> None:
    """Discard everything. The default, so nobody has to check for None."""


class FileSink:
    """Append events to an NDJSON file, flushed per line.

    Flushing every line is the point: a follower in another process is reading
    this while it is written, and a buffered writer would make progress arrive
    in bursts at exactly the moments it is least useful.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, event: ProgressEvent) -> None:
        line = event.to_json()
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Losing progress must never fail the work it describes.
                pass


class Reporter:
    """What long-running code calls.

    Tracks just enough state to keep callers simple: ``advance()`` with no
    arguments means "one more of whatever phase we are in", so loops do not have
    to carry counters of their own.
    """

    def __init__(self, sink: Sink | None = None) -> None:
        self._sink: Sink = sink or null_sink
        self._phase = ""
        self._current = 0
        self._total = 0

    @property
    def current_phase(self) -> str:
        return self._phase

    def phase(self, name: str, *, total: int = 0, message: str = "") -> None:
        """Start a new phase, resetting the counter."""
        self._phase = name
        self._current = 0
        self._total = max(0, int(total))
        self._emit(PHASE, message=message)

    def advance(self, n: int = 1, *, message: str = "") -> None:
        self._current += n
        self._emit(ADVANCE, message=message)

    def log(self, message: str) -> None:
        self._emit(LOG, message=message)

    def _emit(self, kind: str, *, message: str) -> None:
        event = ProgressEvent(
            kind=kind,
            phase=self._phase,
            message=message,
            current=self._current,
            total=self._total,
            at=datetime.now(UTC).isoformat(),
        )
        try:
            self._sink(event)
        except Exception:  # noqa: BLE001 - reporting must not break the work
            pass


class ConsoleSink:
    """Render progress as one line that rewrites itself.

    Only ever writes to a terminal. Under a pipe -- which is where a job's
    output goes -- a progress bar would just be thousands of near-identical
    lines in the log, so there it does nothing and the NDJSON file carries the
    same information in a form built for reading back.

    ASCII only, and the line is truncated to the terminal width: a line longer
    than the window wraps, and a wrapped line cannot be erased by a carriage
    return, which leaves a trail of half-drawn progress behind the output.
    """

    #: Deliberately not braille or block characters; this renders on Windows
    #: consoles that are still on a non-UTF-8 code page.
    FRAMES = "|/-\\"

    def __init__(self, stream: Any = None, *, enabled: bool | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = self._is_tty() if enabled is None else enabled
        self._state = ProgressState()
        self._frame = 0
        self._width = 0
        self._started = time.monotonic()

    def _is_tty(self) -> bool:
        isatty = getattr(self._stream, "isatty", None)
        if isatty is None:
            return False
        try:
            return bool(isatty())
        except ValueError:
            return False

    def __call__(self, event: ProgressEvent) -> None:
        if not self._enabled:
            return
        self._state = fold([event], self._state)
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self._write(self._render())

    def _render(self) -> str:
        elapsed = time.monotonic() - self._started
        spinner = self.FRAMES[self._frame]
        line = f"{spinner} {self._state.describe()} ({elapsed:.0f}s)"
        limit = max(20, _terminal_width() - 1)
        if len(line) > limit:
            line = line[: limit - 3] + "..."
        return line

    def _write(self, line: str) -> None:
        pad = " " * max(0, self._width - len(line))
        self._width = len(line)
        try:
            self._stream.write("\r" + line + pad)
            self._stream.flush()
        except (OSError, ValueError):
            self._enabled = False

    def clear(self) -> None:
        """Erase the progress line, leaving the cursor at the start of it."""
        if not self._enabled or not self._width:
            return
        try:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
        except (OSError, ValueError):
            pass
        self._width = 0


def _terminal_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        return default


def null_reporter() -> Reporter:
    return Reporter()


def file_reporter(path: str | Path) -> Reporter:
    return Reporter(FileSink(path))


def reporter_from_env(environ: dict[str, str] | None = None) -> Reporter:
    """Report to wherever the job runner asked, or nowhere at all."""
    env = environ if environ is not None else dict(os.environ)
    path = (env.get(ENV_PROGRESS_FILE) or "").strip()
    return file_reporter(path) if path else null_reporter()


# ------------------------------------------------------------------- reading


@dataclass(frozen=True)
class ProgressState:
    """The latest position, for rendering one line of status."""

    phase: str = ""
    message: str = ""
    current: int = 0
    total: int = 0

    @property
    def fraction(self) -> float | None:
        if self.total <= 0:
            return None
        return min(1.0, max(0.0, self.current / self.total))

    def describe(self) -> str:
        """A compact human summary, e.g. ``sources 2/6 mail``."""
        parts = [self.phase or "working"]
        if self.total > 0:
            parts.append(f"{self.current}/{self.total}")
        elif self.current > 0:
            parts.append(str(self.current))
        if self.message:
            parts.append(self.message)
        return " ".join(parts)


def iter_events(path: str | Path, *, start: int = 0) -> tuple[list[ProgressEvent], int]:
    """Read events appended since byte offset ``start``.

    Returns the events and the offset to resume from. A trailing partial line is
    left unconsumed so the next call sees it whole, which is the normal state of
    a file being appended to by another process.
    """
    file_path = Path(path)
    if not file_path.exists():
        return [], start
    try:
        with file_path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return [], start

    consumed = start
    events: list[ProgressEvent] = []
    for raw in data.splitlines(keepends=True):
        if not raw.endswith((b"\n", b"\r\n")):
            break  # partial line; wait for the rest
        consumed += len(raw)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(ProgressEvent.from_mapping(payload))
    return events, consumed


def fold(events: Iterator[ProgressEvent] | list[ProgressEvent], state: ProgressState | None = None):
    """Apply events to a :class:`ProgressState`."""
    current = state or ProgressState()
    for event in events:
        if event.kind == PHASE:
            current = ProgressState(
                phase=event.phase, message=event.message, current=0, total=event.total
            )
        elif event.kind == ADVANCE:
            current = replace(
                current,
                phase=event.phase or current.phase,
                current=event.current,
                total=event.total or current.total,
                message=event.message or current.message,
            )
        elif event.kind == LOG and event.message:
            current = replace(current, message=event.message)
    return current
