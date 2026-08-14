"""Tests for the shared service facade.

These are the closest thing to end-to-end coverage: they exercise the same
entry points the CLI and MCP server call, against a real on-disk state
directory.
"""

from __future__ import annotations

import pytest

from task_graph.app import TaskGraphApp
from task_graph.config import Settings
from task_graph.connectors.base import ConnectorStatus, SourceItem
from task_graph.embeddings import HashingEmbedder
from task_graph.ontology.types import SourceKind, TaskState


@pytest.fixture
def settings(tmp_path):
    return Settings(home=tmp_path / "state", embedding_provider="hashing")


@pytest.fixture
def app(settings):
    a = TaskGraphApp(settings, embedder=HashingEmbedder())
    yield a
    a.close()


def items() -> list[SourceItem]:
    return [
        SourceItem(
            source=SourceKind.GITHUB,
            source_uri="github:issue:o/r#7",
            title="Nightly invoice job fails",
            body="Tracked in ADO as AB#12345",
            source_state="open",
            owner="me",
        ),
        SourceItem(
            source=SourceKind.ADO,
            source_uri="ado:workitem:12345",
            title="Remediate nightly invoicing failure",
            source_state="Active",
            owner="me",
        ),
        SourceItem(
            source=SourceKind.MAIL,
            source_uri="mail:<q3@corp>",
            title="Can you review the Q3 pricing deck?",
            source_state="flagged",
            owner="me",
        ),
    ]


def seed(app: TaskGraphApp) -> None:
    app.ingestor.ingest(items())
    app.deduper.run()
    app.rank()


# ------------------------------------------------------------------ lifecycle


def test_opening_creates_the_state_directory(settings):
    app = TaskGraphApp(settings, embedder=HashingEmbedder())
    assert settings.home.exists()
    assert settings.graph_db.exists()
    app.close()


def test_run_id_is_stable_across_reopen(settings):
    """Events are scoped to a run; a new id each start would fragment history."""
    first = TaskGraphApp(settings, embedder=HashingEmbedder())
    run_id = first.graph.run_id
    first.ingestor.ingest(items()[:1])
    first.close()

    second = TaskGraphApp(settings, embedder=HashingEmbedder())
    assert second.graph.run_id == run_id
    assert second.events.count() > 0
    second.close()


def test_data_survives_reopen(settings):
    first = TaskGraphApp(settings, embedder=HashingEmbedder())
    seed(first)
    open_before = first.status()["open_tasks"]
    first.close()

    second = TaskGraphApp(settings, embedder=HashingEmbedder())
    assert second.status()["open_tasks"] == open_before
    second.close()


def test_weights_are_saved_on_close(settings):
    app = TaskGraphApp(settings, embedder=HashingEmbedder())
    app.weights.nudge("dedupe", "title_similarity", -1.0)
    tuned = app.weights.dedupe["title_similarity"]
    app.close()

    reopened = TaskGraphApp(settings, embedder=HashingEmbedder())
    assert reopened.weights.dedupe["title_similarity"] == pytest.approx(tuned)
    reopened.close()


def test_works_as_a_context_manager(settings):
    with TaskGraphApp(settings, embedder=HashingEmbedder()) as app:
        seed(app)
        assert app.status()["open_tasks"] > 0


# -------------------------------------------------------------------- syncing


def test_sync_pulls_from_registered_connectors(app, monkeypatch):
    class FakeConnector:
        kind = SourceKind.GITHUB
        name = "fake"

        def is_available(self):
            return ConnectorStatus(available=True, detail="ok")

        def fetch(self, since=None):
            return items()

    monkeypatch.setattr("task_graph.app.get_connector", lambda kind: FakeConnector())
    report = app.sync(sources=[SourceKind.GITHUB])

    assert len(report.ingest.created) == 3
    assert report.ranked > 0
    assert "new" in report.summary()


def test_sync_reports_unavailable_connectors_without_failing(app, monkeypatch):
    class Unavailable:
        def is_available(self):
            return ConnectorStatus(available=False, detail="not logged in",
                                   remediation="gh auth login")

        def fetch(self, since=None):
            raise AssertionError("must not fetch when unavailable")

    monkeypatch.setattr("task_graph.app.get_connector", lambda kind: Unavailable())
    report = app.sync(sources=[SourceKind.GITHUB])

    assert report.errors and "not logged in" in report.errors[0]
    assert report.ingest.created == []


def test_sync_survives_a_throwing_connector(app, monkeypatch):
    class Exploding:
        def is_available(self):
            return ConnectorStatus(available=True, detail="ok")

        def fetch(self, since=None):
            raise RuntimeError("upstream 500")

    monkeypatch.setattr("task_graph.app.get_connector", lambda kind: Exploding())
    report = app.sync(sources=[SourceKind.ADO])
    assert any("upstream 500" in e for e in report.errors)


def test_sync_does_not_propose_actions_by_default(app, monkeypatch):
    """Proposals on every scheduled sync would flood the approval queue."""
    class FakeConnector:
        def is_available(self):
            return ConnectorStatus(available=True, detail="ok")

        def fetch(self, since=None):
            return items()

    monkeypatch.setattr("task_graph.app.get_connector", lambda kind: FakeConnector())
    assert app.sync(sources=[SourceKind.GITHUB]).proposed_actions == 0


# ------------------------------------------------------------------- querying


def test_dedupe_unifies_the_cross_referenced_pair(app):
    seed(app)
    # Three source items, but the GitHub/ADO pair collapses into one task.
    assert app.status()["objects"] == 6
    assert app.status()["open_tasks"] == 2


