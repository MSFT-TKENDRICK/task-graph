"""Project CRM entities from already-ingested source records.

The CRM projector makes deal context queryable without changing ingest: raw source
items remain the evidence, while accounts, opportunities, milestones and people
are derived idempotently from their stable source payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from activegraph import Graph, Object

from task_graph.connectors.base import parse_datetime
from task_graph.ontology.models import (
    AccountProps,
    MilestoneProps,
    OpportunityProps,
    PersonProps,
)
from task_graph.ontology.types import ObjectType, RelationType, SourceKind
from task_graph.store.sqlite_graph_store import SqliteGraphStore

_LEGAL_SUFFIXES = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "plc",
    }
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class CrmReport:
    """Counts of projected CRM context surfaced by sync."""

    sources_seen: int = 0
    accounts_created: int = 0
    opportunities_created: int = 0
    milestones_created: int = 0
    people_created: int = 0
    objects_updated: int = 0
    relations_created: int = 0
    errors: list[str] | None = None

    @property
    def entities_created(self) -> int:
        return (
            self.accounts_created
            + self.opportunities_created
            + self.milestones_created
            + self.people_created
        )

    def summary(self) -> str:
        parts = [
            f"{self.entities_created} CRM entities projected",
            f"{self.relations_created} CRM relation(s) linked",
        ]
        if self.objects_updated:
            parts.append(f"{self.objects_updated} updated")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


@dataclass(frozen=True)
class _PersonCandidate:
    display_name: str
    email: str | None = None
    upn: str | None = None
    aliases: tuple[str, ...] = ()


class CrmProjector:
    """Derives stable CRM/account-team nodes from source item payloads."""

    def __init__(self, graph: Graph, store: SqliteGraphStore) -> None:
        self.graph = graph
        self.store = store

    def run(self, actor: str = "crm") -> CrmReport:
        report = CrmReport(errors=[])
        with self.store.bulk_writes():
            for source in self.store.find_objects(ObjectType.SOURCE_ITEM.value):
                try:
                    self._project_source(source, actor, report)
                except Exception as exc:  # one malformed CRM payload must not break sync
                    uri = source.data.get("source_uri", source.id)
                    report.errors.append(f"{uri}: {exc}")
        return report

    # ---------------------------------------------------------------- source

    def _project_source(self, source: Object, actor: str, report: CrmReport) -> None:
        source_kind = _source_kind(source)
        raw = source.data.get("raw") if isinstance(source.data.get("raw"), dict) else {}
        if source_kind not in {SourceKind.MSX, SourceKind.MAIL, SourceKind.TEAMS}:
            return

        tasks = self._tasks_for_source(source.id)
        if not tasks:
            return

        projected = False
        account: Object | None = None
        opportunity: Object | None = None
        milestone: Object | None = None

        if source_kind is SourceKind.MSX:
            account = self._account_from(raw, actor, report)
            opportunity = self._opportunity_from(source, raw, actor, report)
            milestone = self._milestone_from(source, raw, actor, report)
            projected = any((account, opportunity, milestone))

            if account is not None and opportunity is not None:
                self._ensure_relation(
                    opportunity.id, account.id, RelationType.ABOUT_ACCOUNT.value, actor, report
                )
            if milestone is not None and opportunity is not None:
                self._ensure_relation(
                    milestone.id, opportunity.id, RelationType.PART_OF.value, actor, report
                )

        people = self._people_from(source, raw, source_kind)
        if people:
            projected = True

        if not projected:
            return
        report.sources_seen += 1

        for task in tasks:
            if account is not None:
                self._ensure_relation(
                    task.id, account.id, RelationType.ABOUT_ACCOUNT.value, actor, report
                )
            if opportunity is not None:
                self._ensure_relation(
                    task.id, opportunity.id, RelationType.PART_OF.value, actor, report
                )
            self._link_people(task.id, people, actor, report)

    def _tasks_for_source(self, source_id: str) -> list[Object]:
        out: list[Object] = []
        for rel in self.store.find_relations(source=source_id, type=RelationType.EVIDENCE_OF.value):
            task = self.store.get_object(rel.target)
            if task is not None and task.type == ObjectType.TASK.value:
                out.append(task)
        return out

    # --------------------------------------------------------------- accounts

    def _account_from(self, raw: dict[str, Any], actor: str, report: CrmReport) -> Object | None:
        name = _clean_text(_first(raw, "accountName", "customerName", "account", "customer"))
        account_id = _clean_text(
            _first(raw, "msx_account_id", "msxAccountId", "accountId", "customerId")
        )
        tpid = _clean_text(_first(raw, "tpid", "tpId", "topParentId"))
        if not (name or account_id or tpid):
            return None

        display_name = name or account_id or tpid or "Unknown account"
        normalised = normalise_account_name(display_name)
        existing = self._find_account(account_id=account_id, tpid=tpid, normalised_name=normalised)
        if existing is not None:
            display_name = str(existing.data.get("name") or display_name)
        updates = AccountProps(name=display_name, msx_account_id=account_id, tpid=tpid).to_props()
        updates["normalized_name"] = normalised
        updates["crm_key"] = f"account:{account_id or tpid or normalised}"
        return self._upsert_entity(
            existing,
            ObjectType.ACCOUNT.value,
            updates,
            actor,
            report,
            created_field="accounts_created",
        )

    def _find_account(
        self, *, account_id: str | None, tpid: str | None, normalised_name: str
    ) -> Object | None:
        for account in self.store.find_objects(ObjectType.ACCOUNT.value):
            if account_id and account.data.get("msx_account_id") == account_id:
                return account
            if tpid and account.data.get("tpid") == tpid:
                return account
            if normalised_name and account.data.get("normalized_name") == normalised_name:
                return account
        return None

    # ----------------------------------------------------------- opportunities

    def _opportunity_from(
        self, source: Object, raw: dict[str, Any], actor: str, report: CrmReport
    ) -> Object | None:
        source_uri = str(source.data.get("source_uri") or "")
        is_opportunity_source = source_uri.startswith("msx:opportunity:")
        msx_id = _clean_text(_first(raw, "opportunityid", "opportunityId", "opportunityNumber"))
        if not msx_id and is_opportunity_source:
            msx_id = _clean_text(_first(raw, "msxId", "id")) or source_uri.rsplit(":", 1)[-1]
        name_keys = ("name", "topic", "opportunityName", "title") if is_opportunity_source else (
            "opportunityName",
            "opportunity",
        )
        name = _clean_text(_first(raw, *name_keys))
        if not (msx_id or name):
            return None

        stage = _clean_text(
            _first(raw, "salesStage", "stage", "stageName", "status", "state", "statusReason")
        )
        estimated_value = _float_or_none(
            _first(raw, "estimatedValue", "estimatedRevenue", "estimatedvalue", "revenue")
        )
        close_date = parse_datetime(
            _first(raw, "closeDate", "estimatedCloseDate", "estimatedclosedate", "dueDate")
        )
        updates = OpportunityProps(
            name=name or str(source.data.get("title") or msx_id or "MSX opportunity"),
            msx_id=msx_id,
            stage=stage if is_opportunity_source else None,
            estimated_value=estimated_value if is_opportunity_source else None,
            close_date=close_date if is_opportunity_source else None,
        ).to_props()
        key = msx_id or normalise_account_name(updates["name"])
        updates["crm_key"] = f"opportunity:{key}"
        existing = self._find_by_key(ObjectType.OPPORTUNITY.value, updates["crm_key"])
        return self._upsert_entity(
            existing,
            ObjectType.OPPORTUNITY.value,
            updates,
            actor,
            report,
            created_field="opportunities_created",
        )

    # --------------------------------------------------------------- milestone

    def _milestone_from(
        self, source: Object, raw: dict[str, Any], actor: str, report: CrmReport
    ) -> Object | None:
        source_uri = str(source.data.get("source_uri") or "")
        is_milestone_source = source_uri.startswith("msx:milestone:")
        msx_id = _clean_text(_first(raw, "milestoneid", "milestoneId"))
        if not msx_id and is_milestone_source:
            msx_id = _clean_text(_first(raw, "msxId", "id")) or source_uri.rsplit(":", 1)[-1]
        name_keys = ("name", "title", "milestoneName", "subject") if is_milestone_source else (
            "milestoneName",
        )
        name = _clean_text(_first(raw, *name_keys))
        if not (msx_id or name):
            return None

        updates = MilestoneProps(
            name=name or str(source.data.get("title") or msx_id or "MSX milestone"),
            msx_id=msx_id,
            status=_clean_text(_first(raw, "status", "state", "milestoneStatus", "statusReason")),
            due_at=parse_datetime(_first(raw, "dueDate", "targetDate", "milestoneDate")),
        ).to_props()
        key = msx_id or normalise_account_name(updates["name"])
        updates["crm_key"] = f"milestone:{key}"
        existing = self._find_by_key(ObjectType.MILESTONE.value, updates["crm_key"])
        return self._upsert_entity(
            existing,
            ObjectType.MILESTONE.value,
            updates,
            actor,
            report,
            created_field="milestones_created",
        )

    # ---------------------------------------------------------------- people

    def _people_from(
        self, source: Object, raw: dict[str, Any], source_kind: SourceKind
    ) -> list[_PersonCandidate]:
        owner_values: list[Any] = []
        other_values: list[Any] = []

        if source.data.get("owner"):
            owner_values.append(source.data["owner"])
        if source_kind is SourceKind.MSX:
            owner_values.append(_first(raw, "owner", "ownerid", "seller", "primarySeller"))
            team_keys = (
                "dealTeam",
                "teamMembers",
                "salesTeam",
                "assignedTo",
                "owners",
                "participants",
            )
            for key in team_keys:
                other_values.extend(_as_list(_first(raw, key)))
            other_values.extend(source.data.get("assignees") or [])
        elif source_kind is SourceKind.MAIL:
            owner_values.append(_first(raw, "from", "sender"))
        elif source_kind is SourceKind.TEAMS:
            owner_values.append(_first(raw, "from", "sender"))
            other_values.extend(
                _as_list(_first(raw, "mentions", "mentioned", "participants", "members"))
            )

        people: list[_PersonCandidate] = []
        seen: set[tuple[str | None, str | None, str]] = set()
        for value in [*owner_values, *other_values]:
            candidate = person_candidate(value)
            if candidate is None:
                continue
            key = (candidate.email, candidate.upn, normalise_person_name(candidate.display_name))
            if key in seen:
                continue
            seen.add(key)
            people.append(candidate)
        return people

    def _link_people(
        self,
        task_id: str,
        people: list[_PersonCandidate],
        actor: str,
        report: CrmReport,
    ) -> None:
        owner_linked = False
        for candidate in people:
            person = self._upsert_person(candidate, actor, report)
            rel_type = (
                RelationType.OWNED_BY.value if not owner_linked else RelationType.MENTIONS.value
            )
            self._ensure_relation(task_id, person.id, rel_type, actor, report)
            owner_linked = True

    def _upsert_person(
        self, candidate: _PersonCandidate, actor: str, report: CrmReport
    ) -> Object:
        existing = self._find_person(candidate)
        existing_aliases = existing.data.get("aliases") if existing else []
        aliases = sorted(_person_aliases(candidate, existing_aliases))
        updates = PersonProps(
            display_name=candidate.display_name,
            email=candidate.email,
            upn=candidate.upn,
            aliases=aliases,
        ).to_props()
        updates["crm_key"] = _person_key(candidate)
        return self._upsert_entity(
            existing,
            ObjectType.PERSON.value,
            updates,
            actor,
            report,
            created_field="people_created",
        )

    def _find_person(self, candidate: _PersonCandidate) -> Object | None:
        candidate_keys = _person_match_keys(candidate)
        for person in self.store.find_objects(ObjectType.PERSON.value):
            existing = _person_match_keys(
                _PersonCandidate(
                    display_name=str(person.data.get("display_name") or ""),
                    email=_normalise_email(person.data.get("email")),
                    upn=_normalise_email(person.data.get("upn")),
                    aliases=tuple(str(a) for a in person.data.get("aliases") or []),
                )
            )
            if candidate_keys & existing:
                return person
        return None

    # --------------------------------------------------------------- mutation

    def _find_by_key(self, obj_type: str, crm_key: str) -> Object | None:
        for obj in self.store.find_objects(obj_type):
            if obj.data.get("crm_key") == crm_key:
                return obj
        return None

    def _upsert_entity(
        self,
        existing: Object | None,
        obj_type: str,
        updates: dict[str, Any],
        actor: str,
        report: CrmReport,
        *,
        created_field: str,
    ) -> Object:
        if existing is None:
            created = self.graph.add_object(obj_type, updates, actor=actor)
            setattr(report, created_field, getattr(report, created_field) + 1)
            return created
        if existing.data.get("crm_key"):
            updates.pop("crm_key", None)
        changed = {key: value for key, value in updates.items() if existing.data.get(key) != value}
        if changed:
            self.graph.patch_object(existing.id, changed, actor=actor)
            report.objects_updated += 1
            return self.store.get_object(existing.id) or existing
        return existing

    def _ensure_relation(
        self, source: str, target: str, rel_type: str, actor: str, report: CrmReport
    ) -> None:
        if self.store.find_relations(source=source, target=target, type=rel_type):
            return
        self.graph.add_relation(source, target, rel_type, actor=actor)
        report.relations_created += 1


# --------------------------------------------------------------- resolution


def normalise_account_name(value: str | None) -> str:
    """Canonicalise account names enough to absorb legal suffix variants."""

    if not value:
        return ""
    words = _WORD_RE.findall(value.casefold())
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalise_person_name(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(_WORD_RE.findall(value.casefold()))


def normalise_alias(value: str | None) -> str:
    if not value:
        return ""
    return "".join(_WORD_RE.findall(value.casefold()))


def person_candidate(value: Any) -> _PersonCandidate | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip()
        email = _normalise_email(text) if _EMAIL_RE.match(text) else None
        display_name = _display_from_email(email) if email else text
        return _PersonCandidate(display_name=display_name, email=email)
    if not isinstance(value, dict):
        return person_candidate(str(value))

    user = value.get("user") if isinstance(value.get("user"), dict) else value
    email = _normalise_email(
        _first(user, "email", "mail", "address", "smtp", "userPrincipalName", "upn")
    )
    email_address = user.get("emailAddress")
    if not email and isinstance(email_address, dict):
        email = _normalise_email(_first(email_address, "address", "email", "mail"))
    upn = _normalise_email(_first(user, "upn", "userPrincipalName"))
    display = _clean_text(
        _first(user, "displayName", "name", "fullName")
        or (email_address.get("name") if isinstance(email_address, dict) else None)
    )
    display_name = display or _display_from_email(email or upn) or email or upn
    aliases: list[str] = []
    for key in ("alias", "aliases", "id"):
        aliases.extend(str(item) for item in _as_list(user.get(key)) if item)
    if not display_name:
        return None
    return _PersonCandidate(display_name=display_name, email=email, upn=upn, aliases=tuple(aliases))


def _person_key(candidate: _PersonCandidate) -> str:
    if candidate.email:
        return f"person:email:{candidate.email}"
    if candidate.upn:
        return f"person:upn:{candidate.upn}"
    return f"person:name:{normalise_person_name(candidate.display_name)}"


def _person_match_keys(candidate: _PersonCandidate) -> set[str]:
    keys = {normalise_person_name(candidate.display_name), normalise_alias(candidate.display_name)}
    for value in (candidate.email, candidate.upn, *candidate.aliases):
        if not value:
            continue
        normalised_email = _normalise_email(value)
        if normalised_email:
            keys.add(f"email:{normalised_email}")
            keys.add(normalise_alias(normalised_email.split("@", 1)[0]))
        keys.add(normalise_alias(value))
    return {key for key in keys if key}


def _person_aliases(candidate: _PersonCandidate, existing_aliases: Any) -> set[str]:
    aliases = {str(alias) for alias in _as_list(existing_aliases) if alias}
    aliases.add(candidate.display_name)
    for value in (candidate.email, candidate.upn, *candidate.aliases):
        if value:
            aliases.add(value)
            email = _normalise_email(value)
            if email:
                aliases.add(email.split("@", 1)[0])
    return aliases


def _normalise_email(value: Any) -> str | None:
    text = _clean_text(value)
    if not text or not _EMAIL_RE.match(text):
        return None
    return text.casefold()


def _display_from_email(email: str | None) -> str | None:
    if not email:
        return None
    local = email.split("@", 1)[0]
    pieces = [piece for piece in re.split(r"[._\-]+", local) if piece]
    return " ".join(piece.capitalize() for piece in pieces) or local


# ---------------------------------------------------------------- utilities


def _source_kind(source: Object) -> SourceKind | None:
    try:
        return SourceKind(source.data.get("source"))
    except ValueError:
        return None


def _first(record: dict[str, Any], *keys: str) -> Any:
    lowered = {key.lower(): key for key in record}
    for key in keys:
        actual = lowered.get(key.lower())
        if actual is not None and record.get(actual) not in (None, ""):
            return record[actual]
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).translate(str.maketrans("", "", "$,"))
    try:
        return float(text)
    except ValueError:
        return None
