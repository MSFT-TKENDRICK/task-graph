"""Foundry Local adapter for OpenAI-compatible embedding models."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from task_graph.config import DEFAULT_FOUNDRY_ENDPOINT, DEFAULT_FOUNDRY_MODEL
from task_graph.embeddings.base import EmbeddingError


class FoundryLocalEmbedder:
    """Use local model embeddings when available, falling back elsewhere."""

    def __init__(
        self,
        endpoint: str = DEFAULT_FOUNDRY_ENDPOINT,
        model: str = DEFAULT_FOUNDRY_MODEL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        max_batch_size: int = 64,
    ) -> None:
        if max_batch_size <= 0:
            msg = "max_batch_size must be positive"
            raise ValueError(msg)
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_batch_size = max_batch_size
        self._client = client or httpx.Client(timeout=timeout)
        self._dimension: int | None = None

    @property
    def name(self) -> str:
        return "foundry"

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self.embed_one("")
        if self._dimension is None:
            msg = "Foundry Local did not report an embedding dimension"
            raise EmbeddingError(msg)
        return self._dimension

    @classmethod
    def is_available(
        cls,
        endpoint: str = DEFAULT_FOUNDRY_ENDPOINT,
        model: str = DEFAULT_FOUNDRY_MODEL,
        timeout: float = 1.0,
    ) -> bool:
        try:
            embedder = cls(endpoint=endpoint, model=model, timeout=timeout)
            embedder.embed_one("ping")
        except Exception:
            return False
        return True

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch_size):
            vectors.extend(self._embed_batch(list(texts[start:start + self.max_batch_size])))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed([text])
        return vectors[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            response = self._client.post(
                f"{self.endpoint}/embeddings",
                json={"model": self.model, "input": texts},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            msg = f"Foundry Local embedding request failed: {exc}"
            raise EmbeddingError(msg) from exc

        if response.status_code != 200:
            msg = f"Foundry Local embedding request failed with HTTP {response.status_code}"
            raise EmbeddingError(msg)

        try:
            payload = response.json()
            data = payload["data"]
            ordered = sorted(data, key=lambda item: item["index"])
            vectors = [self._parse_embedding(item) for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            msg = "Foundry Local embedding response had an unexpected shape"
            raise EmbeddingError(msg) from exc

        if len(vectors) != len(texts):
            msg = (
                f"Foundry Local returned {len(vectors)} embeddings for {len(texts)} input texts"
            )
            raise EmbeddingError(msg)

        if vectors:
            dimension = len(vectors[0])
            if any(len(vector) != dimension for vector in vectors):
                msg = "Foundry Local returned embeddings with inconsistent dimensions"
                raise EmbeddingError(msg)
            if self._dimension is None:
                self._dimension = dimension
            elif self._dimension != dimension:
                msg = (
                    f"Foundry Local embedding dimension changed from {self._dimension} "
                    f"to {dimension}"
                )
                raise EmbeddingError(msg)
        return vectors

    @staticmethod
    def _parse_embedding(item: dict[str, Any]) -> list[float]:
        embedding = item["embedding"]
        if not isinstance(embedding, list):
            msg = "embedding must be a list"
            raise TypeError(msg)
        return [float(value) for value in embedding]
