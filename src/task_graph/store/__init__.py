"""Storage layer: the SQLite projection of the activegraph event log."""

from task_graph.store.search import RRF_K, SearchHit, SearchIndex, escape_fts_query
from task_graph.store.sqlite_graph_store import (
    SCHEMA_VERSION,
    SqliteGraphStore,
    searchable_text,
)
from task_graph.store.vectors import (
    SqliteVectorIndex,
    VectorHit,
    VectorIndex,
    pack_vector,
    text_fingerprint,
    unpack_vector,
)

__all__ = [
    "RRF_K",
    "SCHEMA_VERSION",
    "SearchHit",
    "SearchIndex",
    "SqliteGraphStore",
    "SqliteVectorIndex",
    "VectorHit",
    "VectorIndex",
    "escape_fts_query",
    "pack_vector",
    "searchable_text",
    "text_fingerprint",
    "unpack_vector",
]
