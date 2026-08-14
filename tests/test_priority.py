from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from activegraph import Graph

from task_graph.learning.weights import Weights
from task_graph.ontology.types import ObjectType, RelationType, SourceKind, TaskState
from task_graph.pipeline.priority import (
    blocking,
    build_identity,
    explain,
    explicit_ask,
    owner_is_me,
    persist_scores,
    rank_tasks,
    score_task,
    source_importance,
    staleness,
    urgency,
)
from task_graph.store import SqliteGraphStore

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "graph.db")
    yield s
    s.close()


@pytest.fixture
def graph(store):
    return Graph(graph_store=store)


def add_task(graph, title: str = "Task", **overrides):
    data = {
        "title": title,
        "state": TaskState.ACTIVE.value,
        "last_seen_at": NOW.isoformat(),
    }
    data.update(overrides)
    return graph.add_object(ObjectType.TASK.value, data)


def add_source(graph, task, uri: str, **overrides):
    data = {
        "source": SourceKind.GITHUB.value,
        "source_uri": uri,
        "title": "Source",
        "body": "",
        "labels": [],
        "assignees": [],
        "raw": {},
    }
    data.update(overrides)
    source = graph.add_object(ObjectType.SOURCE_ITEM.value, data)
    graph.add_relation(source.id, task.id, RelationType.EVIDENCE_OF.value)
    graph.patch_object(task.id, {"source_uris": [uri]})
    return source


def assert_factor(value: float) -> None:
    assert 0.0 <= value <= 1.0


def test_urgency_boundaries(graph):
    overdue = add_task(graph, due_at=(NOW - timedelta(days=3)).isoformat())
    due_now = add_task(graph, due_at=NOW.isoformat())
    soon = add_task(graph, due_at=(NOW + timedelta(days=1)).isoformat())
    far = add_task(graph, due_at=(NOW + timedelta(days=365)).isoformat())
    undated = add_task(graph, due_at=None)
    missing = add_task(graph)

    assert urgency(overdue, NOW) == 1.0
    assert urgency(due_now, NOW) == 1.0
    assert urgency(soon, NOW) > urgency(far, NOW)
    assert urgency(undated, NOW) == urgency(missing, NOW)
    assert urgency(undated, NOW) > 0.0
    for task in (overdue, due_now, soon, far, undated, missing):
        assert_factor(urgency(task, NOW))


def test_staleness_boundaries(graph):
    fresh = add_task(graph, last_seen_at=NOW.isoformat())
    old = add_task(graph, last_seen_at=(NOW - timedelta(days=90)).isoformat())
    updated_fallback = add_task(
        graph, last_seen_at=None, updated_at=(NOW - timedelta(days=15)).isoformat()
    )
    missing = add_task(graph, last_seen_at=None, updated_at=None)
    future = add_task(graph, last_seen_at=(NOW + timedelta(days=3)).isoformat())

    assert staleness(fresh, NOW) == 0.0
    assert staleness(old, NOW) > staleness(updated_fallback, NOW)
    assert staleness(missing, NOW) > 0.0
    assert staleness(future, NOW) == 0.0
    for task in (fresh, old, updated_fallback, missing, future):
        assert_factor(staleness(task, NOW))


def test_source_importance_rules(graph):
    task = add_task(graph)
    msx = add_source(
        graph,
        task,
        "msx:opportunity:1",
        source=SourceKind.MSX.value,
        raw={"estimatedValue": 50_000},
    )
    mail = add_source(
        graph,
        task,
        "mail:<1>",
        source=SourceKind.MAIL.value,
        raw={"directToMe": True},
    )
    review = add_source(
        graph,
        task,
        "github:pr:o/r#1",
        raw={"reviewRequested": True},
    )
    fyi = add_source(
        graph,
        task,
        "mail:<fyi>",
        source=SourceKind.MAIL.value,
        title="FYI only",
    )

    assert source_importance(task, [msx]) > source_importance(task, [review])
    assert source_importance(task, [review]) > source_importance(task, [mail])
    assert source_importance(task, [mail]) > source_importance(task, [fyi])
    assert source_importance(task, []) > 0.0
    for sources in ([msx], [mail], [review], [fyi], []):
        assert_factor(source_importance(task, sources))


def test_explicit_ask_signals(graph):
    task = add_task(graph)
    review = add_source(graph, task, "github:pr:o/r#2", raw={"reviewRequested": True})
    assigned = add_source(graph, task, "ado:workitem:2", assignees=["ada"])
    direct_mail = add_source(
        graph,
        task,
        "mail:<2>",
        source=SourceKind.MAIL.value,
        body="Could you follow up?",
        raw={"directToMe": True},
    )
    fyi = add_source(graph, task, "mail:<3>", title="FYI")

    assert explicit_ask(task, [review]) == 1.0
    assert explicit_ask(task, [assigned]) > 0.0
    assert explicit_ask(task, [direct_mail]) > 0.0
    assert explicit_ask(task, [fyi]) == 0.0
    for sources in ([review], [assigned], [direct_mail], [fyi], []):
        assert_factor(explicit_ask(task, sources))


def test_owner_is_me_handles_identity_aliases(graph):
    owned = add_task(graph, owner="Ada@example.com")
    other = add_task(graph, owner="grace@example.com")

    identity = build_identity("ada")
    assert owner_is_me(owned, identity) == 1.0
    assert owner_is_me(other, identity) == 0.0
    assert owner_is_me(owned, set()) == 0.0
    assert_factor(owner_is_me(owned, identity))


