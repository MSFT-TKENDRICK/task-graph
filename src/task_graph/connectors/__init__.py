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
from task_graph.connectors.github import GitHubConnector
from task_graph.connectors.mail import MailConnector, is_actionable_message
from task_graph.ontology.types import SourceKind

register_connector(SourceKind.GITHUB, GitHubConnector)
register_connector(SourceKind.ADO, AdoConnector)
register_connector(SourceKind.MAIL, MailConnector)

__all__ = [
    "AdoConnector",
    "Connector",
    "ConnectorError",
    "ConnectorStatus",
    "GitHubConnector",
    "MailConnector",
    "SourceItem",
    "ado_workitem_uri",
    "available_connectors",
    "extract_external_refs",
    "get_connector",
    "github_discussion_uri",
    "github_issue_uri",
    "github_pr_uri",
    "is_actionable_message",
    "mail_uri",
    "register_connector",
]
