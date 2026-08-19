"""Background jobs: the slow half of the CLI, made watchable and killable.

Everything that can block lives here -- sync, doctor, rebuild, learn -- because
all of it is dominated by waiting on `agency mcp` subprocesses, and all of it
used to freeze the prompt with nothing on screen to say why.

**Jobs are child processes, not threads.** Two reasons, and the second is the
one that decides it:

- activegraph's event store opens SQLite without ``check_same_thread=False``, so
  a worker thread cannot append events at all.
- A thread blocked inside an MCP call cannot be interrupted. Cooperative
  cancellation only works if the work checks a flag, and this work spends its
  time inside somebody else's blocking read. Killing the process tree is the
  only cancellation that actually stops an in-flight `agency mcp` call -- and it
  reaps the grandchild too, which a thread-based design leaves running.

Both databases are in WAL mode, so a job writing does not block the prompt
reading, and `triage` stays instant while a sync runs.

State lives in ``$TASK_GRAPH_HOME/jobs/<id>/`` -- a numeric id you can actually
type, plus ``meta.json``, ``log.txt`` and ``progress.ndjson``. Keeping it on
disk rather than in memory is what lets a job outlive the shell that started it,
and lets the one-shot CLI and the MCP server see the same jobs.

The child writes its own terminal state, so nothing depends on the parent still
being alive to record the outcome. When a process dies without recording one --
killed, crashed, machine rebooted -- readers reap it as ``lost`` rather than
showing a job that is running forever.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from task_graph.progress import ENV_PROGRESS_FILE, ProgressState, fold, iter_events

STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_LOST = "lost"

TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED, STATE_LOST})

#: Commands worth running as a job. Everything else in the CLI is a local SQLite
#: read that finishes in single-digit milliseconds, and making those wait for a
#: process spawn would be strictly worse than what they do now.
JOB_COMMANDS = frozenset({"sync", "doctor", "rebuild", "learn"})

_META = "meta.json"
_LOG = "log.txt"
_PROGRESS = "progress.ndjson"
_CANCEL = "cancel"

#: How long to let a killed tree wind down before reporting it stopped.
_KILL_GRACE_SECONDS = 3.0

#: How long a job may sit with no pid before it counts as a failed spawn.
_SPAWN_GRACE_SECONDS = 30.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def is_job_command(argv: list[str]) -> bool:
    """Decide whether an argument list should run as a job.

    Group options may come first (``--json sync``), so this looks for the first
    bare word. ``approve action --execute`` is included because executing a
    remediation calls out to a source system and can block just as long as a
    sync; plain ``approve`` only writes locally.
    """
    words = [arg for arg in argv if not arg.startswith("-")]
    if not words:
        return False
    if words[0] in JOB_COMMANDS:
        return True
    return words[0] == "approve" and "--execute" in argv


@dataclass
class Job:
    """One background run, reconstructed from its directory."""

    id: str
    dir: Path
    argv: list[str] = field(default_factory=list)
    state: str = STATE_RUNNING
    pid: int | None = None
    start_token: str = ""
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    exit_code: int | None = None
    error: str = ""

    @property
    def meta_path(self) -> Path:
        return self.dir / _META

    @property
    def log_path(self) -> Path:
        return self.dir / _LOG

    @property
    def progress_path(self) -> Path:
        return self.dir / _PROGRESS

    @property
    def cancel_path(self) -> Path:
        return self.dir / _CANCEL

    @property
    def label(self) -> str:
        """The command as typed, for display."""
        return " ".join(self.argv) or self.id

    @property
    def is_running(self) -> bool:
        return self.state not in TERMINAL_STATES

    @property
    def ok(self) -> bool:
        return self.state == STATE_SUCCEEDED

    def progress(self) -> ProgressState:
        events, _ = iter_events(self.progress_path)
        return fold(events)

    def duration_seconds(self) -> float:
        start = _parse(self.started_at) or _parse(self.created_at)
        if start is None:
            return 0.0
        end = _parse(self.ended_at) or datetime.now(UTC)
        return max(0.0, (end - start).total_seconds())

    def read_log(self, *, tail: int | None = None) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if tail is None:
            return text
        lines = text.splitlines()
        return "\n".join(lines[-tail:])

    def to_dict(self) -> dict[str, Any]:
        progress = self.progress()
        return {
            "id": self.id,
            "command": self.label,
            "state": self.state,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "seconds": round(self.duration_seconds(), 1),
            "phase": progress.phase,
            "progress": progress.describe(),
            "error": self.error,
        }


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------------ liveness


def process_start_token(pid: int | None) -> str | None:
    """A value that changes when ``pid`` is reused by a different process.

    A bare pid does not identify a process. After a reboot -- a case this
    module explicitly expects -- a job directory survives with
    ``state=running, pid=N``, and N is now something else entirely, very often
    a low-numbered system process. Trusting liveness alone would list that as a
    running job and, worse, ``cancel`` would force-kill its whole tree.

    So each job records the start time of the process it spawned, and liveness
    is only believed when that still matches. Where the platform will not tell
    us (macOS has no ``/proc``), this returns ``None`` and callers fall back to
    the pid alone rather than refusing to work.
    """
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        return _windows_start_token(pid)
    return _proc_start_token(pid)


def _windows_start_token(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}"
    finally:
        kernel32.CloseHandle(handle)


def _proc_start_token(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks since boot."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces and
    # parentheses, so everything is counted from the *last* closing bracket.
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def pid_alive(pid: int | None, start_token: str | None = None) -> bool:
    """Report whether ``pid`` is still running, and still the same process.

    ``os.kill(pid, 0)`` is the usual trick and is *not* portable here: on
    Windows :func:`os.kill` terminates the target rather than probing it, so
    asking "are you alive" would kill the job. Windows goes through
    ``GetExitCodeProcess`` instead.
    """
    if not pid or pid <= 0:
        return False
    if not _pid_exists(pid):
        return False
    if start_token:
        current = process_start_token(pid)
        if current is not None and current != start_token:
            return False  # the pid was recycled
    return True


def _pid_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill_tree(pid: int, *, owned: subprocess.Popen[Any] | None = None) -> None:
    """Kill ``pid`` and everything it started.

    The grandchildren are the point: a sync's real cost is the `agency mcp`
    servers it spawned, and killing only the direct child would orphan them
    still holding their MCP sessions open.

    ``owned`` is the handle for a child we spawned ourselves, if we still have
    it. Waiting on it is what actually reaps the process: without that, a dead
    POSIX child lingers as a zombie that still answers ``kill(pid, 0)``, so the
    grace loop below would never see it die and every cancel would burn the
    full timeout before escalating.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        _reap_owned(owned)
        return
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        group = None
    if group is None:
        with _suppress_os_error():
            os.kill(pid, signal.SIGKILL)
        _reap_owned(owned)
        return
    with _suppress_os_error():
        os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if _reap_owned(owned, timeout=0.1) or not pid_alive(pid):
            return
        time.sleep(0.05)
    with _suppress_os_error():
        os.killpg(group, signal.SIGKILL)
    _reap_owned(owned)


def _reap_owned(process: subprocess.Popen[Any] | None, timeout: float = 5.0) -> bool:
    """Collect a child we spawned. Returns True once it has been reaped."""
    if process is None:
        return False
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return True
    return True


class _suppress_os_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


# -------------------------------------------------------------------- runner


class JobRunner:
    """Submit, inspect and cancel jobs under one state directory."""

    def __init__(self, home: str | Path) -> None:
        self.root = Path(home) / "jobs"
        #: Handles for children this instance spawned. Keeping them is what
        #: lets us poll and reap them rather than guessing from the pid.
        self._processes: dict[str, subprocess.Popen[Any]] = {}

    # --------------------------------------------------------------- storage

    def _ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def _read(self, directory: Path) -> Job | None:
        try:
            payload = json.loads((directory / _META).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        job = Job(
            id=directory.name,
            dir=directory,
            argv=list(payload.get("argv") or []),
            state=str(payload.get("state") or STATE_RUNNING),
            pid=payload.get("pid"),
            start_token=str(payload.get("start_token") or ""),
            created_at=str(payload.get("created_at") or ""),
            started_at=str(payload.get("started_at") or ""),
            ended_at=str(payload.get("ended_at") or ""),
            exit_code=payload.get("exit_code"),
            error=str(payload.get("error") or ""),
        )
        return self._reap(job)

    def _alive(self, job: Job) -> bool:
        """Is this job's process still running?

        A handle for a child we spawned ourselves is authoritative and, on
        POSIX, polling it is also what reaps the zombie -- without which a dead
        child keeps answering ``kill(pid, 0)`` and never looks finished.
        """
        owned = self._processes.get(job.id)
        if owned is not None:
            return owned.poll() is None
        return pid_alive(job.pid, job.start_token)

    def _reap(self, job: Job) -> Job:
        """Correct the state of a job whose process is gone.

        A child records its own outcome, so a running job with a dead pid means
        it never got the chance -- killed, crashed, or the machine went down.
        Left alone it would sit in the list claiming to run forever.
        """
        if job.state in TERMINAL_STATES:
            return job
        if job.pid is None:
            # Never got as far as a process. Give the spawn a moment to land
            # before writing it off, or a job could be reaped between its
            # directory being created and Popen returning.
            if _age_seconds(job.created_at) < _SPAWN_GRACE_SECONDS:
                return job
        elif self._alive(job):
            return job

        cancelled = job.cancel_path.exists()
        job.state = STATE_CANCELLED if cancelled else STATE_LOST
        job.ended_at = job.ended_at or _now()
        if not cancelled and not job.error:
            job.error = "the job process exited without recording a result"
        write_meta(job)
        return job

    # ------------------------------------------------------------ operations

    def submit(self, argv: list[str], *, python: str | None = None) -> Job:
        """Start ``argv`` as a detached child and return immediately."""
        directory = self._new_dir()
        job = Job(
            id=directory.name,
            dir=directory,
            argv=list(argv),
            state=STATE_RUNNING,
            created_at=_now(),
        )
        write_meta(job)

        env = dict(os.environ)
        env[ENV_PROGRESS_FILE] = str(job.progress_path)
        # Unbuffered, or the follower sees the log arrive in bursts long after
        # the work that produced it.
        env["PYTHONUNBUFFERED"] = "1"

        command = [python or sys.executable, "-m", "task_graph.jobs", str(directory)]
        log = job.log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(Path.cwd()),
                **_detach_kwargs(),
            )
        except OSError as exc:
            # Record the failure rather than leaving a directory that says
            # "running" with no process behind it, which nothing would reap.
            job.state = STATE_FAILED
            job.error = f"could not start the job process: {exc}"
            job.ended_at = _now()
            write_meta(job)
            raise
        finally:
            log.close()

        self._processes[job.id] = process
        job.pid = process.pid
        job.start_token = process_start_token(process.pid) or ""
        write_meta(job)
        return job

    def get(self, job_id: str) -> Job:
        directory = self.root / str(job_id).strip()
        job = self._read(directory) if directory.is_dir() else None
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    def list(self, *, limit: int | None = None, running_only: bool = False) -> list[Job]:
        if not self.root.is_dir():
            return []
        jobs = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            job = self._read(directory)
            if job is None:
                continue
            if running_only and not job.is_running:
                continue
            jobs.append(job)
        jobs.sort(key=lambda j: _sort_key(j.id))
        if limit is not None:
            jobs = jobs[-limit:]
        return jobs

    def running(self) -> list[Job]:
        return self.list(running_only=True)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if not job.is_running:
            return job
        job.cancel_path.write_text(_now(), encoding="utf-8")
        # Only kill something we are sure is still this job. A recycled pid
        # belongs to somebody else, and taskkill /T and killpg both take out a
        # whole tree.
        if job.pid and self._alive(job):
            kill_tree(job.pid, owned=self._processes.get(job.id))
        self._processes.pop(job.id, None)
        job.state = STATE_CANCELLED
        job.ended_at = _now()
        write_meta(job)
        return job

    def prune(self, *, keep: int = 20) -> int:
        """Delete the oldest finished jobs, keeping the most recent ``keep``."""
        finished = [job for job in self.list() if not job.is_running]
        removed = 0
        for job in finished[: max(0, len(finished) - keep)]:
            shutil.rmtree(job.dir, ignore_errors=True)
            removed += 1
        return removed

    # ------------------------------------------------------------- internals

    def _new_dir(self) -> Path:
        """Claim the next numeric id, atomically.

        ``mkdir`` fails if the name is taken, which is exactly the lock needed
        when two shells submit at the same moment.
        """
        root = self._ensure_root()
        candidate = self._next_number()
        for _ in range(100):
            directory = root / str(candidate)
            try:
                directory.mkdir()
            except FileExistsError:
                candidate += 1
                continue
            return directory
        raise RuntimeError(f"could not allocate a job directory under {root}")

    def _next_number(self) -> int:
        highest = 0
        for directory in self.root.iterdir() if self.root.is_dir() else []:
            if directory.is_dir() and directory.name.isdigit():
                highest = max(highest, int(directory.name))
        return highest + 1


