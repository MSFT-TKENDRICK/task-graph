"""Typed remediation actions and conservative proposal rules."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from activegraph import Graph, Object
from pydantic import BaseModel, ConfigDict

from task_graph.ontology.models import RemediationProps
from task_graph.ontology.types import (
    OPEN_STATES,
    ApprovalState,
    ObjectType,
    RelationType,
    SourceKind,
)


class RemediationError(Exception):
    """Raised for invalid or unsafe remediation definitions."""


class AdoStateParams(BaseModel):
    work_item_id: int | str
    current_state: str
    new_state: str


class GitHubCommentParams(BaseModel):
    owner_repo: str
    number: int
    body: str


class GitHubCloseIssueParams(BaseModel):
    owner_repo: str
    number: int
    reason: str | None = None


class MailReplyParams(BaseModel):
    message_id: str
    to: str
    subject: str
    body: str


class MsxMilestoneParams(BaseModel):
    milestone_id: str
    current_status: str
    new_status: str


class TeamsUpdateParams(BaseModel):
    channel: str
    message: str


class DictParams(BaseModel):
    model_config = ConfigDict(extra="allow")


RenderPreview = Callable[[Object | Mapping[str, Any] | None, dict[str, Any]], str]
Executor = Callable[[Any, dict[str, Any]], str]


@dataclass(frozen=True)
class RemediationAction:
    """A typed source-system mutation that can only be reached through approval."""

    name: str
    target_source: SourceKind
    params: type[BaseModel] | dict[str, Any]
    render_preview: RenderPreview | None = None
    executor: Executor | None = None
    verified: bool = True
    description: str = ""

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(self.params, type) and issubclass(self.params, BaseModel):
            return self.params.model_validate(dict(params)).model_dump(mode="json")
        return dict(params)

    def preview(self, task: Object | Mapping[str, Any] | None, params: Mapping[str, Any]) -> str:
        validated = self.validate_params(params)
        if self.render_preview is None:
            raise RemediationError(f"Action {self.name} does not define preview rendering.")
        return self.render_preview(task, validated)

    def execute(self, client: Any, params: Mapping[str, Any]) -> str:
        if not self.verified:
            raise RemediationError(
                f"{self.name} is propose-only until {self.target_source.value} write semantics "
                "are verified against a live tenant."
            )
        validated = self.validate_params(params)
        if self.executor is None:
            raise RemediationError(f"Action {self.name} does not define an executor.")
        return self.executor(client, validated)


_ACTIONS: dict[str, RemediationAction] = {}


def remediation_action(
    name: str,
    target_source: SourceKind,
    params: type[BaseModel] | dict[str, Any],
    *,
    verified: bool = True,
    description: str = "",
) -> Callable[[RenderPreview], RenderPreview]:
    """Register an action with one decorator on its preview function."""

    def decorator(func: RenderPreview) -> RenderPreview:
        executor = globals().get(f"_execute_{name}")
        action = RemediationAction(
            name=name,
            target_source=target_source,
            params=params,
            render_preview=func,
            executor=executor if callable(executor) else None,
            verified=verified,
            description=description,
        )
        if name in _ACTIONS:
            raise RemediationError(f"Duplicate remediation action registered: {name}")
        _ACTIONS[name] = action
        return func

    return decorator


def get_action(name: str) -> RemediationAction:
    try:
        return _ACTIONS[name]
    except KeyError as exc:
        raise RemediationError(f"Unknown remediation action: {name}") from exc


def all_actions() -> list[RemediationAction]:
    return list(_ACTIONS.values())


def _call_client(client: Any, method: str, tool: str, args: dict[str, Any]) -> str:
    if hasattr(client, method):
        result = getattr(client, method)(**args)
    elif hasattr(client, "call_tool"):
        result = client.call_tool(tool, args)
    else:
        raise RemediationError(
            f"Client cannot execute {method}; provide a fake or connector client with call_tool()."
        )
    if isinstance(result, dict):
        return str(result.get("outcome") or result.get("result") or result)
    return str(result)


def _execute_update_ado_state(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "update_ado_state",
        "ado_update_work_item",
        {"work_item_id": params["work_item_id"], "state": params["new_state"]},
    )


@remediation_action("update_ado_state", SourceKind.ADO, AdoStateParams)
def _preview_update_ado_state(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return (
        f"Set ADO #{params['work_item_id']} state: "
        f"{params['current_state']} \u2192 {params['new_state']}"
    )


def _execute_comment_github(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "comment_github",
        "github_add_issue_comment",
        {"owner_repo": params["owner_repo"], "number": params["number"], "body": params["body"]},
    )


@remediation_action("comment_github", SourceKind.GITHUB, GitHubCommentParams)
def _preview_comment_github(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return f"Comment on GitHub {params['owner_repo']}#{params['number']}: {params['body']}"


def _execute_close_github_issue(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "close_github_issue",
        "github_close_issue",
        {
            "owner_repo": params["owner_repo"],
            "number": params["number"],
            "reason": params.get("reason"),
        },
    )


@remediation_action("close_github_issue", SourceKind.GITHUB, GitHubCloseIssueParams)
def _preview_close_github_issue(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return f"Close GitHub issue {params['owner_repo']}#{params['number']}"


def _execute_draft_mail_reply(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "draft_mail_reply",
        "mail_create_reply_draft",
        {
            "message_id": params["message_id"],
            "to": params["to"],
            "subject": params["subject"],
            "body": params["body"],
        },
    )


@remediation_action("draft_mail_reply", SourceKind.MAIL, MailReplyParams)
def _preview_draft_mail_reply(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return f"Draft mail reply to {params['to']}: {params['subject']}"


def _execute_advance_milestone(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "advance_milestone",
        "msx_advance_milestone",
        {
            "milestone_id": params["milestone_id"],
            "status": params["new_status"],
        },
    )


@remediation_action("advance_milestone", SourceKind.MSX, MsxMilestoneParams, verified=False)
def _preview_advance_milestone(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return (
        f"Advance MSX milestone {params['milestone_id']}: "
        f"{params['current_status']} \u2192 {params['new_status']}"
    )


def _execute_post_teams_update(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "post_teams_update",
        "teams_post_message",
        {"channel": params["channel"], "message": params["message"]},
    )


@remediation_action("post_teams_update", SourceKind.TEAMS, TeamsUpdateParams)
def _preview_post_teams_update(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return f"Post Teams update to {params['channel']}: {params['message']}"


def propose_remediations(
    graph: Graph, task: Object, *, proposed_by: str = "remediation"
) -> list[Object]:
    """Create pending remediation objects linked to ``task`` by ``REMEDIATES``."""

    if task.type != ObjectType.TASK:
        raise RemediationError(f"Expected task object, got {task.type}.")
    if str(task.data.get("state", "")) not in OPEN_STATES:
        return []

    proposals: list[Object] = []
    seen: set[tuple[str, str, str]] = set()
    for proposal in _candidate_proposals(graph, task):
        key = (proposal.action, proposal.target_uri, proposal.preview)
        if key in seen:
            continue
        seen.add(key)
        remediation = graph.add_object(
            ObjectType.REMEDIATION,
            proposal.to_props(),
            actor=proposed_by,
            evidence=[task.id],
        )
        graph.add_relation(
            remediation.id,
            task.id,
            RelationType.REMEDIATES,
            data={"rationale": proposal.rationale},
        )
        proposals.append(remediation)
    return proposals


def _candidate_proposals(graph: Graph, task: Object) -> Iterable[RemediationProps]:
    for source_item in _source_items_for_task(graph, task):
        for rule in _PROPOSAL_RULES:
            proposal = rule(task, source_item)
            if proposal is not None:
                yield proposal
    if task.data.get("teams_channel"):
        yield _build_proposal(
            "post_teams_update",
            f"teams:channel:{task.data['teams_channel']}",
            {"channel": str(task.data["teams_channel"]), "message": _status_message(task)},
            task,
            "Post an approved status update for this active task.",
            0.45,
        )


def _source_items_for_task(graph: Graph, task: Object) -> list[Object]:
    items: dict[str, Object] = {}
    for rel in graph.relations(target=task.id, type=RelationType.EVIDENCE_OF):
        obj = graph.get_object(rel.source)
        if obj is not None and obj.type == ObjectType.SOURCE_ITEM:
            items[obj.id] = obj
    source_uris = set(task.data.get("source_uris") or [])
    if source_uris:
        for obj in graph.objects(ObjectType.SOURCE_ITEM):
            if obj.data.get("source_uri") in source_uris:
                items[obj.id] = obj
    return list(items.values())


def _build_proposal(
    action_name: str,
    target_uri: str,
    params: dict[str, Any],
    task: Object,
    rationale: str,
    confidence: float,
) -> RemediationProps:
    action = get_action(action_name)
    validated = action.validate_params(params)
    return RemediationProps(
        action=action.name,
        target_source=action.target_source,
        target_uri=target_uri,
        params=validated,
        preview=action.preview(task, validated),
        rationale=rationale,
        approval=ApprovalState.PENDING,
        confidence=confidence,
        proposed_at=datetime.now(UTC),
    )


def _propose_ado_resolution(task: Object, source_item: Object) -> RemediationProps | None:
    if source_item.data.get("source") != SourceKind.ADO:
        return None
    state = str(source_item.data.get("source_state") or "")
    if state.lower() in {"resolved", "closed", "done"}:
        return None
    if _task_state(task) != "done_intent":
        return None
    work_item_id = str(source_item.data.get("source_uri") or "").rsplit(":", 1)[-1]
    return _build_proposal(
        "update_ado_state",
        str(source_item.data["source_uri"]),
        {
            "work_item_id": work_item_id,
            "current_state": state or "Unknown",
            "new_state": "Resolved",
        },
        task,
        "The task appears complete while the backing ADO work item is still open.",
        0.7,
    )


def _propose_github_close(task: Object, source_item: Object) -> RemediationProps | None:
    if source_item.data.get("source") != SourceKind.GITHUB:
        return None
    uri = str(source_item.data.get("source_uri") or "")
    state = str(source_item.data.get("source_state") or "").lower()
    if ":issue:" not in uri or state not in {"open", ""} or _task_state(task) != "done_intent":
        return None
    owner_repo, number = _parse_github_uri(uri)
    return _build_proposal(
        "close_github_issue",
        uri,
        {"owner_repo": owner_repo, "number": number},
        task,
        "The task appears complete while the GitHub issue is still open.",
        0.68,
    )


def _propose_github_comment(task: Object, source_item: Object) -> RemediationProps | None:
    if source_item.data.get("source") != SourceKind.GITHUB:
        return None
    uri = str(source_item.data.get("source_uri") or "")
    if ":issue:" not in uri or _task_state(task) == "done_intent":
        return None
    owner_repo, number = _parse_github_uri(uri)
    return _build_proposal(
        "comment_github",
        uri,
        {"owner_repo": owner_repo, "number": number, "body": _status_message(task)},
        task,
        "A concise approved comment would move the GitHub thread forward.",
        0.5,
    )


def _propose_mail_reply(task: Object, source_item: Object) -> RemediationProps | None:
    if source_item.data.get("source") != SourceKind.MAIL:
        return None
    sender = source_item.data.get("owner")
    if not sender:
        return None
    message_id = str(source_item.data.get("source_uri") or "").removeprefix("mail:")
    return _build_proposal(
        "draft_mail_reply",
        str(source_item.data["source_uri"]),
        {
            "message_id": message_id,
            "to": sender,
            "subject": f"Re: {source_item.data.get('title') or task.data.get('title')}",
            "body": _status_message(task),
        },
        task,
        "Draft a reply for user review; no mail is sent automatically.",
        0.55,
    )


def _propose_msx_milestone(task: Object, source_item: Object) -> RemediationProps | None:
    if source_item.data.get("source") != SourceKind.MSX or _task_state(task) != "done_intent":
        return None
    status = str(source_item.data.get("source_state") or "Unknown")
    if status.lower() in {"complete", "completed", "done"}:
        return None
    milestone_id = str(source_item.data.get("source_uri") or "").rsplit(":", 1)[-1]
    return _build_proposal(
        "advance_milestone",
        str(source_item.data["source_uri"]),
        {"milestone_id": milestone_id, "current_status": status, "new_status": "Complete"},
        task,
        "MSX milestone advancement is propose-only until tenant write semantics are verified.",
        0.4,
    )


_PROPOSAL_RULES = (
    _propose_ado_resolution,
    _propose_github_close,
    _propose_github_comment,
    _propose_mail_reply,
    _propose_msx_milestone,
)


def _task_state(task: Object) -> str:
    text = (
        f"{task.data.get('state', '')} "
        f"{task.data.get('title', '')} "
        f"{task.data.get('summary', '')}"
    )
    lowered = text.lower()
    if any(token in lowered for token in ("done", "complete", "resolved", "fixed")):
        return "done_intent"
    return str(task.data.get("state") or "")


def _status_message(task: Object) -> str:
    summary = str(task.data.get("summary") or "").strip()
    title = str(task.data.get("title") or "this task").strip()
    return summary if summary else f"Update on {title}: work is in progress."


def _parse_github_uri(uri: str) -> tuple[str, int]:
    _, _, rest = uri.partition("github:issue:")
    owner_repo, _, number = rest.partition("#")
    if not owner_repo or not number:
        raise RemediationError(f"Invalid GitHub issue URI: {uri}")
    return owner_repo, int(number)
