"""The interactive ``tg`` shell: one process, many commands.

``tg``'s start-up cost is dominated by imports — numpy, pydantic and the MCP SDK
together take about a second and a half before the first character of output
appears. That is invisible when you run one command and intolerable when you run
ten in a row, which is exactly what triage looks like: ``triage``, ``why <id>``,
``show <id>``, ``correct <id> --lower``, ``triage`` again.

So this dispatches back into the *same* Click group in the *same* process. The
imports are paid once, at start-up, and every command after that is effectively
free. Commands run with ``standalone_mode=False`` so Click hands control back
instead of calling :func:`sys.exit`, and every way a command can fail — a usage
error, a :class:`click.ClickException`, ``ctx.exit(1)``, an unhandled exception —
is turned into a status code and a printed message rather than a dead shell.

The shell is deliberately not a new language. A line is the argument list you
would have typed after ``tg``, so ``triage --limit 5`` and
``tg triage --limit 5`` do the same thing and everything in ``tg --help``
already works here, group options included.
"""

from __future__ import annotations

import shlex
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from functools import partial
from typing import Any

import click

from task_graph.jobs import Job, JobRunner, is_job_command
from task_graph.progress import ConsoleSink, iter_events

PROMPT = "tg> "

#: Words that leave the shell. ``:q`` is muscle memory for enough people to be
#: worth the two lines it costs.
EXIT_WORDS = frozenset({"exit", "quit", ":q"})

#: Bare words that mean "show me what I can type", mapped onto Click's own help.
HELP_WORDS = frozenset({"help", "?"})


def split_line(line: str) -> list[str]:
    """Split ``line`` into arguments, leaving backslashes alone.

    ``shlex.split`` treats a backslash as an escape character, which quietly
    turns ``--home C:\\src\\graph`` into ``C:srcgraph``. Windows paths are the
    common case here, and the shells this imitates (PowerShell, cmd) do not
    escape with backslashes either — so quoting is honoured and escaping is not.
    """
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


def normalise(argv: Sequence[str]) -> list[str]:
    """Resolve the shell's own conveniences into a real ``tg`` argument list.

    A leading ``tg`` is dropped, because typing it inside a ``tg`` shell is a
    reflex rather than a mistake, and ``help`` is rewritten to ``--help`` so the
    obvious thing to type at an unfamiliar prompt works.
    """
    args = list(argv)
    if args and args[0].lower() in {"tg", "tg.exe"}:
        args = args[1:]
    if not args:
        return args
    if args[0].lower() in HELP_WORDS:
        # `help` -> `--help`, but `help triage` -> `triage --help`, which is the
        # page you actually wanted.
        return [*args[1:], "--help"]
    return args


def split_background(argv: Sequence[str]) -> tuple[list[str], bool]:
    """Strip a trailing ``&`` and report whether it was there.

    Accepts both ``sync &`` and ``sync&``, because which one you type depends
    entirely on which shell you used last.
    """
    args = list(argv)
    if not args:
        return args, False
    if args[-1] == "&":
        return args[:-1], True
    if len(args[-1]) > 1 and args[-1].endswith("&"):
        args[-1] = args[-1][:-1]
        return args, True
    return args, False


def follow(
    runner: JobRunner,
    job: Job,
    *,
    poll: float = 0.25,
    out: Any = None,
    err: Any = None,
) -> int:
    """Watch a running job: stream its output, draw its progress, allow Ctrl-C.

    Output and progress go to different streams on purpose. The log is real
    content and belongs on stdout, where it can be piped; the progress line is
    a redrawn scribble that only makes sense on a terminal, so it goes to
    stderr and is erased before anything is printed over it.
    """
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    console = ConsoleSink(stderr)
    log_at = 0
    progress_at = 0

    def drain() -> None:
        nonlocal log_at, progress_at
        current = runner.get(job.id)
        text = current.read_log()
        if len(text) > log_at:
            console.clear()
            stdout.write(text[log_at:])
            stdout.flush()
            log_at = len(text)
        events, progress_at = iter_events(current.progress_path, start=progress_at)
        for event in events:
            console(event)

    try:
        while True:
            drain()
            if not runner.get(job.id).is_running:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        console.clear()
        # A cancel is not instant -- it kills a process tree and waits for it --
        # and an impatient second Ctrl-C lands right here. Absorb it, or the
        # interrupt escapes into the shell loop and ends the session.
        try:
            runner.cancel(job.id)
            click.echo(f"\nCancelled job {job.id}.", err=True)
        except KeyboardInterrupt:
            click.echo(f"\nStill stopping job {job.id}; check `jobs`.", err=True)
        except Exception as exc:  # noqa: BLE001 - report, never propagate
            click.echo(f"\ncould not cancel job {job.id}: {exc}", err=True)
        return 130
    finally:
        console.clear()

    drain()
    finished = runner.get(job.id)
    if finished.ok:
        return 0
    detail = finished.error or f"job {finished.id} {finished.state}"
    click.echo(f"job {finished.id} {finished.state}: {detail}", err=True)
    return finished.exit_code if finished.exit_code is not None else 1