def _sort_key(job_id: str) -> tuple[int, int | str]:
    return (0, int(job_id)) if job_id.isdigit() else (1, job_id)


def _detach_kwargs() -> dict[str, Any]:
    """Spawn flags that keep a job alive and independently killable.

    A new process group on both platforms is what stops the shell's Ctrl-C from
    reaching a job it is not following, and on POSIX it gives ``killpg`` a group
    to aim at.
    """
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def write_meta(job: Job, *, attempts: int = 50, pause: float = 0.02) -> None:
    """Persist a job's metadata, replacing the file atomically.

    The temp file is named per writer. A fixed name would be shared by every
    process touching this job -- the child, the submitting shell, and any
    reader that reaps it -- so two writers would truncate the same file and
    race to publish a half-written mixture, which reads back as corrupt JSON
    and makes the job disappear from the list.

    ``os.replace`` on Windows fails while any process holds the destination
    open, and readers open ``meta.json`` several times a second while
    following. Retrying briefly turns that collision into a short wait instead
    of a lost result -- including the child's final write, where losing it
    would report a job that succeeded as ``lost``.
    """
    payload = {
        "argv": job.argv,
        "state": job.state,
        "pid": job.pid,
        "start_token": job.start_token,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "exit_code": job.exit_code,
        "error": job.error,
    }
    job.dir.mkdir(parents=True, exist_ok=True)
    temp = job.dir / f"meta.{os.getpid()}.{uuid4().hex}.tmp"
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    last: OSError | None = None
    for _ in range(max(1, attempts)):
        try:
            os.replace(temp, job.meta_path)
            return
        except PermissionError as exc:  # a reader has it open, briefly
            last = exc
            time.sleep(pause)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
    temp.unlink(missing_ok=True)
    if last is not None:
        raise last