def test_blocking_chain(graph, store):
    a = add_task(graph, "a")
    b = add_task(graph, "b")
    c = add_task(graph, "c")
    graph.add_relation(a.id, b.id, RelationType.BLOCKS.value)
    graph.add_relation(b.id, c.id, RelationType.BLOCKS.value)

    assert blocking(a, store) == pytest.approx(0.4)
    assert blocking(b, store) == pytest.approx(0.2)
    assert blocking(c, store) == 0.0
    assert_factor(blocking(a, store))


def test_blocking_diamond_counts_unique_open_tasks(graph, store):
    a = add_task(graph, "a")
    b = add_task(graph, "b")
    c = add_task(graph, "c")
    d = add_task(graph, "d")
    graph.add_relation(a.id, b.id, RelationType.BLOCKS.value)
    graph.add_relation(a.id, c.id, RelationType.BLOCKS.value)
    graph.add_relation(b.id, d.id, RelationType.BLOCKS.value)
    graph.add_relation(c.id, d.id, RelationType.BLOCKS.value)

    assert blocking(a, store) == pytest.approx(0.6)


def test_blocking_cycle_terminates(graph, store):
    a = add_task(graph, "a")
    b = add_task(graph, "b")
    c = add_task(graph, "c")
    graph.add_relation(a.id, b.id, RelationType.BLOCKS.value)
    graph.add_relation(b.id, c.id, RelationType.BLOCKS.value)
    graph.add_relation(c.id, a.id, RelationType.BLOCKS.value)

    assert blocking(a, store) == pytest.approx(0.4)


def test_depends_on_counts_inverse_waiters(graph, store):
    dependency = add_task(graph, "dependency")
    dependent = add_task(graph, "dependent")
    graph.add_relation(dependent.id, dependency.id, RelationType.DEPENDS_ON.value)

    assert blocking(dependency, store) == pytest.approx(0.2)


def test_closed_tasks_are_excluded_from_rank_tasks(graph, store):
    open_task = add_task(graph, "open")
    add_task(graph, "done", state=TaskState.DONE.value)
    add_task(graph, "dropped", state=TaskState.DROPPED.value)

    ranked = rank_tasks(store, Weights(), now=NOW)
    assert [task.id for task, _ in ranked] == [open_task.id]


def test_weight_change_changes_ranking_order(graph, store):
    urgent_task = add_task(graph, "urgent", due_at=NOW.isoformat())
    blocker = add_task(graph, "blocker", due_at=None)
    for i in range(5):
        blocked = add_task(graph, f"blocked {i}")
        graph.add_relation(blocker.id, blocked.id, RelationType.BLOCKS.value)

    urgency_weights = Weights()
    urgency_weights.priority.update({"urgency": 2.0, "blocking": 0.0, "staleness": 0.0})
    blocking_weights = Weights()
    blocking_weights.priority.update({"urgency": 0.0, "blocking": 2.0, "staleness": 0.0})

    assert rank_tasks(store, urgency_weights, now=NOW)[0][0].id == urgent_task.id
    assert rank_tasks(store, blocking_weights, now=NOW)[0][0].id == blocker.id


def test_explanation_mentions_dominant_factor_and_reads_as_prose(graph, store):
    task = add_task(graph, "review", due_at=(NOW - timedelta(days=3)).isoformat())
    add_source(graph, task, "github:pr:o/r#3", raw={"reviewRequested": True})

    breakdown = score_task(task, store=store, weights=Weights(), now=NOW)

    assert "overdue 3 days" in breakdown.explanation
    assert breakdown.explanation.startswith("Prioritised because ")
    assert not breakdown.explanation.endswith("{}")


def test_persist_scores_writes_fields_and_is_idempotent(graph, store):
    task = add_task(graph, "persist", due_at=NOW.isoformat())

    first = persist_scores(graph, store, Weights(), now=NOW)
    stored = store.get_object(task.id)
    assert stored.data["priority"] == first[0][1].score
    assert set(stored.data["priority_factors"]) == {
        "urgency",
        "source_importance",
        "blocking",
        "staleness",
        "explicit_ask",
        "owner_is_me",
    }

    second = persist_scores(graph, store, Weights(), now=NOW)
    stored_again = store.get_object(task.id)
    assert stored_again.data["priority"] == stored.data["priority"]
    assert stored_again.data["priority_factors"] == stored.data["priority_factors"]
    assert second[0][1] == first[0][1]


def test_determinism_and_stable_tie_breaks(graph, store):
    b = add_task(graph, "b same", due_at=None)
    a = add_task(graph, "a same", due_at=None)

    first = rank_tasks(store, Weights(), now=NOW)
    second = rank_tasks(store, Weights(), now=NOW)

    assert [(task.id, breakdown) for task, breakdown in first] == [
        (task.id, breakdown) for task, breakdown in second
    ]
    assert [task.id for task, _ in first[:2]] == [a.id, b.id]


def test_empty_task_gets_finite_score(graph, store):
    task = add_task(
        graph,
        "empty",
        due_at=None,
        last_seen_at=None,
        updated_at=None,
        source_uris=[],
        owner=None,
    )

    breakdown = score_task(task, store=store, weights=Weights(), now=NOW)

    assert_factor(breakdown.score)
    assert breakdown.score > 0.0
    assert all(0.0 <= value <= 1.0 for value in breakdown.factors.values())


def test_explain_returns_one_task_breakdown(graph, store):
    task = add_task(graph, "explain", due_at=NOW.isoformat())

    assert explain(task.id, store, Weights(), now=NOW) == score_task(
        task, store=store, weights=Weights(), now=NOW
    )
