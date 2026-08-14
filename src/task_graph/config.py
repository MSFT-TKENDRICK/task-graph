"""Runtime configuration and filesystem layout.

Everything the task graph persists lives outside the repository, under
``TASK_GRAPH_HOME`` (default ``~/.task-graph``). No credentials are ever stored:
all source access uses ambient auth (``agency mcp *`` inherits the domain-joined
Entra/WAM session, ``gh`` is already authenticated, Azure uses
``DefaultAzureCredential``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_HOME = "TASK_GRAPH_HOME"
ENV_EMBEDDING_PROVIDER = "TASK_GRAPH_EMBEDDINGS"
ENV_FOUNDRY_ENDPOINT = "TASK_GRAPH_FOUNDRY_ENDPOINT"
ENV_FOUNDRY_MODEL = "TASK_GRAPH_FOUNDRY_MODEL"

DEFAULT_FOUNDRY_ENDPOINT = "http://localhost:5273/v1"
DEFAULT_FOUNDRY_MODEL = "qwen3-embedding-0.6b"

#: Dimensionality used by the deterministic fallback embedder. Foundry Local
#: models report their own dimension, which is recorded per-vector in the store.
DEFAULT_EMBEDDING_DIM = 512


def default_home() -> Path:
    """Return the task-graph state directory, honouring ``TASK_GRAPH_HOME``."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".task-graph"


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings.

    ``events_db`` is the durable append-only log and the sole source of truth.
    ``graph_db`` is a *projection* of it and can be deleted and rebuilt at any
    time via ``tg rebuild``.
    """

    home: Path = field(default_factory=default_home)
    embedding_provider: str = "auto"
    foundry_endpoint: str = DEFAULT_FOUNDRY_ENDPOINT
    foundry_model: str = DEFAULT_FOUNDRY_MODEL

    @property
    def events_db(self) -> Path:
        return self.home / "events.db"

    @property
    def graph_db(self) -> Path:
        return self.home / "graph.db"

    @property
    def weights_path(self) -> Path:
        """Learned dedupe/priority weights, updated from user corrections."""
        return self.home / "weights.json"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    def ensure_home(self) -> Path:
        """Create the state directory tree if absent and return it."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.home

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            home=default_home(),
            embedding_provider=os.environ.get(ENV_EMBEDDING_PROVIDER, "auto"),
            foundry_endpoint=os.environ.get(ENV_FOUNDRY_ENDPOINT, DEFAULT_FOUNDRY_ENDPOINT),
            foundry_model=os.environ.get(ENV_FOUNDRY_MODEL, DEFAULT_FOUNDRY_MODEL),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, resolved from the environment once."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(settings: Settings) -> None:
    """Override the process-wide settings. Intended for tests and the CLI."""
    global _settings
    _settings = settings
