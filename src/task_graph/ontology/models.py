"""Typed property bags for graph objects.

activegraph stores object properties as a plain ``dict``. These models validate
and document the shape of those dicts at the boundaries (connectors in, MCP
tools out) while keeping the stored form JSON-native.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from task_graph.ontology.types import (
    ApprovalState,
    CorrectionKind,
    SourceKind,
    TaskState,
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=True)

    def to_props(self) -> dict[str, Any]:
        """Serialise to the JSON-native dict stored on a graph object."""
        return self.model_dump(mode="json", exclude_none=True)


class SourceItemProps(_Base):
    """A record as it exists in a source system, normalised but not interpreted."""

    source: SourceKind
    #: Stable, globally unique identity for this record. Ingest is idempotent on
    #: this value, so it must not change between syncs (e.g.
    #: ``github:issue:owner/repo#123``, ``ado:workitem:12345``, ``mail:<msg-id>``).
    source_uri: str
    title: str
    body: str = ""
    url: str | None = None
    #: The source system's own state string, preserved verbatim for remediation.
    source_state: str | None = None
    owner: str | None = None
    assignees: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    due_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    #: Identifiers this record explicitly references (other work items, PRs,
    #: opportunity numbers). A strong dedupe signal.
    external_refs: list[str] = Field(default_factory=list)
    #: Untouched source payload, retained so connectors can be re-interpreted
    #: without re-fetching.
    raw: dict[str, Any] = Field(default_factory=dict)


class TaskProps(_Base):
    """A canonical unit of work, potentially unifying several source items."""

    title: str
    summary: str = ""
    state: TaskState = TaskState.TRIAGE
    owner: str | None = None
    due_at: datetime | None = None
    #: Cached priority score. Always reproducible from ``priority.score()``.
    priority: float | None = None
    #: Per-factor contributions behind ``priority``, for explainability.
    priority_factors: dict[str, float] = Field(default_factory=dict)
    #: ``source_uri`` values of every source item backing this task.
    source_uris: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class PersonProps(_Base):
    display_name: str
    email: str | None = None
    upn: str | None = None
    aliases: list[str] = Field(default_factory=list)


class AccountProps(_Base):
    name: str
    msx_account_id: str | None = None
    tpid: str | None = None


class OpportunityProps(_Base):
    name: str
    msx_id: str | None = None
    stage: str | None = None
    estimated_value: float | None = None
    close_date: datetime | None = None


class MilestoneProps(_Base):
    name: str
    msx_id: str | None = None
    status: str | None = None
    due_at: datetime | None = None


class ProjectProps(_Base):
    """A repo, ADO area path, or board that groups work."""

    name: str
    source: SourceKind
    url: str | None = None


class RemediationProps(_Base):
    """A proposed action that would move a task forward.

    Created in :attr:`ApprovalState.PENDING`. The executor for ``action`` is
    unreachable until approval is granted.
    """

    action: str
    #: Target source system the action would mutate.
    target_source: SourceKind
    #: The exact record the action would mutate.
    target_uri: str
    #: Action-specific arguments (new state, comment body, recipients...).
    params: dict[str, Any] = Field(default_factory=dict)
    #: Human-readable description of precisely what would change. Rendered
    #: before approval so the effect is never a surprise.
    preview: str = ""
    rationale: str = ""
    approval: ApprovalState = ApprovalState.PENDING
    confidence: float = 0.0
    proposed_at: datetime | None = None
    resolved_at: datetime | None = None
    #: Populated on failure or rejection.
    outcome: str | None = None


class CorrectionProps(_Base):
    """A user correction, retained as a first-class fact to learn from."""

    kind: CorrectionKind
    #: Objects/relations the correction was about.
    subject_ids: list[str] = Field(default_factory=list)
    #: What the system had concluded.
    before: dict[str, Any] = Field(default_factory=dict)
    #: What the user says is correct.
    after: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    created_at: datetime | None = None
    #: Set once the learner has folded this correction into the weights.
    applied: bool = False


class PriorityBreakdown(BaseModel):
    """Explainable priority result."""

    score: float
    factors: dict[str, float]
    explanation: str

    model_config = ConfigDict(extra="forbid")


#: Maps object type -> model, so callers can validate a property bag generically.
PROPS_MODELS: dict[str, type[_Base]] = {
    "source_item": SourceItemProps,
    "task": TaskProps,
    "person": PersonProps,
    "account": AccountProps,
    "opportunity": OpportunityProps,
    "milestone": MilestoneProps,
    "project": ProjectProps,
    "remediation": RemediationProps,
    "correction": CorrectionProps,
}