def start_job(
    runner: JobRunner,
    argv: Sequence[str],
    *,
    background: bool,
    base_args: Sequence[str] = (),
    echo: Callable[..., None] = click.echo,
) -> int:
    """Submit a job and either watch it or hand the prompt straight back."""
    job = runner.submit([*base_args, *argv])
    if background:
        echo(f"Started job {job.id}: {job.label} (jobs, logs {job.id}, cancel {job.id})")
        return 0
    echo(f"[job {job.id}] {job.label} - Ctrl-C cancels; add & to keep the prompt")
    return follow(runner, job)


def invoke(group: click.Group, argv: Sequence[str], *, base_args: Sequence[str] = ()) -> int:
    """Run one command in this process and return its exit status.

    ``base_args`` are the group-level options the shell itself was started with
    (``--home``, ``--json``, ``-v``). They are prepended rather than merged, so a
    line can still override any of them — Click takes the last occurrence of an
    option, which makes ``--no-json`` inside a ``--json`` shell do what it says.
    """
    args = [*base_args, *argv]
    try:
        result = group.main(args=args, prog_name="tg", standalone_mode=False)
    except click.ClickException as exc:
        # Usage errors and ClickExceptions are re-raised rather than printed
        # once standalone_mode is off, so the shell owns the reporting.
        exc.show()
        return exc.exit_code
    except click.exceptions.Exit as exc:  # --help, --version, ctx.exit(n)
        return int(exc.exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1
    except KeyboardInterrupt:
        click.echo("^C", err=True)
        return 130
    except SystemExit as exc:  # a command that exits the hard way
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - one bad command must not end the session
        click.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        return 1
    # A command that simply returns has succeeded; Click only hands back an int
    # when it caught an Exit on our behalf.
    return result if isinstance(result, int) else 0


def _prompt_lines(prompt: str) -> Iterator[str]:
    """Yield typed lines, surviving Ctrl-C and stopping at Ctrl-D/Ctrl-Z."""
    while True:
        try:
            yield input(prompt)
        except EOFError:
            click.echo("")
            return
        except KeyboardInterrupt:
            # Abandon the half-typed line, keep the shell.
            click.echo("^C", err=True)
            continue


def _piped_lines(stream: object) -> Iterator[str]:
    """Yield lines from a non-tty stdin, so ``echo triage | tg shell`` works."""
    for line in stream:  # type: ignore[attr-defined]
        yield line.rstrip("\n")


def run_shell(
    group: click.Group,
    *,
    base_args: Sequence[str] = (),
    lines: Iterable[str] | None = None,
    banner: str | None = None,
    prompt: str = PROMPT,
    echo: Callable[..., None] = click.echo,
    runner: JobRunner | None = None,
) -> int:
    """Run the read-eval-print loop and return the last command's exit status.

    ``lines`` exists for tests and for ``tg shell -c``; when it is omitted the
    source is stdin, prompting only if there is a terminal on the other end.

    Slow commands are handed to ``runner`` as background jobs rather than run
    inline, so the prompt is never held hostage by a source that will not
    answer. Fast commands stay in-process, because a local SQLite read finishes
    in milliseconds and spawning a process to do it would be pure latency.
    """
    interactive = lines is None and sys.stdin is not None and sys.stdin.isatty()
    if lines is None:
        lines = _prompt_lines(prompt) if interactive else _piped_lines(sys.stdin)
    if banner and interactive:
        echo(banner)

    status = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            argv = split_line(line)
        except ValueError as exc:  # an unbalanced quote
            echo(f"error: {exc}", err=True)
            status = 1
            continue
        if argv[0].lower() in EXIT_WORDS:
            break
        argv = normalise(argv)
        if not argv:
            continue
        argv, background = split_background(argv)
        if not argv:
            continue
        if runner is not None and is_job_command(argv):
            status = _guarded(
                partial(
                    start_job,
                    runner,
                    argv,
                    background=background,
                    base_args=base_args,
                    echo=echo,
                )
            )
            continue
        status = invoke(group, argv, base_args=base_args)
    return status


def _guarded(action: Callable[[], int]) -> int:
    """Run ``action`` with the same firewall :func:`invoke` gives commands.

    The job path can fail in ways a command cannot -- a spawn that will not
    start, a metadata write that loses a race -- and a second Ctrl-C lands
    while the first one is still inside ``cancel``, which is not instant.
    None of that may take the session with it.
    """
    try:
        return action()
    except KeyboardInterrupt:
        click.echo("^C", err=True)
        return 130
    except Exception as exc:  # noqa: BLE001 - the session outlives its jobs
        click.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        return 1


def banner_for(version: str, home: object, running: int = 0) -> str:
    """Return the greeting: what you are attached to, and how to get out.

    ASCII only, deliberately: this is printed on Windows consoles that are still
    running a non-UTF-8 code page, where anything else arrives as mojibake.
    """
    lines = [
        f"task-graph {version} - interactive shell",
        f"state: {home}",
        "Type a tg command without the `tg` (e.g. `triage`, `why <id>`).",
        "Slow work (sync, doctor, rebuild, learn) runs as a job: Ctrl-C cancels,",
        "`sync &` keeps the prompt, and `jobs` / `logs <id>` / `cancel <id>` inspect it.",
        "`help` lists commands, `exit` leaves.",
    ]
    if running:
        lines.append(f"{running} job(s) still running from earlier - see `jobs`.")
    return "\n".join(lines)
