"""Deterministic fallback embeddings for offline and reproducible operation."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence

from task_graph.config import DEFAULT_EMBEDDING_DIM
from task_graph.embeddings.base import normalize

_TOKEN_RE = re.compile(r"\b\w+\b", re.UNICODE)
_HASH_PERSON = b"task-graph-embed"


class HashingEmbedder:
    """Provide stable lexical similarity when no model runtime is available."""

    def __init__(self, dimension: int = DEFAULT_EMBEDDING_DIM) -> None:
        if dimension <= 0:
            msg = "Embedding dimension must be positive"
            raise ValueError(msg)
        self._dimension = dimension

    @property
    def name(self) -> str:
        return "hashing"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        counts = Counter(self._features(text))
        vec = [0.0] * self._dimension
        for feature, count in counts.items():
            digest = hashlib.blake2b(
                feature.encode("utf-8"),
                digest_size=16,
                person=_HASH_PERSON,
            ).digest()
            bucket = int.from_bytes(digest[:8], "big") % self._dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[bucket] += sign * (1.0 + math.log(count))
        return normalize(vec)

    @staticmethod
    def _features(text: str) -> list[str]:
        tokens = _TOKEN_RE.findall(text.lower())
        features: list[str] = [f"w:{token}" for token in tokens]
        for token in tokens:
            padded = f"  {token} "
            features.extend(f"c:{padded[index:index + 3]}" for index in range(len(padded) - 2))
        return features
