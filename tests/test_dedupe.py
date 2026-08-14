"""Tests for cross-source deduplication.

The scenario that matters most: the same deliverable tracked as a GitHub issue
and an ADO work item, worded differently, linked only by an ``AB#`` reference.
"""

from __future__ import annotations

import pytest
from activegraph import Graph

from task_graph.connectors.base import SourceItem
from task_graph.embeddings import HashingEmbedder
from task_graph.learning.weights import Weights
from task_graph.ontology.types import RelationType, SourceKind, TaskState
from task_graph.pipeline.dedupe import (
    Decision,
    Deduper,
    referenced_identifiers,
    squash,
    title_similarity,
)
from task_graph.pipeline.ingest import Ingestor
from task_graph.store import SqliteGraphStore


@pytest.fixture
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "graph.db")
    yield s
    s.close()


@pytest.fixture
def graph(store):
    return Graph(graph_store=store)


@pytest.fixture
def embedder():
    return HashingEmbedder()


@pytest.fixture
def env(graph, store, embedder):
    ingestor = Ingestor(graph, store, embedder=embedder)
    deduper = Deduper(graph, store, weights=Weights(), search=ingestor.search, embedder=embedder)
    return ingestor, deduper


def gh(uri="github:issue:o/r#7", title="Fix billing pipeline", body="") -> SourceItem:
    return SourceItem(
        source=SourceKind.GITHUB,
        source_uri=uri,
        title=title,
        body=body,
        source_state="open",
        owner="tykendrick",
    )


def ado(uri="ado:workitem:12345", title="Billing pipeline remediation", body="") -> SourceItem:
    return SourceItem(
        source=SourceKind.ADO,
        source_uri=uri,
        title=title,
        body=body,
        source_state="Active",
        owner="tykendrick",
    )


def tasks_by_title(store) -> dict[str, object]:
    return {t.data["title"]: t for t in store.find_objects("task")}


# --------------------------------------------------------------- primitives


def test_title_similarity_is_symmetric_and_bounded():
    a, b = "Fix the billing pipeline", "Billing pipeline fixes"
    assert title_similarity(a, b) == title_similarity(b, a)
    assert 0.0 <= title_similarity(a, b) <= 1.0
    assert title_similarity(a, a) == pytest.approx(1.0)


def test_title_similarity_ignores_boilerplate():
    """Generic verbs shouldn't make unrelated work look alike."""
    generic = title_similarity("Fix the issue", "Update the task")
    real = title_similarity("Fix billing pipeline", "Billing pipeline repair")
    assert real > generic


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tracked as AB#12345", {"12345"}),
        ("see AB[9876]", {"9876"}),
        ("https://dev.azure.com/org/_workitems/edit/4242", {"4242"}),
        ("fixes #77", {"77"}),
        ("https://github.com/o/r/pull/31", {"31"}),
        ("no identifiers here", set()),
    ],
)
def test_referenced_identifiers(text, expected):
    from activegraph import Object

    obj = Object(id="s", type="source_item", data={"body": text}, version=1, provenance={})
    assert referenced_identifiers(obj) >= expected


def test_squash_is_monotonic_and_bounded():
    values = [squash(x) for x in (-2, -1, 0, 0.8, 1.5, 2.0, 4.0)]
    assert values == sorted(values)
    assert all(0.0 <= v <= 1.0 for v in values)
    assert squash(0.8) == pytest.approx(0.5)


# ------------------------------------------------------- decisive evidence


def test_cross_reference_auto_links_across_systems(env, store):
    """The headline case: GitHub issue naming AB#12345 and ADO work item 12345."""
    ingestor, deduper = env
    ingestor.ingest(
        [
            gh(title="Nightly invoice job fails", body="Tracked in ADO as AB#12345"),
            ado(title="Remediate nightly invoicing failure"),
        ]
    )

    report = deduper.run()
    assert len(report.linked) == 1, report.summary()
    pair = report.linked[0]
    assert pair.features["cross_reference"] == 1.0
    assert pair.decision is Decision.AUTO_LINK


def test_auto_link_creates_duplicate_of_and_merges_evidence(env, store):
    ingestor, deduper = env
    ingestor.ingest(
        [
            gh(title="Nightly invoice job fails", body="Tracked in ADO as AB#12345"),
            ado(title="Remediate nightly invoicing failure"),
        ]
    )
    deduper.run()

    duplicates = store.find_relations(type=RelationType.DUPLICATE_OF.value)
    assert len(duplicates) == 1

    canonical = store.get_object(duplicates[0].target)
    absorbed = store.get_object(duplicates[0].source)
    assert set(canonical.data["source_uris"]) == {"github:issue:o/r#7", "ado:workitem:12345"}
    assert absorbed.data["state"] == TaskState.DROPPED.value
    assert absorbed.data["merged_into"] == canonical.id
    # Both source items now point at the surviving task.
    assert len(ingestor.sources_for_task(canonical.id)) == 2


# ----------------------------------------------------------- weaker signals


def test_similar_titles_are_proposed_not_linked(env):
    """Similarity alone is never enough to merge silently."""
    ingestor, deduper = env
    ingestor.ingest(
        [
            gh(title="Fix the billing pipeline timeout"),
            ado(title="Billing pipeline timeout fix"),
        ]
    )

    report = deduper.run()
    assert report.linked == []
    assert len(report.proposed) == 1
    assert report.proposed[0].decision is Decision.PROPOSE
    assert len(deduper.pending_merges()) == 1


def test_unrelated_work_is_ignored(env):
    ingestor, deduper = env
    ingestor.ingest(
        [
            gh(title="Fix the billing pipeline timeout"),
            ado(title="Order new laptops for the team offsite"),
        ]
    )
    report = deduper.run()
    assert report.linked == []
    assert report.proposed == []


def test_same_source_pairs_are_penalised(env, store):
    """Two GitHub issues are usually genuinely different work."""
    ingestor, deduper = env
    ingestor.ingest(
        [
            gh("github:issue:o/r#1", title="Fix the billing pipeline timeout"),
            gh("github:issue:o/r#2", title="Fix the billing pipeline retry"),
        ]
    )
    tasks = store.find_objects("task")
    pair = deduper.score_pair(tasks[0], tasks[1])
    assert pair.features["same_source_penalty"] == 1.0

    cross_source = Deduper(deduper.graph, store, weights=Weights())
    tweaked = cross_source.features(tasks[0], tasks[1])
    tweaked["same_source_penalty"] = 0.0
    assert squash(cross_source.weights.score("dedupe", tweaked)) > pair.score


# ------------------------------------------------------------ merge lifecycle


def test_approving_a_merge_materialises_it(env, store):
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    deduper.run()
    patch = deduper.pending_merges()[0]

    canonical_id = deduper.apply_merge(patch.id, approved_by="tykendrick")

    assert store.get_patch(patch.id).status == "applied"
    assert len(store.find_relations(type=RelationType.DUPLICATE_OF.value)) == 1
    canonical = store.get_object(canonical_id)
    assert len(canonical.data["source_uris"]) == 2
    assert store.get_object(patch.target).data["state"] == TaskState.DROPPED.value


def test_rejecting_a_merge_retains_the_reason(env, store):
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    deduper.run()
    patch = deduper.pending_merges()[0]

    deduper.reject_merge(patch.id, "different releases", actor="tykendrick")

    stored = store.get_patch(patch.id)
    assert stored.status == "rejected"
    assert stored.rejection_reason == "different releases"
    assert store.find_relations(type=RelationType.DUPLICATE_OF.value) == []
    # Both tasks stay open — nothing was hidden.
    assert all(t.data["state"] != TaskState.DROPPED.value for t in store.find_objects("task"))


def test_apply_merge_rejects_a_non_merge_patch(env, store, graph):
    _, deduper = env
    task = graph.add_object("task", {"title": "solo", "state": "active"})
    patch = graph.propose_patch(task.id, "update", {"title": "renamed"}, proposed_by="test")
    with pytest.raises(ValueError, match="not a merge proposal"):
        deduper.apply_merge(patch.id)


def test_apply_merge_rejects_unknown_patch(env):
    _, deduper = env
    with pytest.raises(KeyError):
        deduper.apply_merge("no-such-patch")


# ---------------------------------------------------------------- stability


def test_repeated_runs_do_not_re_propose(env):
    """An approval queue that repeats itself every sync is unusable."""
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    first = deduper.run()
    second = deduper.run()

    assert len(first.proposed) == 1
    assert second.proposed == []
    assert second.skipped_existing >= 1
    assert len(deduper.pending_merges()) == 1


def test_rejected_pairs_are_not_re_proposed(env, store):
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    deduper.run()
    deduper.reject_merge(deduper.pending_merges()[0].id, "not the same", actor="user")

    assert deduper.run().proposed == []


def test_canonical_choice_is_deterministic(env, store):
    ingestor, deduper = env
    ingestor.ingest([gh(title="Alpha work"), ado(title="Alpha work")])
    a, b = store.find_objects("task")
    assert deduper.canonical_order(a, b) == deduper.canonical_order(b, a)


def test_canonical_prefers_the_task_backed_by_more_sources(env, store, graph):
    _, deduper = env
    rich = graph.add_object("task", {"title": "x", "source_uris": ["a", "b"]})
    thin = graph.add_object("task", {"title": "x", "source_uris": ["c"]})
    canonical, absorbed = deduper.canonical_order(thin, rich)
    assert canonical.id == rich.id
    assert absorbed.id == thin.id


def test_closed_tasks_are_not_considered(env, store, graph):
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    for task in store.find_objects("task"):
        graph.patch_object(task.id, {"state": TaskState.DONE.value})

    report = deduper.run()
    assert report.considered == 0


def test_scoring_is_deterministic(env, store):
    ingestor, deduper = env
    ingestor.ingest([gh(title="Fix the billing pipeline"), ado(title="Billing pipeline fix")])
    a, b = store.find_objects("task")
    assert deduper.score_pair(a, b).score == deduper.score_pair(a, b).score


# ----------------------------------------------------------------- steering


def test_weights_change_the_decision(env, store):
    """Proves the learning loop can actually steer dedupe behaviour."""
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    a, b = store.find_objects("task")
    assert deduper.score_pair(a, b).decision is Decision.PROPOSE

    deduper.weights.dedupe["title_similarity"] = 0.0
    deduper.weights.dedupe["embedding_similarity"] = 0.0
    assert deduper.score_pair(a, b).decision is Decision.IGNORE


def test_raising_thresholds_suppresses_proposals(env, store):
    ingestor, deduper = env
    ingestor.ingest(
        [gh(title="Fix the billing pipeline timeout"), ado(title="Billing pipeline timeout fix")]
    )
    deduper.weights.propose_threshold = 0.99
    assert deduper.run().proposed == []


def test_pair_explanation_is_readable(env, store):
    ingestor, deduper = env
    ingestor.ingest(
        [
            gh(title="Nightly invoice job fails", body="Tracked in ADO as AB#12345"),
            ado(title="Remediate nightly invoicing failure"),
        ]
    )
    pair = deduper.run().linked[0]
    assert "explicitly references" in pair.explain()
