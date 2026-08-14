from __future__ import annotations

import math
from collections.abc import Iterator

import httpx
import pytest

import task_graph.embeddings as embeddings_module
from task_graph.config import Settings
from task_graph.embeddings import (
    EmbeddingError,
    FoundryLocalEmbedder,
    HashingEmbedder,
    get_embedder,
)


def dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class MockClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, object], float | None]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float | None = None,
    ) -> httpx.Response:
        self.requests.append((url, json, timeout))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def clear_embedder_cache() -> Iterator[None]:
    embeddings_module._PROVIDERS.clear()
    yield
    embeddings_module._PROVIDERS.clear()


def test_hashing_embedder_is_deterministic_across_instances() -> None:
    text = "Update the billing API docs"

    assert HashingEmbedder().embed_one(text) == HashingEmbedder().embed_one(text)


def test_hashing_embedder_vectors_are_unit_norm() -> None:
    vector = HashingEmbedder().embed_one("Update the billing API docs")

    assert math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)


def test_hashing_embedder_related_strings_score_higher_than_unrelated() -> None:
    embedder = HashingEmbedder()
    anchor = embedder.embed_one("Update the billing API docs")
    related = embedder.embed_one("Update billing API documentation")
    unrelated = embedder.embed_one("Order more coffee for the kitchen")

    assert dot(anchor, related) > dot(anchor, unrelated)


def test_hashing_embedder_batch_preserves_order() -> None:
    embedder = HashingEmbedder()
    texts = ["alpha", "beta", "gamma"]

    assert embedder.embed(texts) == [embedder.embed_one(text) for text in texts]


def test_foundry_local_request_shape_ordering_and_dimension() -> None:
    client = MockClient(
        [
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [3, 4, 5]},
                        {"index": 0, "embedding": [0, 1, 2]},
                    ]
                },
            )
        ]
    )
    embedder = FoundryLocalEmbedder(
        endpoint="http://foundry.test/v1/",
        model="test-model",
        timeout=7.0,
        client=client,  # type: ignore[arg-type]
    )

    vectors = embedder.embed(["first", "second"])

    assert client.requests == [
        (
            "http://foundry.test/v1/embeddings",
            {"model": "test-model", "input": ["first", "second"]},
            7.0,
        )
    ]
    assert vectors == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]]
    assert embedder.dimension == 3


def test_foundry_local_non_200_raises_embedding_error() -> None:
    client = MockClient([httpx.Response(500, json={"error": "boom"})])
    embedder = FoundryLocalEmbedder(client=client)  # type: ignore[arg-type]

    with pytest.raises(EmbeddingError, match="HTTP 500"):
        embedder.embed_one("hello")


def test_get_embedder_forces_hashing() -> None:
    settings = Settings(embedding_provider="hashing")

    assert isinstance(get_embedder(settings), HashingEmbedder)


def test_get_embedder_forces_foundry_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FoundryLocalEmbedder, "is_available", lambda **_: True)
    settings = Settings(embedding_provider="foundry")

    assert isinstance(get_embedder(settings), FoundryLocalEmbedder)


def test_get_embedder_forced_foundry_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FoundryLocalEmbedder, "is_available", lambda **_: False)
    settings = Settings(embedding_provider="foundry")

    with pytest.raises(EmbeddingError):
        get_embedder(settings)


def test_get_embedder_auto_uses_foundry_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FoundryLocalEmbedder, "is_available", lambda **_: True)
    settings = Settings(embedding_provider="auto")

    assert isinstance(get_embedder(settings), FoundryLocalEmbedder)


def test_get_embedder_auto_falls_back_to_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FoundryLocalEmbedder, "is_available", lambda **_: False)
    settings = Settings(embedding_provider="auto")

    assert isinstance(get_embedder(settings), HashingEmbedder)


def test_get_embedder_caches_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def is_available(**_: object) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(FoundryLocalEmbedder, "is_available", is_available)
    settings = Settings(embedding_provider="auto")

    assert get_embedder(settings) is get_embedder(settings)
    assert calls == 1
