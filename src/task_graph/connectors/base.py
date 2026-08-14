"""Shared connector contracts and stable source identities."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from task_graph.ontology.models import SourceItemProps
from task_graph.ontology.types import SourceKind

SourceItem = SourceItemProps


@dataclass(frozen=True)
class ConnectorStatus:
    """Cheap preflight result used before ingest attempts expensive work."""

    available: bool
    detail: str
    remediation: str | None = None


class ConnectorError(Exception):
    """Raised when a connector cannot fetch from its source."""


class Connector(Protocol):
    @property
    def kind(self) -> SourceKind: ...

    @property
    def name(self) -> str: ...

    def is_available(self) -> ConnectorStatus:
        """Return source readiness; this method must not raise."""

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        """Yield normalised source items changed since ``since`` when supported."""


ConnectorFactory = Callable[[], Connector]
registry: dict[SourceKind, ConnectorFactory] = {}


def register_connector(kind: SourceKind, factory: ConnectorFactory) -> None:
    """Registering a new source is intentionally one line in ``connectors.__init__``."""

    registry[kind] = factory


def get_connector(kind: SourceKind | str) -> Connector:
    try:
        source_kind = SourceKind(kind)
    except ValueError as exc:
        raise ConnectorError(f"Unknown connector kind: {kind}") from exc
    try:
        return registry[source_kind]()
    except KeyError as exc:
        raise ConnectorError(f"No connector registered for {source_kind.value}") from exc


def available_connectors() -> dict[SourceKind, ConnectorStatus]:
    statuses: dict[SourceKind, ConnectorStatus] = {}
    for kind, factory in registry.items():
        try:
            statuses[kind] = factory().is_available()
        except Exception as exc:  # pragma: no cover - defensive contract guard
            statuses[kind] = ConnectorStatus(False, str(exc))
    return statuses


def github_issue_uri(owner_repo: str, number: int | str) -> str:
    return _github_uri("issue", owner_repo, number)


def github_pr_uri(owner_repo: str, number: int | str) -> str:
    return _github_uri("pr", owner_repo, number)


def github_discussion_uri(owner_repo: str, number: int | str) -> str:
    return _github_uri("discussion", owner_repo, number)


def ado_workitem_uri(work_item_id: int | str) -> str:
    return f"ado:workitem:{work_item_id}"


def mail_uri(message_id: str) -> str:
    return f"mail:{message_id.strip()}"


def extract_external_refs(text: str | None) -> list[str]:
    """Extract explicit cross-system references for deterministic dedupe."""

    if not text:
        return []
    refs: list[str] = []
    patterns = [
        r"\bAB#\d+\b",
        r"(?<![\w/])#\d+\b",
        r"https?://[^\s<>)\"']+",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            ref = match.group(0).rstrip(".,;:?")
            if ref not in refs:
                refs.append(ref)
    return refs


def parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _github_uri(kind: str, owner_repo: str, number: int | str) -> str:
    return f"github:{kind}:{owner_repo.lower()}#{number}"
