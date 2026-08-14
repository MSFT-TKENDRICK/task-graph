"""Approval queue and the only executable remediation path."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from activegraph import Graph, Object, Policy

from task_graph.connectors.gh_client import GitHubCliClient
from task_graph.connectors.mcp_client import AgencyMcpClient
from task_graph.ontology.types import ApprovalState, ObjectType, SourceKind
from task_graph.pipeline.remediation import RemediationError, get_action

_VALID_PATCH_OPS = {"create", "update", "replace", "remove"}


class ApprovalRequiredError(Exception):
    """Raised whenever execution is attempted without a fresh granted approval."""


class RemediationExecutionError(Exception):
    """Raised after a failed execution attempt has been durably recorded."""


class ApprovalQueue:
    """Replayable approval state transitions over remediation objects."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    def pending(self) -> list[Object]:
        return [
            obj
            for obj in self.graph.objects(ObjectType.REMEDIATION)
            if obj.data.get("approval") == ApprovalState.PENDING
        ]

    def get(self, remediation_id: str) -> Object:
        remediation = self.graph.get_object(remediation_id)
        if remediation is None or remediation.type != ObjectType.REMEDIATION:
            raise KeyError(f"Remediation not found: {remediation_id}")
        return remediation

    def grant(self, remediation_id: str, approved_by: str, note: str | None = None) -> Object:
        remediation = self.get(remediation_id)
        if remediation.data.get("approval") != ApprovalState.PENDING:
            raise ApprovalRequiredError(
                f"Only pending remediations can be granted; current state is "
                f"{remediation.data.get('approval')}."
            )
        value: dict[str, Any] = {
            "approval": ApprovalState.GRANTED.value,
            "resolved_at": datetime.now(UTC).isoformat(),
            "outcome": note,
        }
        _apply_recorded_update(
            self.graph,
            remediation_id,
            value,
            actor=approved_by,
            rationale=note or "User granted remediation approval.",
        )
        return self.get(remediation_id)

    def reject(self, remediation_id: str, reason: str, actor: str) -> Object:
        remediation = self.get(remediation_id)
        if remediation.data.get("approval") in {ApprovalState.EXECUTED, ApprovalState.FAILED}:
            raise ApprovalRequiredError(
                f"Cannot reject remediation in terminal state {remediation.data.get('approval')}."
            )
        _apply_recorded_update(
            self.graph,
            remediation_id,
            {
                "approval": ApprovalState.REJECTED.value,
                "resolved_at": datetime.now(UTC).isoformat(),
                "outcome": reason,
            },
            actor=actor,
            rationale=reason,
        )
        return self.get(remediation_id)

    def dry_run(self, remediation_id: str) -> str:
        return str(self.get(remediation_id).data.get("preview") or "")

    def execute_approved(
        self,
        remediation_id: str,
        *,
        client: Any | None = None,
        clients: Mapping[str, Any] | None = None,
        actor: str = "system",
    ) -> Object:
        remediation = self.get(remediation_id)
        approval = remediation.data.get("approval")
        if approval != ApprovalState.GRANTED:
            raise ApprovalRequiredError(
                f"Remediation {remediation_id} is {approval}; execution requires granted approval."
            )

        try:
            action = get_action(str(remediation.data["action"]))
            executor_client = _resolve_client(remediation, client=client, clients=clients)
            outcome = action.execute(executor_client, remediation.data.get("params") or {})
        except Exception as exc:
            _apply_recorded_update(
                self.graph,
                remediation_id,
                {
                    "approval": ApprovalState.FAILED.value,
                    "resolved_at": datetime.now(UTC).isoformat(),
                    "outcome": str(exc),
                },
                actor=actor,
                rationale=f"Remediation execution failed: {exc}",
            )
            raise RemediationExecutionError(str(exc)) from exc

        _apply_recorded_update(
            self.graph,
            remediation_id,
            {
                "approval": ApprovalState.EXECUTED.value,
                "resolved_at": datetime.now(UTC).isoformat(),
                "outcome": outcome,
            },
            actor=actor,
            rationale="Approved remediation executed.",
        )
        return self.get(remediation_id)


def dry_run(graph: Graph, remediation_id: str) -> str:
    return ApprovalQueue(graph).dry_run(remediation_id)


def execute_approved(
    graph: Graph,
    remediation_id: str,
    *,
    client: Any | None = None,
    clients: Mapping[str, Any] | None = None,
    actor: str = "system",
) -> Object:
    return ApprovalQueue(graph).execute_approved(
        remediation_id, client=client, clients=clients, actor=actor
    )


def remediation_policy() -> Policy:
    """Runtime policy mirror: remediation objects require approval."""

    return Policy(requires_approval=[ObjectType.REMEDIATION.value])


def _resolve_client(
    remediation: Object, *, client: Any | None, clients: Mapping[str, Any] | None
) -> Any:
    if client is not None:
        return client
    source = str(remediation.data.get("target_source") or "")
    if clients is not None and source in clients:
        return clients[source]
    if clients is not None:
        action = str(remediation.data.get("action") or "")
        if action in clients:
            return clients[action]
    if not source:
        raise RemediationError("Remediation has no target_source.")
    # GitHub has no Agency MCP server; its write path is the `gh` CLI, matching
    # how the read connector authenticates.
    if source == SourceKind.GITHUB.value:
        return GitHubCliClient()
    return AgencyMcpClient(source)


def _apply_recorded_update(
    graph: Graph,
    target: str,
    value: dict[str, Any],
    *,
    actor: str,
    rationale: str,
) -> None:
    _validate_patch_op("update")
    patch = graph.propose_patch(target, "update", value, proposed_by=actor, rationale=rationale)
    graph.apply_patch(patch.id, approved_by=actor)


def _validate_patch_op(op: str) -> None:
    if op not in _VALID_PATCH_OPS:
        raise ValueError(f"Invalid activegraph patch op: {op}")
