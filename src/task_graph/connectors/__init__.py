"""Public connector API and built-in registrations."""

from __future__ import annotations

from task_graph.connectors.ado import AdoConnector
from task_graph.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorStatus,
    SourceItem,
    ado_workitem_uri,
    available_connectors,
    extract_external_refs,
    get_connector,
    github_discussion_uri,
    github_issue_uri,
    github_pr_uri,
    mail_uri,
    register_connector,
)
from task_graph.connectors.calendar import (
    CalendarConnector,
    calendar_uri,
    is_actionable_event,
)
from task_graph.connectors.github import GitHubConnector
from task_graph.connectors.mail import MailConnector, is_actionable_message
from task_graph.connectors.mcp_client import (
    MUTATING_VERBS,
    AgencyMcpClient,
    assert_read_only,
    is_mutating_tool,
)
from task_graph.connectors.msx import (
    MsxConnector,
    msx_activity_uri,
    msx_milestone_uri,
    msx_opportunity_uri,
)
from task_graph.connectors.planner import PlannerConnector, planner_uri
from task_graph.connectors.teams import (
    TeamsConnector,
    is_actionable_teams_message,
    teams_uri,
)
from task_graph.ontology.types import SourceKind

register_connector(SourceKind.GITHUB, GitHubConnector)
register_connector(SourceKind.ADO, AdoConnector)
register_connector(SourceKind.MAIL, MailConnector)
register_connector(SourceKind.TEAMS, TeamsConnector)
register_connector(SourceKind.CALENDAR, CalendarConnector)
register_connector(SourceKind.PLANNER, PlannerConnector)
register_connector(SourceKind.MSX, MsxConnector)

__all__ = [
    "MUTATING_VERBS",
    "AdoConnector",
    "AgencyMcpClient",
    "CalendarConnector",
    "Connector",
    "ConnectorError",
    "ConnectorStatus",
    "GitHubConnector",
    "MailConnector",
    "MsxConnector",
    "PlannerConnector",
    "SourceItem",
    "TeamsConnector",
    "ado_workitem_uri",
    "assert_read_only",
    "available_connectors",
    "calendar_uri",
    "extract_external_refs",
    "get_connector",
    "github_discussion_uri",
    "github_issue_uri",
    "github_pr_uri",
    "is_actionable_event",
    "is_actionable_message",
    "is_actionable_teams_message",
    "is_mutating_tool",
    "mail_uri",
    "msx_activity_uri",
    "msx_milestone_uri",
    "msx_opportunity_uri",
    "planner_uri",
    "register_connector",
    "teams_uri",
]
