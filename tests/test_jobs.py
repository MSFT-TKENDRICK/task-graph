"""Jobs and progress: what the shell promises about slow work.

The guarantees under test are the ones a user notices when they fail -- a job
that says it is running when its process is gone, a cancel that leaves the work
going, or progress that stops moving.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from task_graph.cli import main
from task_graph.config import Settings, set_settings
from task_graph.jobs import (
    STATE_CANCELLED,
    STATE_LOST,
    STATE_SUCCEEDED,
    Job,
    JobRunner,
    pid_alive,
    process_start_token,
    runs_as_job,
    write_meta,
)
from task_graph.progress import (
    ConsoleSink,
    FileSink,
    ProgressState,
    Reporter,
    file_reporter,
    fold,
    iter_events,
    null_reporter,
    reporter_from_env,
)
from task_graph.shell import resolve_path, run_shell, split_background


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_GRAPH_HOME", str(tmp_path))
    monkeypatch.setenv("TASK_GRAPH_EMBEDDINGS", "hashing")
    set_settings(Settings(home=tmp_path, embedding_provider="hashing"))
    return tmp_path


@pytest.fixture
def runner(tmp_path):
    return JobRunner(tmp_path)


# ------------------------------------------------------------------ progress


def test_reporter_records_phases_and_counts(tmp_path):
    path = tmp_path / "p.ndjson"
    reporter = file_reporter(path)
    reporter.phase("sources", total=3)
    reporter.advance(message="github: 4 items")
    reporter.advance(message="mail: 0 items")

    events, offset = iter_events(path)
    assert [e.kind for e in events] == ["phase", "advance", "advance"]
    state = fold(events)
    assert (state.phase, state.current, state.total) == ("sources", 2, 3)
    assert state.describe() == "sources 2/3 mail: 0 items"
    assert offset > 0


def test_iter_events_resumes_from_an_offset(tmp_path):
    path = tmp_path / "p.ndjson"
    reporter = file_reporter(path)
    reporter.phase("a", total=2)
    first, offset = iter_events(path)
    reporter.advance()

    later, _ = iter_events(path, start=offset)
    assert len(first) == 1
    assert [e.kind for e in later] == ["advance"]


def test_a_partial_trailing_line_is_left_for_next_time(tmp_path):
    # A follower reads while the writer is mid-line; that must not corrupt.
    # Written as bytes: text mode would translate the newline on Windows and
    # the offset under test is a byte offset.
    path = tmp_path / "p.ndjson"
    complete = b'{"kind": "phase", "phase": "a"}\n'
    path.write_bytes(complete + b'{"kind": "adva')
    events, offset = iter_events(path)
    assert [e.phase for e in events] == ["a"]
    assert offset == len(complete)


def test_unparseable_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "p.ndjson"
    path.write_text('not json\n{"kind": "phase", "phase": "b"}\n', encoding="utf-8")
    events, _ = iter_events(path)
    assert [e.phase for e in events] == ["b"]


def test_null_reporter_costs_nothing_and_raises_nothing():
    reporter = null_reporter()
    reporter.phase("x", total=1)
    reporter.advance()
    reporter.log("hello")


def test_a_broken_sink_cannot_break_the_work():
    def explode(event):
        raise RuntimeError("sink is down")

    Reporter(explode).phase("x")  # must not propagate


def test_reporter_from_env_honours_the_job_variable(tmp_path):
    path = tmp_path / "from-env.ndjson"
    reporter = reporter_from_env({"TASK_GRAPH_PROGRESS_FILE": str(path)})
    reporter.phase("x")
    assert path.exists()
    assert reporter_from_env({}) is not None


def test_progress_state_describes_itself():
    assert ProgressState().describe() == "working"
    assert ProgressState(phase="rank").describe() == "rank"
    assert ProgressState(phase="replay", current=3, total=9).describe() == "replay 3/9"
    assert ProgressState(phase="a", current=2).describe() == "a 2"


def test_console_sink_is_silent_off_a_terminal(tmp_path):
    class Pipe:
        def __init__(self):
            self.written = []

        def isatty(self):
            return False

        def write(self, text):
            self.written.append(text)

        def flush(self):
            pass

    pipe = Pipe()
    sink = ConsoleSink(pipe)
    sink(next(iter(_events(tmp_path))))
    assert pipe.written == []


def test_console_sink_rewrites_one_line_on_a_terminal(tmp_path):
    class Tty:
        def __init__(self):
            self.buffer = []

        def isatty(self):
            return True

        def write(self, text):
            self.buffer.append(text)

        def flush(self):
            pass

    tty = Tty()
    sink = ConsoleSink(tty)
    for event in _events(tmp_path):
        sink(event)
    sink.clear()
    assert all(chunk.startswith("\r") for chunk in tty.buffer)
    assert any("sources" in chunk for chunk in tty.buffer)


def _events(tmp_path):
    path = tmp_path / "e.ndjson"
    reporter = Reporter(FileSink(path))
    reporter.phase("sources", total=2)
    reporter.advance(message="github")
    events, _ = iter_events(path)
    return events


# ------------------------------------------------------------- job selection


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["sync"], True),
        (["doctor"], True),
        (["rebuild"], True),
        (["learn"], True),
        (["--json", "sync", "--propose"], True),
        # The option's value used to be read as the command name, so this ran
        # inline and froze the prompt.
        (["--home", r"C:\graph", "sync"], True),
        (["pull"], True),  # alias
        (["action", "execute", "a1"], True),
        (["a", "execute", "a1"], True),  # group alias
        (["approve", "action", "a1", "--execute"], True),  # deprecated but still mutates
        (["approve", "action", "a1"], False),  # grant only, local
        (["action", "approve", "a1"], False),
        (["triage"], False),
        (["t"], False),
        (["show", "task-1"], False),
        (["search", "sync"], False),  # the word sync as an argument, not a command
        (["jobs"], False),
        (["logs", "3"], False),
        ([], False),
        (["--json"], False),
        (["nonsense"], False),
    ],
)
def test_only_blocking_commands_become_jobs(argv, expected):
    assert runs_as_job(resolve_path(main, argv), argv) is expected


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (["t"], ("triage",)),
        (["pull"], ("sync",)),
        (["j", "ls"], ("job", "list")),
        (["jobs"], ("job", "list")),  # shortcut expands to its canonical path
        (["logs", "3"], ("job", "logs")),
        (["bg", "sync"], ("job", "start")),
        (["merges"], ("merge", "list")),
        (["actions"], ("action", "list")),
        (["m", "approve", "p1"], ("merge", "approve")),
        (["--home", r"C:\graph", "sync"], ("sync",)),
        (["nonsense"], ()),
    ],
)
def test_paths_resolve_through_aliases_and_shortcuts(argv, path):
    assert resolve_path(main, argv) == path


@pytest.mark.parametrize(
    ("typed", "argv", "background"),
    [
        (["sync"], ["sync"], False),
        (["sync", "&"], ["sync"], True),
        (["sync&"], ["sync"], True),
        (["sync", "--propose", "&"], ["sync", "--propose"], True),
        ([], [], False),
    ],
)
def test_trailing_ampersand_means_background(typed, argv, background):
    assert split_background(typed) == (argv, background)


# ------------------------------------------------------------- job lifecycle


def test_submit_runs_the_command_and_records_success(runner):
    job = runner.submit(["status"])
    finished = _wait(runner, job.id)
    assert finished.state == STATE_SUCCEEDED
    assert finished.exit_code == 0
    assert "open_tasks:" in finished.read_log()


def test_a_failing_command_is_recorded_as_failed(runner):
    job = runner.submit(["nosuchcommand"])
    finished = _wait(runner, job.id)
    assert finished.state != STATE_SUCCEEDED
    assert finished.exit_code not in (0, None)
    assert "No such command" in finished.read_log()


def test_jobs_get_sequential_ids_that_can_be_typed(runner):
    first = _fabricate(runner, ["sync"], pid=None, state=STATE_SUCCEEDED)
    second = _fabricate(runner, ["doctor"], pid=None, state=STATE_SUCCEEDED)
    assert (first.id, second.id) == ("1", "2")


def test_a_job_whose_process_vanished_is_reaped_as_lost(runner):
    job = _fabricate(runner, ["sync"], pid=_dead_pid())
    reaped = runner.get(job.id)
    assert reaped.state == STATE_LOST
    assert "without recording a result" in reaped.error


def test_a_killed_job_is_reaped_as_cancelled_not_lost(runner):
    job = _fabricate(runner, ["sync"], pid=_dead_pid())
    job.cancel_path.write_text("now", encoding="utf-8")
    assert runner.get(job.id).state == STATE_CANCELLED


def test_cancelling_a_finished_job_leaves_it_alone(runner):
    job = _fabricate(runner, ["sync"], pid=None, state=STATE_SUCCEEDED)
    assert runner.cancel(job.id).state == STATE_SUCCEEDED


def test_a_recycled_pid_is_not_trusted_and_never_killed(runner):
    """A stale pid after a reboot must not be mistaken for a running job.

    Cancelling it would force-kill whatever now owns that pid, tree and all.
    """
    live = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        job = _fabricate(runner, ["sync"], pid=live.pid)
        job.start_token = "not-the-token-this-process-has"
        write_meta(job)

        # The pid exists, but it is not the process this job started.
        assert pid_alive(live.pid, "not-the-token-this-process-has") is False
        assert runner.get(job.id).state == STATE_LOST

        runner.cancel(job.id)
        assert live.poll() is None, "cancel killed an unrelated process"
    finally:
        live.kill()
        live.wait(timeout=10)


def test_a_start_token_is_recorded_and_matches_the_process(runner):
    job = runner.submit(["status"])
    stored = runner.get(job.id)
    if process_start_token(os.getpid()) is None:
        pytest.skip("this platform does not expose process start times")
    assert stored.start_token
    _wait(runner, job.id)


def test_a_job_that_never_spawned_is_reaped_once_the_grace_expires(runner):
    job = _fabricate(runner, ["sync"], pid=None)
    job.created_at = _now_iso()
    write_meta(job)
    assert runner.get(job.id).is_running  # still inside the grace window

    job.created_at = "2020-01-01T00:00:00+00:00"
    write_meta(job)
    assert runner.get(job.id).state == STATE_LOST


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def test_meta_is_written_through_a_private_temp_file(runner):
    job = _fabricate(runner, ["sync"], pid=None, state=STATE_SUCCEEDED)
    write_meta(job)
    leftovers = list(job.dir.glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"
    assert json.loads(job.meta_path.read_text(encoding="utf-8"))["state"] == STATE_SUCCEEDED


def test_meta_survives_a_transient_reader(runner):
    """os.replace fails on Windows while a reader has the destination open.

    Readers only ever hold it for the length of a read, so the retry has to
    outlast that -- modelled here by a handle released shortly after the write
    starts.
    """
    job = _fabricate(runner, ["sync"], pid=None, state=STATE_SUCCEEDED)
    handle = job.meta_path.open("r", encoding="utf-8")
    threading.Timer(0.15, handle.close).start()

    job.error = "written while being read"
    write_meta(job, attempts=200, pause=0.01)
    assert runner.get(job.id).error == "written while being read"


def test_a_finished_job_releases_its_process_handle(runner):
    """Holding the handle past completion is a zombie leak, not caution.

    The child writes its result just before exiting, so the handle is released
    on the first read *after* the process actually goes -- not necessarily the
    read that first sees a terminal state.
    """
    job = runner.submit(["status"])
    owned = runner._processes[job.id]
    _wait(runner, job.id)

    _await(lambda: runner.get(job.id) and job.id not in runner._processes)
    assert owned.returncode is not None, "child was never reaped"


def test_cancel_kills_the_process_it_points_at(runner):
    """Cancellation has to stop a real process, not just relabel a record.

    Driven against a sleeper we spawn ourselves rather than a real command:
    the point under test is the kill, and every command long enough to cancel
    would need the network.
    """
    sleeper = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        job = _fabricate(runner, ["sync"], pid=sleeper.pid)
        assert pid_alive(sleeper.pid)

        cancelled = runner.cancel(job.id)
        assert cancelled.state == STATE_CANCELLED
        _await(lambda: not pid_alive(sleeper.pid))
        assert runner.get(job.id).state == STATE_CANCELLED
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=10)


def test_listing_separates_running_from_finished(runner):
    _fabricate(runner, ["sync"], pid=None, state=STATE_SUCCEEDED)
    _fabricate(runner, ["doctor"], pid=None, state="running", alive=True)
    assert len(runner.list()) == 2
    assert [j.label for j in runner.list(running_only=True)] == ["doctor"]


def test_unknown_job_is_a_clean_error(runner):
    with pytest.raises(KeyError):
        runner.get("404")


def test_prune_keeps_the_most_recent_finished_jobs(runner):
    for _ in range(5):
        _fabricate(runner, ["sync"], pid=None, state=STATE_SUCCEEDED)
    assert runner.prune(keep=2) == 3
    assert len(runner.list()) == 2


def _fabricate(runner, argv, *, pid, state="running", alive=False):
    """Build a job directory by hand, with no process behind it."""
    directory = runner._new_dir()
    job = Job(
        id=directory.name,
        dir=directory,
        argv=list(argv),
        state=state,
        pid=os.getpid() if alive else pid,
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_meta(job)
    return job


def _dead_pid() -> int:
    """A pid that is definitely not running any more."""
    finished = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    finished.wait(timeout=30)
    return finished.pid


def _wait(runner: JobRunner, job_id: str, timeout: float = 60.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if not job.is_running:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _await(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("condition never became true")


# ------------------------------------------------------------ shell dispatch


class FakeRunner:
    """Records submissions instead of spawning anything."""

    def __init__(self):
        self.submitted: list[list[str]] = []

    def submit(self, argv):
        self.submitted.append(list(argv))
        return Job(id=str(len(self.submitted)), dir=Path("."), argv=list(argv))


def test_the_shell_sends_slow_commands_to_the_job_runner(capsys):
    fake = FakeRunner()
    run_shell(main, lines=["sync &"], runner=fake, base_args=["--json"])
    assert fake.submitted == [["--json", "sync"]]
    assert "Started job 1" in capsys.readouterr().out


def test_the_shell_runs_fast_commands_in_process(capsys):
    fake = FakeRunner()
    run_shell(main, lines=["status"], runner=fake)
    assert fake.submitted == []
    assert "open_tasks:" in capsys.readouterr().out


def test_without_a_runner_everything_stays_in_process(capsys):
    run_shell(main, lines=["status"])
    assert "open_tasks:" in capsys.readouterr().out


class BrokenRunner:
    """Every job operation fails, the way a bad spawn would."""

    def submit(self, argv):
        raise OSError("python not found")


class HangingRunner:
    """Submits, then interrupts twice: once following, once cancelling."""

    def __init__(self):
        self.cancelled = False

    def submit(self, argv):
        return Job(id="1", dir=Path("."), argv=list(argv))

    def get(self, job_id):
        raise KeyboardInterrupt  # lands inside follow()

    def cancel(self, job_id):
        self.cancelled = True
        raise KeyboardInterrupt  # the impatient second Ctrl-C


def test_a_failing_submit_does_not_end_the_session(capsys):
    status = run_shell(main, lines=["sync", "status"], runner=BrokenRunner())
    output = capsys.readouterr()
    assert "python not found" in output.err
    assert "open_tasks:" in output.out  # the shell carried on
    assert status == 0


def test_a_second_ctrl_c_while_cancelling_does_not_end_the_session(capsys):
    runner = HangingRunner()
    run_shell(main, lines=["sync", "status"], runner=runner)
    assert runner.cancelled
    assert "open_tasks:" in capsys.readouterr().out


# -------------------------------------------------------------- cli surface


def test_jobs_command_reports_nothing_running():
    result = CliRunner().invoke(main, ["jobs"])
    assert result.exit_code == 0
    assert "No jobs running." in result.output


def test_bg_starts_a_job_and_names_it(tmp_path):
    result = CliRunner().invoke(main, ["bg", "status"])
    assert result.exit_code == 0
    assert "Started job 1" in result.output
    _wait(JobRunner(tmp_path), "1")


def test_bg_passes_options_through_to_the_job(tmp_path):
    """`bg` must not try to claim the job's own options as its own."""
    result = CliRunner().invoke(main, ["bg", "sync", "--propose", "--no-rank"])
    assert result.exit_code == 0, result.output
    runner = JobRunner(tmp_path)
    assert runner.get("1").argv == ["sync", "--propose", "--no-rank"]
    runner.cancel("1")


def test_logs_and_cancel_reject_unknown_ids():
    for argv in (["logs", "404"], ["cancel", "404"]):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code != 0
        assert "unknown job" in result.output


def test_jobs_json_is_machine_readable(tmp_path):
    JobRunner(tmp_path)  # ensure the home exists
    result = CliRunner().invoke(main, ["--json", "jobs", "--all"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []
