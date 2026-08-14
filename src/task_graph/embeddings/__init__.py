"""Embedding provider selection for semantic deduplication."""

from __future__ import annotations

from task_graph.config import Settings, get_settings
from task_graph.embeddings.base import EmbeddingError, EmbeddingProvider, normalize
from task_graph.embeddings.foundry_local import FoundryLocalEmbedder
from task_graph.embeddings.hashing import HashingEmbedder

_PROVIDERS: dict[Settings, EmbeddingProvider] = {}


def get_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    """Resolve once per settings object so auto mode does not repeatedly probe Foundry."""

    resolved_settings = settings or get_settings()
    if resolved_settings in _PROVIDERS:
        return _PROVIDERS[resolved_settings]

    provider = resolved_settings.embedding_provider.lower()
    if provider == "hashing":
        embedder: EmbeddingProvider = HashingEmbedder()
    elif provider == "foundry":
        if not FoundryLocalEmbedder.is_available(
            endpoint=resolved_settings.foundry_endpoint,
            model=resolved_settings.foundry_model,
        ):
            msg = (
                "Foundry Local embeddings were requested but the endpoint is unavailable: "
                f"{resolved_settings.foundry_endpoint}"
            )
            raise EmbeddingError(msg)
        embedder = FoundryLocalEmbedder(
            endpoint=resolved_settings.foundry_endpoint,
            model=resolved_settings.foundry_model,
        )
    elif provider == "auto":
        if FoundryLocalEmbedder.is_available(
            endpoint=resolved_settings.foundry_endpoint,
            model=resolved_settings.foundry_model,
        ):
            embedder = FoundryLocalEmbedder(
                endpoint=resolved_settings.foundry_endpoint,
                model=resolved_settings.foundry_model,
            )
        else:
            embedder = HashingEmbedder()
    else:
        msg = f"Unknown embedding provider: {resolved_settings.embedding_provider}"
        raise EmbeddingError(msg)

    _PROVIDERS[resolved_settings] = embedder
    return embedder


def describe_embedder(settings: Settings | None = None) -> str:
    """Summarise embedding health for diagnostics without leaking implementation details."""

    resolved_settings = settings or get_settings()
    embedder = get_embedder(resolved_settings)
    if isinstance(embedder, FoundryLocalEmbedder):
        return f"Foundry Local ({embedder.model}, {embedder.dimension} dimensions)"
    return f"Hashing fallback ({embedder.dimension} dimensions, degraded)"


__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "FoundryLocalEmbedder",
    "HashingEmbedder",
    "describe_embedder",
    "get_embedder",
    "normalize",
]
