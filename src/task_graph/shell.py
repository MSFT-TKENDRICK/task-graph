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
from collections.abc import Callable, Iterable, Iterator, Sequence

import click

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
) -> int:
    """Run the read-eval-print loop and return the last command's exit status.

    ``lines`` exists for tests and for ``tg shell -c``; when it is omitted the
    source is stdin, prompting only if there is a terminal on the other end.
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
        status = invoke(group, argv, base_args=base_args)
    return status


def banner_for(version: str, home: object) -> str:
    """Return the greeting: what you are attached to, and how to get out.

    ASCII only, deliberately: this is printed on Windows consoles that are still
    running a non-UTF-8 code page, where anything else arrives as mojibake.
    """
    return (
        f"task-graph {version} - interactive shell\n"
        f"state: {home}\n"
        "Type a tg command without the `tg` (e.g. `triage`, `why <id>`).\n"
        "`help` lists commands, `exit` leaves."
    )
