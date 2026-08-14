"""Shared embedding contracts keep semantic dedupe provider-agnostic."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    """Raised when an embedding provider cannot produce a trustworthy vector."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str:
        """Provider identifier stored alongside vectors for diagnostics."""

    @property
    def dimension(self) -> int:
        """Vector width emitted by this provider."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch while preserving input order."""

    def embed_one(self, text: str) -> list[float]:
        """Avoid one-off callers reimplementing batch plumbing."""


def normalize(vec: Sequence[float]) -> list[float]:
    """L2-normalise vectors so downstream cosine similarity is just a dot product."""

    norm = math.sqrt(sum(value * value for value in vec))
    if norm == 0:
        return [0.0 for _ in vec]
    return [value / norm for value in vec]
