"""Object, relation and lifecycle vocabulary for the task graph.

activegraph deliberately types objects and relations with free strings; these
constants are the project's agreed vocabulary so connectors, dedupe, priority
and remediation all speak the same language.
"""

from __future__ import annotations

from enum import StrEnum


class ObjectType(StrEnum):
    """Node kinds in the graph."""

    #: A raw record as it exists in a source system (issue, work item, mail).
    SOURCE_ITEM = "source_item"
    #: Canonical unit of work. May unify several source items across systems.
    TASK = "task"
    PERSON = "person"
    ACCOUNT = "account"
    OPPORTUNITY = "opportunity"
    MILESTONE = "milestone"
    #: A repo, ADO project/area path, or board that groups work.
    PROJECT = "project"
    #: A proposed action that would move work forward. Never auto-executed.
    REMEDIATION = "remediation"
    #: A user correction to how something was modelled or remediated.
    CORRECTION = "correction"


class RelationType(StrEnum):
    """Typed directed edges."""

    #: source_item -> task. A source record backing a canonical task.
    EVIDENCE_OF = "EVIDENCE_OF"
    #: task <-> task. Symmetric "these track the same work" assertion.
    SAME_AS = "SAME_AS"
    #: task -> task. Asymmetric; the target is canonical.
    DUPLICATE_OF = "DUPLICATE_OF"
    #: task -> task. Source blocks target.
    BLOCKS = "BLOCKS"
    #: task -> task. Source depends on target.
    DEPENDS_ON = "DEPENDS_ON"
    #: task -> task. Weak association, surfaced as context only.
    RELATES_TO = "RELATES_TO"
    OWNED_BY = "OWNED_BY"
    MENTIONS = "MENTIONS"
    ABOUT_ACCOUNT = "ABOUT_ACCOUNT"
    #: task -> project, milestone -> opportunity.
    PART_OF = "PART_OF"
    #: remediation -> task.
    REMEDIATES = "REMEDIATES"
    #: correction -> any node the correction was about.
    CORRECTS = "CORRECTS"


#: Relations that are semantically symmetric. Traversal and dedupe treat an edge
#: in either direction as equivalent, so only one direction is ever stored.
SYMMETRIC_RELATIONS: frozenset[str] = frozenset({RelationType.SAME_AS})


class TaskState(StrEnum):
    """Lifecycle of a canonical task, independent of any source system's states."""

    #: Newly ingested, not yet reviewed by the user.
    TRIAGE = "triage"
    ACTIVE = "active"
    #: Cannot proceed because another task blocks it.
    BLOCKED = "blocked"
    #: Waiting on someone else; not actionable right now.
    WAITING = "waiting"
    DONE = "done"
    #: Explicitly decided against; retained so it is not re-ingested as new.
    DROPPED = "dropped"


#: States in which a task is a candidate for remediation proposals.
OPEN_STATES: frozenset[str] = frozenset(
    {TaskState.TRIAGE, TaskState.ACTIVE, TaskState.BLOCKED, TaskState.WAITING}
)

#: States that need no further action.
CLOSED_STATES: frozenset[str] = frozenset({TaskState.DONE, TaskState.DROPPED})


class ApprovalState(StrEnum):
    """Approval status of a proposed remediation.

    A remediation executor is unreachable unless its approval is ``GRANTED``.
    """

    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"
    #: Approved and successfully carried out against the source system.
    EXECUTED = "executed"
    FAILED = "failed"


class CorrectionKind(StrEnum):
    """What a user correction was about, so the learner can route it."""

    #: "These two are/aren't the same work."
    DEDUPE = "dedupe"
    #: "This is more/less important than you ranked it."
    PRIORITY = "priority"
    #: "This remediation is wrong / should be different."
    REMEDIATION = "remediation"
    #: "This field was extracted incorrectly from the source."
    FIELD_MAPPING = "field_mapping"
    #: "This isn't a task at all."
    NOT_A_TASK = "not_a_task"


#: Source systems recognised by the ingest pipeline. Adding a connector means
#: adding a member here and implementing ``connectors.base.Connector``.
class SourceKind(StrEnum):
    GITHUB = "github"
    ADO = "ado"
    MAIL = "mail"
    TEAMS = "teams"
    CALENDAR = "calendar"
    PLANNER = "planner"
    MSX = "msx"
