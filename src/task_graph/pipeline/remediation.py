"""Typed remediation actions and conservative proposal rules."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from activegraph import Graph, Object
from pydantic import BaseModel, ConfigDict, Field

from task_graph.ontology.models import RemediationProps
from task_graph.ontology.types import (
    OPEN_STATES,
    ApprovalState,
    ObjectType,
    RelationType,
    SourceKind,
    TaskState,
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


class CustomerEmailParams(BaseModel):
    recipients: list[str] = Field(min_length=1)
    subject: str
    body: str
    related_id: str


class OpportunityStageParams(BaseModel):
    opportunity_id: str
    opportunity_name: str
    current_stage: str
    new_stage: str


class MsxMilestoneParams(BaseModel):
    milestone_id: str
    current_status: str
    new_status: str


class AccountTeamUpdateParams(BaseModel):
    destination: str
    message: str


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


def _execute_draft_customer_email(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "draft_customer_email",
        "mail_create_draft",
        {
            "to": params["recipients"],
            "subject": params["subject"],
            "body": params["body"],
            "related_id": params["related_id"],
        },
    )


@remediation_action("draft_customer_email", SourceKind.MAIL, CustomerEmailParams)
def _preview_draft_customer_email(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    recipients = ", ".join(params["recipients"])
    return f"Draft customer email to {recipients}: {params['subject']}\n\n{params['body']}"


def _execute_advance_opportunity_stage(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "advance_opportunity_stage",
        "msx_advance_opportunity_stage",
        {
            "opportunity_id": params["opportunity_id"],
            "stage": params["new_stage"],
        },
    )


@remediation_action(
    "advance_opportunity_stage", SourceKind.MSX, OpportunityStageParams, verified=False
)
def _preview_advance_opportunity_stage(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return (
        f"{params['opportunity_name']}: "
        f"{params['current_stage']} -> {params['new_stage']}"
    )


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


def _execute_post_account_team_update(client: Any, params: dict[str, Any]) -> str:
    return _call_client(
        client,
        "post_account_team_update",
        "teams_post_message",
        {"destination": params["destination"], "message": params["message"]},
    )


@remediation_action("post_account_team_update", SourceKind.TEAMS, AccountTeamUpdateParams)
def _preview_post_account_team_update(
    task: Object | Mapping[str, Any] | None, params: dict[str, Any]
) -> str:
    return f"Post account team update to {params['destination']}:\n\n{params['message']}"


def propose_remediations(
    graph: Graph, task: Object, *, proposed_by: str = "remediation"
) -> list[Object]:
    """Create pending remediation objects linked to ``task`` by ``REMEDIATES``."""

    if task.type != ObjectType.TASK:
        raise RemediationError(f"Expected task object, got {task.type}.")
    if str(task.data.get("state", "")) not in OPEN_STATES | {TaskState.DONE.value}:
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
    for rule in _GRAPH_PROPOSAL_RULES:
        yield from rule(graph, task)
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

GraphProposalRule = Callable[[Graph, Object], Iterable[RemediationProps]]


def _propose_customer_email_for_close_date(
    graph: Graph, task: Object
) -> Iterable[RemediationProps]:
    for opportunity in _related_objects(graph, task, RelationType.PART_OF, ObjectType.OPPORTUNITY):
        close_date = _parse_datetime(opportunity.data.get("close_date"))
        if close_date is None or close_date > datetime.now(UTC) + timedelta(days=14):
            continue
        if _has_recent_customer_contact(graph, task, opportunity):
            continue
        account = _first_related(graph, task, RelationType.ABOUT_ACCOUNT, ObjectType.ACCOUNT)
        recipients = _customer_recipients(graph, task)
        if not recipients:
            continue
        account_name = _name(account, "the account")
        opportunity_name = _name(opportunity, "the opportunity")
        close_text = close_date.date().isoformat()
        subject = f"Next steps for {opportunity_name}"
        body = (
            f"Hello,\n\n"
            f"I wanted to reconnect on {opportunity_name} for {account_name}. "
            f"The opportunity close date is {close_text}, and I want to make sure we are "
            "aligned on any remaining milestones, blockers, or decisions needed to move "
            "forward.\n\n"
            "Could you share any updates or confirm the best next step?\n\n"
            "Best,\n"
        )
        related_id = str(opportunity.data.get("msx_id") or opportunity.id)
        yield _build_proposal(
            "draft_customer_email",
            f"mail:draft:{related_id}",
            {
                "recipients": recipients,
                "subject": subject,
                "body": body,
                "related_id": related_id,
            },
            task,
            "The opportunity close date is near or past and no recent customer contact is "
            "recorded.",
            0.62,
        )


def _propose_done_account_team_update(graph: Graph, task: Object) -> Iterable[RemediationProps]:
    if _task_state(task) != "done_intent":
        return
    destination = _team_destination(graph, task)
    if destination is None:
        return
    opportunity = _first_related(graph, task, RelationType.PART_OF, ObjectType.OPPORTUNITY)
    milestone = _first_related(graph, task, RelationType.PART_OF, ObjectType.MILESTONE)
    if opportunity is None and milestone is None:
        return
    account = _first_related(graph, task, RelationType.ABOUT_ACCOUNT, ObjectType.ACCOUNT)
    context = _name(opportunity or milestone, "the related sales item")
    message = (
        f"Account team update: {task.data.get('title') or 'A task'} is marked done for "
        f"{context}"
    )
    if account is not None:
        message += f" ({_name(account, 'account')})"
    summary = str(task.data.get("summary") or "").strip()
    if summary:
        message += f". {summary}"
    yield _build_proposal(
        "post_account_team_update",
        f"teams:{destination}",
        {"destination": destination, "message": message},
        task,
        "The task is done; the account team may need an approved status update.",
        0.6,
    )


def _propose_done_opportunity_stage(graph: Graph, task: Object) -> Iterable[RemediationProps]:
    if _task_state(task) != "done_intent":
        return
    for opportunity in _related_objects(graph, task, RelationType.PART_OF, ObjectType.OPPORTUNITY):
        current_stage = str(opportunity.data.get("stage") or "Unknown")
        if current_stage.lower() in {"close", "closed", "won", "lost"}:
            continue
        opportunity_id = str(opportunity.data.get("msx_id") or opportunity.id)
        yield _build_proposal(
            "advance_opportunity_stage",
            f"msx:opportunity:{opportunity_id}",
            {
                "opportunity_id": opportunity_id,
                "opportunity_name": _name(opportunity, "Opportunity"),
                "current_stage": current_stage,
                "new_stage": "Close",
            },
            task,
            "The task is done and the related opportunity may be ready for the next MSX stage.",
            0.38,
        )


def _propose_done_milestone(graph: Graph, task: Object) -> Iterable[RemediationProps]:
    if _task_state(task) != "done_intent":
        return
    for milestone in _related_objects(graph, task, RelationType.PART_OF, ObjectType.MILESTONE):
        status = str(milestone.data.get("status") or "Unknown")
        if status.lower() in {"complete", "completed", "done"}:
            continue
        milestone_id = str(milestone.data.get("msx_id") or milestone.id)
        yield _build_proposal(
            "advance_milestone",
            f"msx:milestone:{milestone_id}",
            {
                "milestone_id": milestone_id,
                "current_status": status,
                "new_status": "Complete",
            },
            task,
            "The task is done and the related milestone may be ready to complete in MSX.",
            0.38,
        )


def _propose_past_due_milestone(graph: Graph, task: Object) -> Iterable[RemediationProps]:
    if _task_state(task) == "done_intent":
        return
    for milestone in _related_objects(graph, task, RelationType.PART_OF, ObjectType.MILESTONE):
        due_at = _parse_datetime(milestone.data.get("due_at"))
        if due_at is None or due_at >= datetime.now(UTC):
            continue
        status = str(milestone.data.get("status") or "Unknown")
        if status.lower() in {"complete", "completed", "done", "at risk"}:
            continue
        milestone_id = str(milestone.data.get("msx_id") or milestone.id)
        yield _build_proposal(
            "advance_milestone",
            f"msx:milestone:{milestone_id}",
            {
                "milestone_id": milestone_id,
                "current_status": status,
                "new_status": "At Risk",
            },
            task,
            "The related milestone is past due and should be flagged for seller review.",
            0.42,
        )


_GRAPH_PROPOSAL_RULES: tuple[GraphProposalRule, ...] = (
    _propose_customer_email_for_close_date,
    _propose_done_account_team_update,
    _propose_done_opportunity_stage,
    _propose_done_milestone,
    _propose_past_due_milestone,
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


def _related_objects(
    graph: Graph, task: Object, relation_type: RelationType, object_type: ObjectType
) -> list[Object]:
    related: dict[str, Object] = {}
    for rel in graph.relations(source=task.id, type=relation_type):
        obj = graph.get_object(rel.target)
        if obj is not None and obj.type == object_type:
            related[obj.id] = obj
    return list(related.values())


def _first_related(
    graph: Graph, task: Object, relation_type: RelationType, object_type: ObjectType
) -> Object | None:
    objects = _related_objects(graph, task, relation_type, object_type)
    return objects[0] if objects else None


def _customer_recipients(graph: Graph, task: Object) -> list[str]:
    recipients: list[str] = []
    for person in _related_objects(graph, task, RelationType.MENTIONS, ObjectType.PERSON):
        email = person.data.get("email") or person.data.get("upn")
        if email:
            recipients.append(str(email))
    return sorted(set(recipients))


def _team_destination(graph: Graph, task: Object) -> str | None:
    for key in ("account_team_chat_id", "account_team_channel_id", "teams_channel", "teams_chat"):
        if task.data.get(key):
            return str(task.data[key])
    account = _first_related(graph, task, RelationType.ABOUT_ACCOUNT, ObjectType.ACCOUNT)
    if account is None:
        return None
    for key in ("account_team_chat_id", "account_team_channel_id", "teams_channel", "teams_chat"):
        if account.data.get(key):
            return str(account.data[key])
    return None


def _has_recent_customer_contact(graph: Graph, task: Object, opportunity: Object) -> bool:
    cutoff = datetime.now(UTC) - timedelta(days=14)
    for obj in (task, opportunity):
        for key in ("last_customer_contact_at", "customer_contacted_at"):
            value = _parse_datetime(obj.data.get(key))
            if value is not None and value >= cutoff:
                return True
    for source_item in _source_items_for_task(graph, task):
        if source_item.data.get("source") != SourceKind.MAIL:
            continue
        value = _parse_datetime(
            source_item.data.get("updated_at") or source_item.data.get("created_at")
        )
        if value is not None and value >= cutoff:
            return True
    return False


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _name(obj: Object | None, fallback: str) -> str:
    if obj is None:
        return fallback
    return str(obj.data.get("name") or obj.data.get("title") or fallback)


def _parse_github_uri(uri: str) -> tuple[str, int]:
    _, _, rest = uri.partition("github:issue:")
    owner_repo, _, number = rest.partition("#")
    if not owner_repo or not number:
        raise RemediationError(f"Invalid GitHub issue URI: {uri}")
    return owner_repo, int(number)