def test_triage_returns_ranked_open_work(app):
    seed(app)
    ranked = app.triage(limit=10)
    assert ranked
    scores = [b.score for _, b in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(t.data["state"] != TaskState.DONE.value for t, _ in ranked)


def test_triage_respects_the_limit(app):
    seed(app)
    assert len(app.triage(limit=1)) == 1


def test_search_finds_seeded_work(app):
    seed(app)
    hits = app.search_tasks("invoice billing")
    assert hits
    assert {"id", "title", "score", "matched"} <= set(hits[0])


def test_get_task_includes_backing_sources(app):
    seed(app)
    task_id = app.triage()[0][0].id
    detail = app.get_task(task_id)
    assert detail["sources"]
    assert {"source", "source_uri", "url"} <= set(detail["sources"][0])


def test_get_task_on_unknown_id_returns_none(app):
    assert app.get_task("no-such-task") is None


def test_task_graph_returns_the_neighbourhood(app):
    seed(app)
    task_id = app.triage()[0][0].id
    neighbourhood = app.task_graph(task_id, depth=2)
    assert neighbourhood["objects"]
    assert neighbourhood["relations"]


def test_explain_priority_is_prose_plus_factors(app):
    seed(app)
    breakdown = app.explain_priority(app.triage()[0][0].id)
    assert breakdown.factors
    assert len(breakdown.explanation) > 20


# ------------------------------------------------------------------- merges


def test_merge_decisions_feed_the_learner(app):
    """Approving or rejecting a merge must also teach the system."""
    app.ingestor.ingest(
        [
            SourceItem(source=SourceKind.GITHUB, source_uri="github:issue:o/r#1",
                       title="Fix the billing pipeline timeout", source_state="open"),
            SourceItem(source=SourceKind.ADO, source_uri="ado:workitem:99",
                       title="Billing pipeline timeout fix", source_state="Active"),
        ]
    )
    app.deduper.run()
    pending = app.pending_merges()
    assert len(pending) == 1
    assert {"patch_id", "canonical", "absorbed", "score", "rationale"} <= set(pending[0])

    before = app.weights.dedupe["title_similarity"]
    app.reject_merge(pending[0]["patch_id"], "different releases")
    app.learn()
    assert app.weights.dedupe["title_similarity"] < before


def test_approving_a_merge_unifies_the_tasks(app):
    app.ingestor.ingest(
        [
            SourceItem(source=SourceKind.GITHUB, source_uri="github:issue:o/r#1",
                       title="Fix the billing pipeline timeout", source_state="open"),
            SourceItem(source=SourceKind.ADO, source_uri="ado:workitem:99",
                       title="Billing pipeline timeout fix", source_state="Active"),
        ]
    )
    app.deduper.run()
    patch_id = app.pending_merges()[0]["patch_id"]

    canonical_id = app.approve_merge(patch_id)

    canonical = app.store.get_object(canonical_id)
    assert len(canonical.data["source_uris"]) == 2
    assert app.pending_merges() == []


# ------------------------------------------------------------------ rebuild


def test_rebuild_reconstructs_the_projection(app, settings):
    seed(app)
    before = app.status()

    result = app.rebuild()

    assert result["events_replayed"] > 0
    after = app.status()
    assert after["objects"] == before["objects"]
    assert after["open_tasks"] == before["open_tasks"]


def test_rebuild_restores_embeddings(app):
    """Embeddings are derived, not logged; losing them would silently
    degrade semantic search until the next sync."""
    seed(app)
    before = app.status()["embeddings"]
    assert before > 0

    result = app.rebuild()

    assert result["re_embedded"] == before
    assert app.status()["embeddings"] == before


def test_search_still_works_after_rebuild(app):
    seed(app)
    app.rebuild()
    assert app.search_tasks("invoice billing")


def test_rebuild_keeps_a_backup(app, settings):
    seed(app)
    app.rebuild()
    assert settings.graph_db.with_suffix(".db.bak").exists()


def test_ids_do_not_collide_after_reopen(settings):
    """Restarting must not restart the id counters onto existing objects."""
    first = TaskGraphApp(settings, embedder=HashingEmbedder())
    seed(first)
    existing = {o.id for o in first.store.all_objects()}
    first.close()

    second = TaskGraphApp(settings, embedder=HashingEmbedder())
    fresh = second.graph.add_object("task", {"title": "added after restart"})
    assert fresh.id not in existing
    assert second.store.get_object(fresh.id).data["title"] == "added after restart"
    second.close()


def test_ids_do_not_collide_after_rebuild(app):
    seed(app)
    app.rebuild()
    existing = {o.id for o in app.store.all_objects()}

    fresh = app.graph.add_object("task", {"title": "added after rebuild"})

    assert fresh.id not in existing
    # The replayed objects must still be intact alongside the new one.
    assert len(app.store.all_objects()) == len(existing) + 1


# ------------------------------------------------------------------- status


def test_status_reports_the_essentials(app):
    seed(app)
    status = app.status()
    for key in ("home", "run_id", "events", "objects", "open_tasks", "embedder"):
        assert key in status
    assert status["events"] > 0


def test_preflight_returns_actionable_checks(app):
    checks = app.preflight()
    assert checks
    for check in checks:
        assert {"check", "ok", "detail"} <= set(check)
    assert any(c["check"] == "embeddings" for c in checks)


def test_status_on_an_empty_graph_is_safe(app):
    status = app.status()
    assert status["open_tasks"] == 0
    assert app.triage() == []
    assert app.search_tasks("anything") == []
