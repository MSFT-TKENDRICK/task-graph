"""GitHub ingestion through the authenticated ``gh`` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from task_graph.connectors.base import (
    ConnectorError,
    ConnectorStatus,
    SourceItem,
    extract_external_refs,
    github_discussion_uri,
    github_issue_uri,
    github_pr_uri,
    parse_datetime,
)
from task_graph.ontology.types import SourceKind


class GitHubConnector:
    @property
    def kind(self) -> SourceKind:
        return SourceKind.GITHUB

    @property
    def name(self) -> str:
        return "GitHub"

    def is_available(self) -> ConnectorStatus:
        if shutil.which("gh") is None:
            return ConnectorStatus(False, "GitHub CLI was not found on PATH.", "Install `gh`.")
        try:
            completed = subprocess.run(
                ["gh", "auth", "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            return ConnectorStatus(False, f"Could not run `gh auth status`: {exc}", "gh auth login")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh is not authenticated").strip()
            return ConnectorStatus(False, detail, "gh auth login")
        return ConnectorStatus(True, "GitHub CLI is authenticated.")

    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]:
        seen: set[str] = set()
        for record in self._fetch_issue_and_pr_records(since):
            item = self._map_issue_or_pr(record)
            if item.source_uri not in seen:
                seen.add(item.source_uri)
                yield item
        for record in self._fetch_discussions():
            item = self._map_discussion(record)
            if item.source_uri not in seen:
                seen.add(item.source_uri)
                yield item
        for record in self._fetch_project_items():
            mapped = self._map_project_item(record)
            if mapped is not None and mapped.source_uri not in seen:
                seen.add(mapped.source_uri)
                yield mapped

    def _fetch_issue_and_pr_records(self, since: datetime | None) -> list[dict[str, Any]]:
        # `type` is not a valid `gh search` JSON field; `isPullRequest` already
        # distinguishes issues from PRs.
        fields = (
            "repository,number,title,body,url,state,author,assignees,labels,"
            "createdAt,updatedAt,isPullRequest"
        )
        queries = [
            ["search", "issues", "--assignee", "@me", "--state", "open", "--json", fields],
            ["search", "issues", "--author", "@me", "--state", "open", "--json", fields],
            ["search", "prs", "--author", "@me", "--state", "open", "--json", fields],
            ["search", "prs", "--review-requested", "@me", "--state", "open", "--json", fields],
        ]
        if since is not None:
            updated = f"updated:>={since.date().isoformat()}"
            queries = [query + [updated] for query in queries]
        records: list[dict[str, Any]] = []
        for query in queries:
            records.extend(_flatten_json(self._run_gh(query + ["--limit", "100"])))
        return records

    def _fetch_discussions(self) -> list[dict[str, Any]]:
        query = """
        query($endCursor: String) {
          viewer {
            contributionsCollection {
              discussionContributions(first: 100, after: $endCursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  discussion {
                    number title body url createdAt updatedAt
                    repository { nameWithOwner }
                    author { login }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data = self._run_gh(["api", "graphql", "--paginate", "-f", f"query={query}"])
        except ConnectorError:
            return []
        records: list[dict[str, Any]] = []
        for page in _flatten_pages(data):
            nodes = (
                page.get("data", {})
                .get("viewer", {})
                .get("contributionsCollection", {})
                .get("discussionContributions", {})
                .get("nodes", [])
            )
            records.extend(node.get("discussion", node) for node in nodes if node)
        return records

    def _fetch_project_items(self) -> list[dict[str, Any]]:
        query = """
        query($endCursor: String) {
          viewer {
            projectItems(first: 100, after: $endCursor) {
              pageInfo { hasNextPage endCursor }
              nodes {
                content {
                  ... on Issue {
                    number title body url state createdAt updatedAt
                    repository { nameWithOwner }
                    author { login }
                    assignees(first: 20) { nodes { login } }
                    labels(first: 20) { nodes { name } }
                  }
                  ... on PullRequest {
                    number title body url state createdAt updatedAt
                    repository { nameWithOwner }
                    author { login }
                    assignees(first: 20) { nodes { login } }
                    labels(first: 20) { nodes { name } }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data = self._run_gh(["api", "graphql", "--paginate", "-f", f"query={query}"])
        except ConnectorError:
            return []
        records: list[dict[str, Any]] = []
        for page in _flatten_pages(data):
            nodes = page.get("data", {}).get("viewer", {}).get("projectItems", {}).get("nodes", [])
            records.extend(node.get("content", node) for node in nodes if node.get("content"))
        return records

    def _run_gh(self, args: list[str]) -> Any:
        try:
            completed = subprocess.run(
                ["gh", *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise ConnectorError("GitHub CLI was not found. Install `gh`.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConnectorError(f"`gh {' '.join(args)}` timed out.") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ConnectorError(f"`gh {' '.join(args)}` failed: {detail}")
        text = completed.stdout.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pages = [json.loads(line) for line in text.splitlines() if line.strip()]
            return pages

    def _map_issue_or_pr(self, record: dict[str, Any]) -> SourceItem:
        repo = _repo_name(record)
        number = record.get("number")
        is_pr = bool(
            record.get("isPullRequest")
            or record.get("pullRequest")
            or str(record.get("type", "")).lower() in {"pullrequest", "pr"}
        )
        source_uri = github_pr_uri(repo, number) if is_pr else github_issue_uri(repo, number)
        return SourceItem(
            source=SourceKind.GITHUB,
            source_uri=source_uri,
            title=str(record.get("title") or ""),
            body=str(record.get("body") or ""),
            url=record.get("url"),
            source_state=record.get("state"),
            owner=_author(record),
            assignees=_nodes(record.get("assignees"), "login"),
            created_at=parse_datetime(record.get("createdAt") or record.get("created_at")),
            updated_at=parse_datetime(record.get("updatedAt") or record.get("updated_at")),
            labels=_nodes(record.get("labels"), "name"),
            external_refs=extract_external_refs(
                f"{record.get('title') or ''}\n{record.get('body') or ''}"
            ),
            raw=record,
        )

    def _map_discussion(self, record: dict[str, Any]) -> SourceItem:
        repo = _repo_name(record)
        body = str(record.get("body") or "")
        return SourceItem(
            source=SourceKind.GITHUB,
            source_uri=github_discussion_uri(repo, record.get("number")),
            title=str(record.get("title") or ""),
            body=body,
            url=record.get("url"),
            owner=_author(record),
            created_at=parse_datetime(record.get("createdAt")),
            updated_at=parse_datetime(record.get("updatedAt")),
            external_refs=extract_external_refs(body),
            raw=record,
        )

    def _map_project_item(self, record: dict[str, Any]) -> SourceItem | None:
        if not record or record.get("number") is None:
            return None
        return self._map_issue_or_pr(record)


def _repo_name(record: dict[str, Any]) -> str:
    repository = record.get("repository") or record.get("repo") or {}
    if isinstance(repository, str):
        return repository
    return str(
        repository.get("nameWithOwner") or repository.get("fullName") or repository.get("name")
    )


def _author(record: dict[str, Any]) -> str | None:
    author = record.get("author")
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return author.get("login") or author.get("name") or author.get("email")
    return None


def _nodes(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict) and "nodes" in value:
        value = value["nodes"]
    if not isinstance(value, list):
        return []
    results: list[str] = []
    for item in value:
        if isinstance(item, str):
            results.append(item)
        elif isinstance(item, dict) and item.get(field):
            results.append(str(item[field]))
    return results


def _flatten_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        flattened: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, list):
                flattened.extend(x for x in item if isinstance(x, dict))
            elif isinstance(item, dict):
                flattened.append(item)
        return flattened
    if isinstance(value, dict):
        return [value]
    return []


def _flatten_pages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []
