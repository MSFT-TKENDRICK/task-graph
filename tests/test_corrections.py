"""Tests for learning from user corrections.

The point of this layer is that disagreeing with the system *changes* it. Most
of these tests therefore assert on a weight actually moving, not merely on a
correction being filed.
"""

from __future__ import annotations

import pytest
from activegraph import Graph

from task_graph.connectors.base import SourceItem
from task_graph.embeddings import HashingEmbedder
from task_graph.learning.corrections import Corrector, parse_evidence
from task_graph.learning.weights import Weights
from task_graph.ontology.types import (
    CorrectionKind,
    ObjectType,
    RelationType,
    TaskState,
)
from task_graph.pipeline.dedupe import Decision, Deduper
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
def weights():
    return Weights()


@pytest.fixture
def corrector(graph, store, weights):
    return Corrector(graph, store, weights)


@pytest.fixture
def merge_env(graph, store, weights):
    """A graph with one pending merge proposal between two similar tasks."""
    embedder = HashingEmbedder()
    ingestor = Ingestor(graph, store, embedder=embedder)
    ingestor.ingest(
        [
            SourceItem(
                source="github",
                source_uri="github:issue:o/r#1",
                title="Fix the billing pipeline timeout",
                source_state="open",
                owner="me",
            ),
            SourceItem(
                source="ado",
                source_uri="ado:workitem:99",
                title="Billing pipeline timeout fix",
                source_state="Active",
                owner="me",
            ),
        ]
    )
    deduper = Deduper(graph, store, weights, ingestor.search, embedder)
    report = deduper.run()
    assert report.proposed, "fixture expects a proposed merge"
    return deduper, Corrector(graph, store, weights), deduper.pending_merges()[0]


# ------------------------------------------------------------ evidence parse


def test_parse_evidence_reads_feature_vectors():
    assert parse_evidence(["title_similarity=0.812", "same_owner=1.0"]) == {
        "title_similarity": 0.812,
        "same_owner": 1.0,
    }


def test_parse_evidence_ignores_junk():
    assert parse_evidence(["not a feature", "", "x=", "a=1.5"]) == {"a": 1.5}
    assert parse_evidence([]) == {}


# ---------------------------------------------------------------- recording


def test_record_creates_a_correction_object(corrector, store, graph):
    task = graph.add_object("task", {"title": "x", "state": "active"})
    correction = corrector.record(
        CorrectionKind.PRIORITY, [task.id], rationale="too low", actor="tykendrick"
    )

    stored = store.get_object(correction.id)
    assert stored.type == ObjectType.CORRECTION.value
    assert stored.data["kind"] == CorrectionKind.PRIORITY.value
    assert stored.data["rationale"] == "too low"
    assert stored.data["applied"] is False


def test_record_links_to_its_subject(corrector, store, graph):
    task = graph.add_object("task", {"title": "x"})
    correction = corrector.record(CorrectionKind.NOT_A_TASK, [task.id])
    links = store.find_relations(source=correction.id, type=RelationType.CORRECTS.value)
    assert [r.target for r in links] == [task.id]


def test_record_tolerates_unknown_subjects(corrector, store):
    correction = corrector.record(CorrectionKind.DEDUPE, ["ghost-id"])
    assert store.find_relations(source=correction.id) == []


def test_pending_lists_only_unapplied(corrector, graph):
    task = graph.add_object("task", {"title": "x"})
    corrector.record(CorrectionKind.PRIORITY, [task.id])
    assert len(corrector.pending()) == 1
    corrector.learn()
    assert corrector.pending() == []


# ------------------------------------------------------------ dedupe learning


def test_rejecting_a_merge_lowers_the_responsible_weights(merge_env):
    """The core learning loop: disagreeing makes the system less eager."""
    _, corrector, patch = merge_env
    before = dict(corrector.weights.dedupe)

    corrector.reject_merge(patch, "different releases", actor="me")
    report = corrector.learn()

    assert report.corrections_applied == 1
    assert corrector.weights.dedupe["title_similarity"] < before["title_similarity"]
    assert corrector.weights.dedupe["embedding_similarity"] < before["embedding_similarity"]


def test_confirming_a_merge_raises_the_responsible_weights(merge_env):
    _, corrector, patch = merge_env
    before = dict(corrector.weights.dedupe)

    corrector.confirm_merge(patch, actor="me")
    corrector.learn()

    assert corrector.weights.dedupe["title_similarity"] > before["title_similarity"]


def test_blame_is_proportional_to_how_loudly_a_feature_argued(corrector, graph):
    """A feature that barely contributed should barely move."""
    task_a = graph.add_object("task", {"title": "a"})
    task_b = graph.add_object("task", {"title": "b"})
    corrector.record(
        CorrectionKind.DEDUPE,
        [task_a.id, task_b.id],
        before={"features": {"title_similarity": 1.0, "temporal_proximity": 0.1}},
        after={"same_work": False},
    )
    before = dict(corrector.weights.dedupe)
    corrector.learn()

    loud = before["title_similarity"] - corrector.weights.dedupe["title_similarity"]
    quiet = before["temporal_proximity"] - corrector.weights.dedupe["temporal_proximity"]
    assert loud > quiet > 0


def test_learning_actually_changes_a_future_decision(merge_env, store):
    """End to end: reject enough times and the system stops proposing it."""
    deduper, corrector, patch = merge_env
    a = store.get_object(patch.target)
    b = store.get_object(patch.value["merge_into"])
    assert deduper.score_pair(a, b).decision is Decision.PROPOSE

    for _ in range(6):
        corrector.record(
            CorrectionKind.DEDUPE,
            [a.id, b.id],
            before={"features": deduper.features(a, b)},
            after={"same_work": False},
        )
        corrector.learn()

    assert deduper.score_pair(a, b).decision is Decision.IGNORE


def test_dedupe_correction_without_features_is_skipped(corrector, graph):
    task = graph.add_object("task", {"title": "x"})
    corrector.record(CorrectionKind.DEDUPE, [task.id], after={"same_work": False})
    report = corrector.learn()
    assert report.corrections_applied == 0
    assert report.skipped == 1


# ---------------------------------------------------------- priority learning


def test_reprioritizing_moves_the_dominant_factor(corrector, graph):
    task = graph.add_object(
        "task",
        {
            "title": "x",
            "priority": 0.2,
            "priority_factors": {"urgency": 0.9, "staleness": 0.1},
        },
    )
    before = dict(corrector.weights.priority)

    corrector.reprioritize_task(task, direction=1.0, rationale="this is urgent")
    corrector.learn()

    assert corrector.weights.priority["urgency"] > before["urgency"]
    raised = corrector.weights.priority["urgency"] - before["urgency"]
    barely = corrector.weights.priority["staleness"] - before["staleness"]
    assert raised > barely > 0


def test_lowering_a_task_lowers_its_factors(corrector, graph):
    task = graph.add_object(
        "task", {"title": "x", "priority_factors": {"source_importance": 1.0}}
    )
    before = corrector.weights.priority["source_importance"]
    corrector.reprioritize_task(task, direction=-1.0)
    corrector.learn()
    assert corrector.weights.priority["source_importance"] < before


def test_zero_direction_is_not_learned_from(corrector, graph):
    task = graph.add_object("task", {"title": "x", "priority_factors": {"urgency": 1.0}})
    corrector.reprioritize_task(task, direction=0.0)
    assert corrector.learn().corrections_applied == 0


def test_legacy_method_name_still_works(corrector, graph):
    task = graph.add_object("task", {"title": "x", "priority_factors": {"urgency": 1.0}})
    assert corrector.repriorit_task(task, direction=1.0) is not None


# --------------------------------------------------------------- not-a-task


def test_not_a_task_drops_it_immediately(corrector, store, graph):
    """A correction the user makes should take effect now, not next sync."""
    task = graph.add_object("task", {"title": "newsletter", "state": "active"})
    corrector.not_a_task(task, rationale="marketing email")
    assert store.get_object(task.id).data["state"] == TaskState.DROPPED.value


# ------------------------------------------------------------- idempotency


def test_learning_twice_does_not_double_count(merge_env):
    _, corrector, patch = merge_env
    corrector.reject_merge(patch, "no", actor="me")
    corrector.learn()
    after_first = dict(corrector.weights.dedupe)

    second = corrector.learn()
    assert second.corrections_applied == 0
    assert corrector.weights.dedupe == after_first


def test_corrections_applied_counter_increments(merge_env):
    _, corrector, patch = merge_env
    corrector.reject_merge(patch, "no", actor="me")
    corrector.learn()
    assert corrector.weights.corrections_applied == 1


def test_learning_report_summarises_movement(merge_env):
    _, corrector, patch = merge_env
    corrector.reject_merge(patch, "no", actor="me")
    summary = corrector.learn().summary()
    assert "learned from 1" in summary
    assert "title_similarity" in summary


def test_empty_learn_reports_nothing_to_do(corrector):
    assert "no new corrections" in corrector.learn().summary()


# ---------------------------------------------------------------- simulation


def test_simulate_reports_what_would_change(merge_env):
    """Answers "would learning from this have helped?" against real history."""
    _, corrector, _ = merge_env
    candidate = Weights()
    candidate.propose_threshold = 0.99

    changes = corrector.simulate(candidate)
    assert changes
    assert changes[0].was == Decision.PROPOSE.value
    assert changes[0].now == Decision.IGNORE.value


def test_simulate_is_side_effect_free(merge_env):
    _, corrector, _ = merge_env
    before = dict(corrector.weights.dedupe)
    candidate = Weights()
    candidate.dedupe["title_similarity"] = 3.0
    corrector.simulate(candidate)
    assert corrector.weights.dedupe == before


def test_simulate_with_identical_weights_reports_no_change(merge_env):
    _, corrector, _ = merge_env
    assert corrector.simulate(corrector.weights) == []


# ------------------------------------------------------------ weight storage


def test_weights_round_trip(tmp_path):
    path = tmp_path / "weights.json"
    weights = Weights()
    weights.nudge("dedupe", "title_similarity", -1.0)
    weights.corrections_applied = 3
    weights.save(path)

    loaded = Weights.load(path)
    assert loaded.dedupe["title_similarity"] == weights.dedupe["title_similarity"]
    assert loaded.corrections_applied == 3


def test_missing_or_corrupt_weights_fall_back_to_defaults(tmp_path):
    """A damaged weights file must never block a sync."""
    assert Weights.load(tmp_path / "absent.json").dedupe == Weights().dedupe

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert Weights.load(broken).dedupe == Weights().dedupe


def test_nudge_is_clamped():
    weights = Weights()
    for _ in range(500):
        weights.nudge("dedupe", "title_similarity", 1.0)
    assert weights.dedupe["title_similarity"] <= 3.0


def test_nudge_rejects_unknown_weights():
    with pytest.raises(KeyError):
        Weights().nudge("dedupe", "not_a_feature", 1.0)
    with pytest.raises(KeyError):
        Weights().nudge("nonexistent_section", "title_similarity", 1.0)
