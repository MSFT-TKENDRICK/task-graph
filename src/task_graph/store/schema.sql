-- Projection schema for the task graph.
--
-- This database is a *projection* of the activegraph event log, never the
-- source of truth. It can be deleted and rebuilt at any time with `tg rebuild`,
-- which is why it is safe to change aggressively and why `synchronous=NORMAL`
-- is an acceptable durability trade for speed.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- objects --

CREATE TABLE IF NOT EXISTS objects (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}',
    version    INTEGER NOT NULL DEFAULT 0,
    provenance TEXT NOT NULL DEFAULT '{}',

    -- Hot fields lifted out of the JSON bag so they can be indexed. VIRTUAL
    -- keeps them free on write and cheap on read (SQLite recomputes on access).
    source_uri TEXT GENERATED ALWAYS AS (json_extract(data, '$.source_uri')) VIRTUAL,
    state      TEXT GENERATED ALWAYS AS (json_extract(data, '$.state')) VIRTUAL,
    priority   REAL GENERATED ALWAYS AS (json_extract(data, '$.priority')) VIRTUAL,
    approval   TEXT GENERATED ALWAYS AS (json_extract(data, '$.approval')) VIRTUAL
);

CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(type);
CREATE INDEX IF NOT EXISTS idx_objects_state ON objects(type, state);
CREATE INDEX IF NOT EXISTS idx_objects_priority ON objects(priority DESC);
CREATE INDEX IF NOT EXISTS idx_objects_approval ON objects(approval);

-- Ingest idempotency: a source record maps to exactly one source_item object.
-- Partial, because only source_item objects carry `source_uri`.
CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_source_uri
    ON objects(source_uri) WHERE source_uri IS NOT NULL;

-- -------------------------------------------------------------- relations --

CREATE TABLE IF NOT EXISTS relations (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    type       TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT '{}'
);

-- Both directions are indexed because neighbourhood traversal is undirected.
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source, type);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target, type);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(type);

-- ---------------------------------------------------------------- patches --

CREATE TABLE IF NOT EXISTS patches (
    id               TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    op               TEXT NOT NULL,
    value            TEXT NOT NULL DEFAULT '{}',
    expected_version INTEGER NOT NULL DEFAULT 0,
    proposed_by      TEXT NOT NULL DEFAULT '',
    rationale        TEXT,
    evidence         TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'proposed',
    rejection_reason TEXT,
    provenance       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_patches_status ON patches(status);
CREATE INDEX IF NOT EXISTS idx_patches_target ON patches(target);

-- ----------------------------------------------------------------- search --

-- Standalone (not external-content) FTS5 table, maintained explicitly by the
-- store. External-content tables need trigger choreography that would silently
-- drift whenever the searchable-text projection changes; doing it in one place
-- in Python is easier to keep correct.
CREATE VIRTUAL TABLE IF NOT EXISTS objects_fts USING fts5(
    object_id UNINDEXED,
    type UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);

-- ------------------------------------------------------------- embeddings --

CREATE TABLE IF NOT EXISTS embeddings (
    object_id TEXT PRIMARY KEY,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    -- Little-endian float32 array, L2-normalised so cosine == dot product.
    vec       BLOB NOT NULL,
    -- Hash of the text that produced `vec`, so unchanged objects are not
    -- re-embedded on every sync.
    text_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