def _age_seconds(timestamp: str) -> float:
    moment = _parse(timestamp)
    if moment is None:
        return float("inf")
    return max(0.0, (datetime.now(UTC) - moment).total_seconds())


# ------------------------------------------------------------- child process


def run_job(directory: str | Path) -> int:
    """Execute the job in ``directory``. This is the child's entry point.

    stdout and stderr are already pointed at ``log.txt`` by the parent, which
    catches import failures and interpreter crashes too -- redirecting in here
    would miss everything that happens before this function is reached.
    """
    from task_graph.cli import main

    path = Path(directory)
    runner = JobRunner(path.parent.parent)
    job = runner._read(path)
    if job is None:
        print(f"job metadata missing in {path}", file=sys.stderr)
        return 2

    job.state = STATE_RUNNING
    job.pid = os.getpid()
    job.start_token = process_start_token(os.getpid()) or ""
    job.started_at = _now()
    write_meta(job)

    exit_code = 0
    error = ""
    try:
        result = main.main(args=job.argv, prog_name="tg", standalone_mode=False)
        exit_code = result if isinstance(result, int) else 0
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except KeyboardInterrupt:
        exit_code = 130
        error = "interrupted"
    except BaseException as exc:  # noqa: BLE001 - the outcome must always be recorded
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
        print(f"error: {error}", file=sys.stderr)

    job.exit_code = exit_code
    job.error = error
    job.ended_at = _now()
    if job.cancel_path.exists():
        job.state = STATE_CANCELLED
    else:
        job.state = STATE_SUCCEEDED if exit_code == 0 else STATE_FAILED
    try:
        write_meta(job)
    except OSError as exc:
        # The result is already computed; say so in the log rather than dying
        # here, where the exit code would be the only surviving evidence.
        print(f"error: could not record the job result: {exc}", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m task_graph.jobs <job-directory>", file=sys.stderr)
        return 2
    return run_job(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
