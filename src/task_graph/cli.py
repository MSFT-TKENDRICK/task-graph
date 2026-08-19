"""Command-line interface for task-graph."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import click
from activegraph import Object

from task_graph import __version__
from task_graph.app import SyncReport, TaskGraphApp
from task_graph.config import (
    DEFAULT_FOUNDRY_ENDPOINT,
    DEFAULT_FOUNDRY_MODEL,
    Settings,
    set_settings,
)
from task_graph.jobs import JobRunner
from task_graph.learning.corrections import LearningReport
from task_graph.ontology.types import ObjectType
from task_graph.pipeline.approval import ApprovalRequiredError, RemediationExecutionError
from task_graph.progress import (
    ENV_PROGRESS_FILE,
    ConsoleSink,
    Reporter,
    file_reporter,
    null_reporter,
)
from task_graph.shell import banner_for, run_shell
from task_graph.store import SCHEMA_VERSION


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _echo_json(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True, default=_json_default))


def _render(ctx: click.Context, data: Any, text: str | Callable[[], str]) -> None:
    if ctx.obj["json"]:
        _echo_json(data)
    else:
        click.echo(text() if callable(text) else text)


def _settings_from_context(ctx: click.Context) -> Settings:
    home = ctx.obj.get("home")
    settings = Settings(
        home=Path(home).expanduser() if home else Settings.from_env().home,
        embedding_provider=os.environ.get("TASK_GRAPH_EMBEDDINGS", "auto"),
        foundry_endpoint=os.environ.get("TASK_GRAPH_FOUNDRY_ENDPOINT", DEFAULT_FOUNDRY_ENDPOINT),
        foundry_model=os.environ.get("TASK_GRAPH_FOUNDRY_MODEL", DEFAULT_FOUNDRY_MODEL),
    )
    set_settings(settings)
    return settings


def _with_app(ctx: click.Context, reporter: Reporter | None = None) -> TaskGraphApp:
    return TaskGraphApp(_settings_from_context(ctx), reporter=reporter)


def _truncate(text: Any, width: int) -> str:
    """Shorten to ``width``, ASCII-only.

    A single-character ellipsis renders as a replacement glyph on Windows
    consoles running a non-UTF-8 code page, which is exactly where this tool
    is used most.
    """
    value = str(text or "")
    if len(value) <= width:
        return value
    return value[: max(0, width - 3)] + "..."


def _task_row(rank: int, task: Object, breakdown: Any) -> dict[str, Any]:
    return {
        "rank": rank,
        "id": task.id,
        "priority": round(float(breakdown.score), 3),
        "title": task.data.get("title"),
        "state": task.data.get("state"),
        "sources": task.data.get("source_uris") or [],
        "explanation": breakdown.explanation,
        "factors": breakdown.factors,
    }


def _format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No tasks found. Run `tg sync` first."
    widths = {"rank": 4, "priority": 8, "state": 8, "sources": 12}
    title_width = 42
    lines = [
        f"{'#':>{widths['rank']}}  {'PRIORITY':>{widths['priority']}}  "
        f"{'STATE':<{widths['state']}}  {'SOURCES':<{widths['sources']}}  "
        f"{'TITLE':<{title_width}}  WHY"
    ]
    for row in rows:
        sources = ",".join(str(s).split(":", 1)[0] for s in row["sources"]) or "-"
        lines.append(
            f"{row['rank']:>{widths['rank']}}  {row['priority']:>{widths['priority']}.3f}  "
            f"{_truncate(row['state'], widths['state']):<{widths['state']}}  "
            f"{_truncate(sources, widths['sources']):<{widths['sources']}}  "
            f"{_truncate(row['title'], title_width):<{title_width}}  "
            f"{_truncate(row['explanation'], 90)}"
        )
    return "\n".join(lines)


def _empty_message(kind: str = "tasks") -> str:
    return f"No {kind} found. Run `tg sync` first."


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--home", type=click.Path(path_type=Path), help="Override TASK_GRAPH_HOME.")
@click.option("--json/--no-json", "as_json", default=False, help="Emit machine-readable JSON.")
@click.option("-v", "--verbose", count=True, help="Increase diagnostic output.")
@click.version_option()
@click.pass_context
def main(ctx: click.Context, home: Path | None, as_json: bool, verbose: int) -> None:
    """Local-first unified task graph CLI."""
    ctx.ensure_object(dict)
    ctx.obj.update({"home": home, "json": as_json, "verbose": verbose})


def _reporter(ctx: click.Context) -> tuple[Reporter, ConsoleSink | None]:
    """Build the progress reporter for a slow command.

    Three destinations, in priority order: the file a job runner asked for, a
    live line on the terminal, or nowhere. JSON mode never draws, because the
    point of `--json` is that stdout parses.

    The opening event is emitted here rather than left to the caller so that
    something appears immediately. Opening the graph and probing the embedder
    happen before any command-specific work, and a silent gap at the start is
    indistinguishable from a hang -- which is the whole complaint.
    """
    env_path = (os.environ.get(ENV_PROGRESS_FILE) or "").strip()
    if env_path:
        reporter, console = file_reporter(env_path), None
    elif ctx.obj.get("json"):
        return null_reporter(), None
    else:
        console = ConsoleSink()
        reporter = Reporter(console)
    reporter.phase("starting", message="opening the graph")
    return reporter, console


def _finish(console: ConsoleSink | None) -> None:
    if console is not None:
        console.clear()


@main.command()
@click.option("--source", "sources", multiple=True, type=click.Choice(["github", "ado", "mail"]))
@click.option("--since", help="Only sync changes since this ISO timestamp.")
@click.option("--no-dedupe", is_flag=True, help="Skip dedupe after ingest.")
@click.option("--no-rank", is_flag=True, help="Skip priority ranking after ingest.")
@click.option("--propose", is_flag=True, help="Propose remediation actions after sync.")
@click.pass_context
def sync(
    ctx: click.Context,
    sources: tuple[str, ...],
    since: str | None,
    no_dedupe: bool,
    no_rank: bool,
    propose: bool,
) -> None:
    """Pull source changes into the local graph."""
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    reporter, console = _reporter(ctx)
    try:
        with _with_app(ctx, reporter) as app:
            report = app.sync(
                sources=sources or None,
                since=since_dt,
                dedupe=not no_dedupe,
                rank=not no_rank,
                propose=propose,
                reporter=reporter,
            )
    finally:
        _finish(console)
    data = _sync_report(report)
    _render(ctx, data, report.summary())


@main.command()
@click.option("--limit", default=20, show_default=True, type=int)
@click.pass_context
def triage(ctx: click.Context, limit: int) -> None:
    """Show the ranked list of what to do next."""
    with _with_app(ctx) as app:
        rows = [_task_row(i, task, score) for i, (task, score) in enumerate(app.triage(limit), 1)]
    _render(ctx, rows, lambda: _format_table(rows))


@main.command()
@click.argument("task_id")
@click.pass_context
def show(ctx: click.Context, task_id: str) -> None:
    """Show full task detail."""
    with _with_app(ctx) as app:
        task = app.get_task(task_id)
        if task is None:
            _render(ctx, {"error": "task_not_found", "task_id": task_id}, _empty_message("task"))
            return
        priority = app.explain_priority(task_id).model_dump(mode="json")
        graph = app.task_graph(task_id, 1)
    related = [
        {"id": obj["id"], "title": obj["data"].get("title"), "state": obj["data"].get("state")}
        for obj in graph["objects"]
        if obj["type"] == ObjectType.TASK.value and obj["id"] != task_id
    ]
    data = {**task, "priority": priority, "related_tasks": related}
    _render(ctx, data, lambda: _format_task_detail(data))


@main.command()
@click.argument("query")
@click.option("--limit", default=20, show_default=True, type=int)
@click.pass_context
def search(ctx: click.Context, query: str, limit: int) -> None:
    """Search tasks."""
    with _with_app(ctx) as app:
        results = app.search_tasks(query, limit)
    _render(ctx, results, lambda: _format_search(results))


@main.command()
@click.argument("task_id")
@click.pass_context
def why(ctx: click.Context, task_id: str) -> None:
    """Explain why a task has its priority."""
    try:
        with _with_app(ctx) as app:
            breakdown = app.explain_priority(task_id)
    except KeyError:
        _render(ctx, {"error": "task_not_found", "task_id": task_id}, _empty_message("task"))
        return
    data = breakdown.model_dump(mode="json")
    _render(ctx, data, lambda: _format_breakdown(data))


@main.command()
@click.pass_context
def merges(ctx: click.Context) -> None:
    """List pending merge proposals."""
    with _with_app(ctx) as app:
        pending = app.pending_merges()
    _render(ctx, pending, lambda: _format_merges(pending))


@main.command()
@click.option("--task", "task_id", help="Create/show approvals for one task.")
@click.pass_context
def actions(ctx: click.Context, task_id: str | None) -> None:
    """List pending remediation approvals and rendered previews."""
    with _with_app(ctx) as app:
        if task_id:
            try:
                app.propose_for(task_id)
            except KeyError:
                _render(
                    ctx,
                    {"error": "task_not_found", "task_id": task_id},
                    _empty_message("task"),
                )
                return
        pending = [_remediation_dict(obj) for obj in app.pending_approvals()]
    _render(ctx, pending, lambda: _format_actions(pending))


@main.group()
def approve() -> None:
    """Approve proposed graph changes or actions."""


@approve.command("merge")
@click.argument("patch_id")
@click.pass_context
def approve_merge(ctx: click.Context, patch_id: str) -> None:
    """Approve and apply a pending merge proposal."""
    try:
        with _with_app(ctx) as app:
            canonical = app.approve_merge(patch_id, actor="cli")
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    data = {"patch_id": patch_id, "merged_into": canonical, "status": "applied"}
    _render(ctx, data, f"Approved merge {patch_id}; canonical task is {canonical}.")


@approve.command("action")
@click.argument("approval_id")
@click.option(
    "--execute",
    is_flag=True,
    help=(
        "Actually run the source-system mutation. WITHOUT THIS FLAG tg only grants "
        "approval and prints the preview; it does not execute anything."
    ),
)
@click.pass_context
def approve_action(ctx: click.Context, approval_id: str, execute: bool) -> None:
    """Grant an action approval. Does NOT execute unless --execute is present."""
    try:
        with _with_app(ctx) as app:
            granted = app.approvals.grant(approval_id, approved_by="cli")
            preview = app.approvals.dry_run(approval_id)
            executed = None
            if execute:
                executed = app.approvals.execute_approved(approval_id, actor="cli")
    except (KeyError, ApprovalRequiredError, RemediationExecutionError) as exc:
        raise click.ClickException(str(exc)) from exc
    data = {
        "approval_id": approval_id,
        "approval": (executed or granted).data.get("approval"),
        "executed": execute,
        "preview": preview,
    }
    text = (
        f"Granted approval for {approval_id}.\n"
        f"DRY RUN ONLY: not executed. Re-run with --execute to mutate the source system.\n"
        f"Preview: {preview}"
        if not execute
        else f"Executed approved action {approval_id}.\nPreview: {preview}"
    )
    _render(ctx, data, text)


@main.group()
def reject() -> None:
    """Reject proposed graph changes or actions."""


@reject.command("merge")
@click.argument("patch_id")
@click.option("--reason", required=True, help="Why this merge is wrong.")
@click.pass_context
def reject_merge(ctx: click.Context, patch_id: str, reason: str) -> None:
    """Reject a pending merge proposal."""
    try:
        with _with_app(ctx) as app:
            app.reject_merge(patch_id, reason, actor="cli")
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    data = {"patch_id": patch_id, "status": "rejected", "reason": reason}
    _render(ctx, data, f"Rejected merge {patch_id}: {reason}")


@reject.command("action")
@click.argument("approval_id")
@click.option("--reason", required=True, help="Why this action should not run.")
@click.pass_context
def reject_action(ctx: click.Context, approval_id: str, reason: str) -> None:
    """Reject a pending remediation approval."""
    try:
        with _with_app(ctx) as app:
            obj = app.approvals.reject(approval_id, reason=reason, actor="cli")
    except (KeyError, ApprovalRequiredError) as exc:
        raise click.ClickException(str(exc)) from exc
    data = _remediation_dict(obj)
    _render(ctx, data, f"Rejected action {approval_id}: {reason}")


@main.command()
@click.argument("task_id")
@click.option("--not-a-task", "not_a_task", is_flag=True, help="Drop this item from open work.")
@click.option("--higher", is_flag=True, help="Teach that this task should rank higher.")
@click.option("--lower", is_flag=True, help="Teach that this task should rank lower.")
@click.option("--reason", default="", help="Reason for the correction.")
@click.pass_context
def correct(
    ctx: click.Context,
    task_id: str,
    not_a_task: bool,
    higher: bool,
    lower: bool,
    reason: str,
) -> None:
    """Record a user correction for a task."""
    if sum(bool(v) for v in (not_a_task, higher, lower)) != 1:
        raise click.ClickException("Choose exactly one of --not-a-task, --higher or --lower.")
    with _with_app(ctx) as app:
        task = app.store.get_object(task_id)
        if task is None or task.type != ObjectType.TASK.value:
            _render(ctx, {"error": "task_not_found", "task_id": task_id}, _empty_message("task"))
            return
        if not_a_task:
            correction = app.corrector.not_a_task(task, rationale=reason, actor="cli")
        else:
            breakdown = app.explain_priority(task_id)
            app.graph.patch_object(
                task_id,
                {"priority": breakdown.score, "priority_factors": breakdown.factors},
                actor="cli",
            )
            task = app.store.get_object(task_id)
            correction = app.corrector.reprioritize_task(
                task,
                1.0 if higher else -1.0,
                rationale=reason,
                actor="cli",
            )
    data = {"correction_id": correction.id, "kind": correction.data.get("kind"), "task_id": task_id}
    _render(ctx, data, f"Recorded correction {correction.id} ({correction.data.get('kind')}).")


@main.command()
@click.pass_context
def learn(ctx: click.Context) -> None:
    """Fold pending corrections into learned weights."""
    reporter, console = _reporter(ctx)
    try:
        with _with_app(ctx, reporter) as app:
            reporter.phase("learn")
            report = app.learn()
    finally:
        _finish(console)
    _render(ctx, _learning_report(report), report.summary())


@main.command()
@click.option("--set", "assignment", help="Set a learned weight: SECTION.FEATURE=VALUE.")
@click.pass_context
def weights(ctx: click.Context, assignment: str | None) -> None:
    """Show or edit learned weights."""
    with _with_app(ctx) as app:
        if assignment:
            key, raw_value = _parse_assignment(assignment)
            _set_weight(app, key, float(raw_value))
            app.save_weights()
        data = app.weights.to_dict()
    _render(ctx, data, lambda: _format_weights(data))


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show local graph status."""
    with _with_app(ctx) as app:
        data = app.status()
    _render(ctx, data, lambda: "\n".join(f"{k}: {v}" for k, v in data.items()))


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Run environment and storage diagnostics."""
    reporter, console = _reporter(ctx)
    try:
        with _with_app(ctx, reporter) as app:
            checks = list(app.preflight(reporter))
            checks.extend(_storage_checks(app))
    finally:
        _finish(console)
    critical_failed = any((not c["ok"]) and c.get("critical") for c in checks)
    _render(ctx, {"checks": checks}, lambda: _format_checks(checks))
    if critical_failed:
        ctx.exit(1)


@main.command()
@click.pass_context
def rebuild(ctx: click.Context) -> None:
    """Rebuild the disposable graph projection from the event log."""
    reporter, console = _reporter(ctx)
    try:
        with _with_app(ctx, reporter) as app:
            data = app.rebuild(reporter)
    finally:
        _finish(console)
    _render(ctx, data, lambda: "\n".join(f"{k}: {v}" for k, v in data.items()))


@main.command()
@click.option(
    "--command",
    "-c",
    "commands",
    multiple=True,
    help="Run a command and exit instead of prompting; repeatable.",
)
@click.pass_context
def shell(ctx: click.Context, commands: tuple[str, ...]) -> None:
    """Start an interactive tg shell.

    Every tg command is available without the `tg` prefix, and they all share
    one process - so only the first one pays the import cost.
    """
    settings = _settings_from_context(ctx)
    base_args = _inherited_args(ctx)
    runner = JobRunner(settings.home)
    if commands:
        status_code = run_shell(main, base_args=base_args, lines=list(commands), runner=runner)
    else:
        status_code = run_shell(
            main,
            base_args=base_args,
            banner=banner_for(__version__, settings.home, len(runner.running())),
            runner=runner,
        )
    ctx.exit(status_code)


def _inherited_args(ctx: click.Context) -> list[str]:
    """Rebuild the group-level options this shell was started with.

    They are replayed ahead of every line so that `tg --json shell` keeps
    emitting JSON, while a line remains free to say `--no-json` and win.
    """
    obj = ctx.obj or {}
    args: list[str] = []
    home = obj.get("home")
    if home:
        args += ["--home", str(home)]
    if obj.get("json"):
        args.append("--json")
    args += ["-v"] * int(obj.get("verbose") or 0)
    return args


@main.command(
    "bg",
    # Everything after `bg` belongs to the job, not to bg. Without this Click
    # tries to resolve `--propose` as an option of bg itself and refuses, which
    # breaks every command worth backgrounding.
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.pass_context
def bg(ctx: click.Context, command: tuple[str, ...]) -> None:
    """Start a tg command as a background job and return immediately.

    Everything after `bg` is passed through untouched, so
    `tg bg sync --propose` runs exactly the sync you would have typed.
    """
    runner = _job_runner(ctx)
    job = runner.submit([*_inherited_args(ctx), *command])
    data = job.to_dict()
    _render(ctx, data, f"Started job {job.id}: {job.label}")


@main.command("jobs")
@click.option("--all", "show_all", is_flag=True, help="Include finished jobs.")
@click.option("--limit", default=15, show_default=True, type=int)
@click.pass_context
def jobs_cmd(ctx: click.Context, show_all: bool, limit: int) -> None:
    """List background jobs and what they are doing."""
    runner = _job_runner(ctx)
    found = runner.list(limit=limit, running_only=not show_all)
    rows = [job.to_dict() for job in found]
    _render(ctx, rows, lambda: _format_jobs(rows, show_all=show_all))


@main.command("logs")
@click.argument("job_id")
@click.option("--follow", "-f", is_flag=True, help="Keep printing until the job ends.")
@click.option("--tail", "-n", type=int, default=None, help="Only the last N lines.")
@click.pass_context
def logs_cmd(ctx: click.Context, job_id: str, follow: bool, tail: int | None) -> None:
    """Show a job's captured output."""
    runner = _job_runner(ctx)
    try:
        job = runner.get(job_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    if not follow:
        text = job.read_log(tail=tail)
        _render(ctx, {"id": job.id, "log": text}, text or "(no output yet)")
        return

    offset = 0
    click.echo(f"--- job {job.id}: {job.label} (Ctrl-C to stop watching) ---")
    try:
        while True:
            job = runner.get(job_id)
            text = job.read_log()
            if len(text) > offset:
                click.echo(text[offset:], nl=False)
                offset = len(text)
            if not job.is_running:
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        click.echo("\n(stopped watching; the job is still running)")
        return
    click.echo(f"--- job {job.id} {job.state} ---")


@main.command("cancel")
@click.argument("job_id")
@click.pass_context
def cancel_cmd(ctx: click.Context, job_id: str) -> None:
    """Stop a running job and everything it started."""
    runner = _job_runner(ctx)
    try:
        job = runner.cancel(job_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    _render(ctx, job.to_dict(), f"Job {job.id} {job.state}.")


def _job_runner(ctx: click.Context) -> JobRunner:
    return JobRunner(_settings_from_context(ctx).home)


def _format_jobs(rows: list[dict[str, Any]], *, show_all: bool) -> str:
    if not rows:
        return "No jobs running." if not show_all else "No jobs yet."
    lines = [f"{'ID':>4}  {'STATE':<10}  {'TIME':>7}  {'COMMAND':<24}  PROGRESS"]
    for row in rows:
        lines.append(
            f"{row['id']:>4}  {_truncate(row['state'], 10):<10}  "
            f"{row['seconds']:>6.1f}s  {_truncate(row['command'], 24):<24}  "
            f"{_truncate(row['progress'] or row['error'], 40)}"
        )
    return "\n".join(lines)


@main.command("init")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
@click.option("--print-config", is_flag=True, help="Emit only the task-graph MCP JSON snippet.")
@click.pass_context
def init_cmd(ctx: click.Context, dry_run: bool, print_config: bool) -> None:
    """Register the task-graph MCP server for Copilot CLI."""
    settings = _settings_from_context(ctx)
    snippet = _mcp_server_config(settings)
    if print_config:
        _echo_json({"task-graph": snippet})
        return

    path = _mcp_config_path()
    before = _read_mcp_config(path)
    after = json.loads(json.dumps(before))
    after.setdefault("mcpServers", {})["task-graph"] = snippet
    changed = before != after

    if not dry_run and changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
        path.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    data = {"path": str(path), "changed": changed, "dry_run": dry_run, "server": snippet}
    if ctx.obj["json"]:
        _echo_json(data)
    elif dry_run:
        click.echo("Would update " + str(path) if changed else f"{path} is already configured.")
    else:
        click.echo("Updated " + str(path) if changed else f"{path} is already configured.")


def _sync_report(report: SyncReport) -> dict[str, Any]:
    return {
        "summary": report.summary(),
        "ingest": report.ingest.__dict__,
        "dedupe": report.dedupe.__dict__,
        "crm": report.crm.__dict__,
        "ranked": report.ranked,
        "proposed_actions": report.proposed_actions,
        "errors": report.errors or [],
    }


def _learning_report(report: LearningReport) -> dict[str, Any]:
    return {
        "corrections_applied": report.corrections_applied,
        "weights_changed": report.weights_changed,
        "skipped": report.skipped,
        "summary": report.summary(),
    }


def _remediation_dict(obj: Object) -> dict[str, Any]:
    return {
        "id": obj.id,
        "action": obj.data.get("action"),
        "target_source": obj.data.get("target_source"),
        "target_uri": obj.data.get("target_uri"),
        "approval": obj.data.get("approval"),
        "confidence": obj.data.get("confidence"),
        "rationale": obj.data.get("rationale"),
        "preview": obj.data.get("preview"),
        "params": obj.data.get("params") or {},
    }


def _format_task_detail(data: dict[str, Any]) -> str:
    task = data["data"]
    lines = [
        f"{task.get('title') or data['id']}",
        f"ID: {data['id']}",
        f"State: {task.get('state', '-')}",
        f"Priority: {data['priority']['score']:.3f}",
        f"Why: {data['priority']['explanation']}",
        "",
        "Sources:",
    ]
    for source in data["sources"]:
        lines.append(
            f"  - {source.get('source')} {source.get('source_uri')}: "
            f"{source.get('title') or ''} {source.get('url') or ''}".rstrip()
        )
    lines.append("Factors:")
    for name, value in sorted(data["priority"]["factors"].items()):
        lines.append(f"  {name}: {value:.3f}")
    if data["related_tasks"]:
        lines.append("Related tasks:")
        for related in data["related_tasks"]:
            lines.append(f"  - {related['id']}: {related.get('title')}")
    return "\n".join(lines)


def _format_search(results: list[dict[str, Any]]) -> str:
    if not results:
        return _empty_message("matching tasks")
    lines = []
    for hit in results:
        lines.append(
            f"{hit['score']:.3f}  {hit['id']}  {hit.get('state') or '-'}  {hit.get('title')}"
        )
        if hit.get("snippet"):
            lines.append(f"       {hit['snippet']}")
    return "\n".join(lines)


def _format_breakdown(data: dict[str, Any]) -> str:
    lines = [f"Priority: {data['score']:.3f}", data["explanation"], "Factors:"]
    for name, value in sorted(data["factors"].items()):
        lines.append(f"  {name}: {value:.3f}")
    return "\n".join(lines)


def _format_merges(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return _empty_message("pending merges")
    lines = []
    for merge in pending:
        lines.append(
            f"{merge['patch_id']}  score={float(merge.get('score') or 0):.3f}\n"
            f"  into: {merge['canonical'].get('title')} ({merge['canonical'].get('id')})\n"
            f"  drop: {merge['absorbed'].get('title')} ({merge['absorbed'].get('id')})\n"
            f"  why: {merge.get('rationale')}"
        )
    return "\n".join(lines)


def _format_actions(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return _empty_message("pending actions")
    lines = []
    for action in pending:
        lines.append(
            f"{action['id']}  {action.get('action')}  {action.get('target_uri')}\n"
            f"  rationale: {action.get('rationale')}\n"
            f"  preview: {action.get('preview')}"
        )
    return "\n".join(lines)


def _format_weights(data: dict[str, Any]) -> str:
    lines = ["Dedupe:"]
    lines.extend(f"  {k}: {v:.3f}" for k, v in sorted(data["dedupe"].items()))
    lines.append("Priority:")
    lines.extend(f"  {k}: {v:.3f}" for k, v in sorted(data["priority"].items()))
    for key in ("auto_link_threshold", "propose_threshold", "learning_rate", "corrections_applied"):
        lines.append(f"{key}: {data[key]}")
    return "\n".join(lines)


def _format_checks(checks: list[dict[str, Any]]) -> str:
    lines = []
    for check in checks:
        marker = "OK" if check.get("ok") else ("FAIL" if check.get("critical") else "WARN")
        lines.append(f"[{marker}] {check.get('check')}: {check.get('detail')}")
        if check.get("remediation"):
            lines.append(f"       Fix: {check['remediation']}")
    return "\n".join(lines)


def _parse_assignment(assignment: str) -> tuple[str, str]:
    if "=" not in assignment:
        raise click.ClickException("--set must be SECTION.FEATURE=VALUE")
    key, value = assignment.split("=", 1)
    if "." not in key and key not in {
        "auto_link_threshold",
        "propose_threshold",
        "learning_rate",
    }:
        raise click.ClickException("--set must be SECTION.FEATURE=VALUE")
    return key, value


def _set_weight(app: TaskGraphApp, key: str, value: float) -> None:
    if "." not in key:
        if not hasattr(app.weights, key):
            raise click.ClickException(f"Unknown weight: {key}")
        setattr(app.weights, key, value)
        return
    section, feature = key.split(".", 1)
    table = getattr(app.weights, section, None)
    if not isinstance(table, dict) or feature not in table:
        raise click.ClickException(f"Unknown weight: {key}")
    table[feature] = value


def _storage_checks(app: TaskGraphApp) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    graph_ok, graph_detail = _sqlite_integrity(app.settings.graph_db)
    checks.append(
        {
            "check": "graph_db_integrity",
            "ok": graph_ok,
            "critical": True,
            "detail": graph_detail,
            "remediation": "Run `tg rebuild` from a healthy events.db." if not graph_ok else None,
        }
    )
    events_ok, events_detail = _sqlite_integrity(app.settings.events_db)
    checks.append(
        {
            "check": "events_db_integrity",
            "ok": events_ok,
            "critical": True,
            "detail": events_detail,
            "remediation": "Restore events.db from backup." if not events_ok else None,
        }
    )
    row = app.store.connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    version = row["value"] if row else None
    checks.append(
        {
            "check": "graph_schema_version",
            "ok": version == SCHEMA_VERSION,
            "critical": True,
            "detail": f"{version or 'missing'} (expected {SCHEMA_VERSION})",
            "remediation": "Run `tg rebuild`." if version != SCHEMA_VERSION else None,
        }
    )
    return checks


def _sqlite_integrity(path: Path) -> tuple[bool, str]:
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        return False, str(exc)
    result = str(row[0]) if row else "no result"
    return result.lower() == "ok", result


def _mcp_config_path() -> Path:
    override = os.environ.get("TASK_GRAPH_COPILOT_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".copilot"
    return base / "mcp-config.json"


def _read_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise click.ClickException(f"{path} must contain a JSON object.")
    data.setdefault("mcpServers", {})
    if not isinstance(data["mcpServers"], dict):
        raise click.ClickException(f"{path}: mcpServers must be an object.")
    return data


def _mcp_server_config(settings: Settings) -> dict[str, Any]:
    script = shutil.which("task-graph-mcp")
    if script:
        command = script
        args: list[str] = []
    else:
        root = Path(__file__).resolve().parents[2]
        command = str(root / ".venv" / "Scripts" / "python.exe")
        args = ["-m", "task_graph.mcpserver.server"]
    return {
        "command": command,
        "args": args,
        "env": {
            "TASK_GRAPH_HOME": str(settings.home),
            "TASK_GRAPH_EMBEDDINGS": settings.embedding_provider,
        },
    }


if __name__ == "__main__":
    main()
