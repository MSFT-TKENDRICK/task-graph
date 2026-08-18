"""The shell's contract: one bad line must never end the session."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from task_graph.cli import main
from task_graph.config import Settings, set_settings
from task_graph.shell import invoke, normalise, run_shell, split_line


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_GRAPH_HOME", str(tmp_path))
    monkeypatch.setenv("TASK_GRAPH_EMBEDDINGS", "hashing")
    set_settings(Settings(home=tmp_path, embedding_provider="hashing"))
    return tmp_path


@pytest.fixture
def group():
    """A stand-in CLI covering every way a command can end."""

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.argument("word")
    def say(word: str) -> None:
        click.echo(f"said {word}")

    @cli.command()
    def boom() -> None:
        raise RuntimeError("kaboom")

    @cli.command()
    @click.pass_context
    def refuse(ctx: click.Context) -> None:
        ctx.exit(3)

    @cli.command()
    def complain() -> None:
        raise click.ClickException("no good")

    return cli


def test_split_line_keeps_windows_paths_intact():
    # shlex's default escaping would turn this into "C:srcgraph".
    assert split_line(r"--home C:\src\graph") == ["--home", r"C:\src\graph"]


def test_split_line_honours_quotes():
    assert split_line('search "billing timeout"') == ["search", "billing timeout"]


def test_normalise_drops_a_redundant_program_name():
    assert normalise(["tg", "triage", "--limit", "5"]) == ["triage", "--limit", "5"]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        (["help"], ["--help"]),
        (["?"], ["--help"]),
        (["help", "triage"], ["triage", "--help"]),
    ],
)
def test_normalise_routes_help_to_click(typed, expected):
    assert normalise(typed) == expected


def test_invoke_reports_status_without_exiting(group):
    assert invoke(group, ["say", "hello"]) == 0
    assert invoke(group, ["refuse"]) == 3
    assert invoke(group, ["nope"]) == 2  # a usage error


def test_invoke_contains_an_unhandled_exception(group, capsys):
    assert invoke(group, ["boom"]) == 1
    assert "RuntimeError: kaboom" in capsys.readouterr().err


def test_invoke_prints_click_exceptions_itself(group, capsys):
    # standalone_mode=False stops Click reporting these, so the shell must.
    assert invoke(group, ["complain"]) == 1
    assert "no good" in capsys.readouterr().err


def test_a_failing_line_does_not_end_the_session(group, capsys):
    status = run_shell(group, lines=["boom", "nope", "say still-here"])
    assert status == 0
    assert "said still-here" in capsys.readouterr().out


def test_an_unbalanced_quote_is_reported_not_raised(group, capsys):
    status = run_shell(group, lines=['say "unclosed'])
    assert status == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("word", ["exit", "quit", ":q", "EXIT"])
def test_exit_words_stop_the_loop(group, word, capsys):
    run_shell(group, lines=[word, "say unreachable"])
    assert "unreachable" not in capsys.readouterr().out


def test_blank_lines_are_ignored(group, capsys):
    assert run_shell(group, lines=["", "   ", "say ok"]) == 0
    assert capsys.readouterr().out.strip() == "said ok"


def test_status_is_the_last_command_run(group):
    assert run_shell(group, lines=["say a", "refuse"]) == 3
    assert run_shell(group, lines=["refuse", "say a"]) == 0


def test_base_args_are_replayed_and_can_be_overridden(capsys):
    run_shell(main, base_args=["--json"], lines=["status"])
    assert capsys.readouterr().out.lstrip().startswith("{")

    run_shell(main, base_args=["--json"], lines=["--no-json status"])
    assert not capsys.readouterr().out.lstrip().startswith("{")


def test_shell_command_runs_commands_and_exits():
    result = CliRunner().invoke(main, ["shell", "-c", "status", "-c", "status"])
    assert result.exit_code == 0
    assert result.output.count("run_id:") == 2


def test_shell_command_reads_piped_lines():
    result = CliRunner().invoke(main, ["shell"], input="status\nexit\n")
    assert result.exit_code == 0
    assert "open_tasks:" in result.output


def test_shell_command_inherits_group_options():
    result = CliRunner().invoke(main, ["--json", "shell", "-c", "status"])
    assert result.exit_code == 0
    assert result.output.lstrip().startswith("{")


def test_shell_command_propagates_failure():
    result = CliRunner().invoke(main, ["shell", "-c", "nosuchcommand"])
    assert result.exit_code == 2
