-- mimir.saga schema
--
-- Lives at mimir/saga/schema.sql (formerly mimir/memory/schema.sql before the rename).
--
-- Key differences from current saga:
-- - No `stability`, `retrievability` columns on atoms. Both subsumed
--   by the access_events log.
-- - No state machine for retrieval gating. `tombstoned` is the only
--   lifecycle flag; activation > threshold is the retrieval gate.
-- - access_events is the source of truth for "how often / how recently
--   was this accessed." Petrov OL reads from this on each retrieval.
-- - Embeddings split into their own table (own provider/dim column),
--   so re-embedding under a new provider doesn't rewrite atoms.
-- - observations_metadata is a sidecar for observation-typed atoms
--   (evidence count, trend label, last evidence event) — no NULL
--   columns for raw atoms.

-- ──────────────────────────────────────────────────────────────────
-- Core
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS atoms (
    id TEXT PRIMARY KEY,
    -- Content + identity
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- Stream/profile (preserved from saga; same semantics)
    stream TEXT DEFAULT 'semantic',   -- semantic | episodic | procedural
    profile TEXT DEFAULT 'standard',  -- standard | full | compact
    -- Hierarchy (CLS-flavored)
    memory_type TEXT DEFAULT 'raw' CHECK(memory_type IN ('raw', 'observation', 'mental_model')),
    -- Annotations (carry over from saga)
    arousal REAL DEFAULT 0.5,
    valence REAL DEFAULT 0.0,
    encoding_confidence REAL DEFAULT 0.7,
    topics TEXT DEFAULT '[]',         -- JSON array
    source_type TEXT DEFAULT 'conversation',
    metadata TEXT DEFAULT '{}',       -- JSON for extensible fields
    -- Lifecycle (just two bits — explicit tombstone + soft delete)
    tombstoned INTEGER DEFAULT 0 CHECK(tombstoned IN (0, 1)),
    tombstoned_at TEXT,                -- ISO ts, nullable
    tombstoned_reason TEXT,            -- 'explicit_forget' | 'merged' | 'superseded'
    -- Pinning (forces retrieval regardless of activation)
    is_pinned INTEGER DEFAULT 0,
    -- Identity / namespacing
    agent_id TEXT DEFAULT 'default',
    session_id TEXT,                   -- session-of-origin (informational)
    -- Timestamps
    created_at TEXT NOT NULL,
    -- Ownership (chainlink #881: fail-closed legacy scope for unproven rows)
    owner_principal TEXT NOT NULL DEFAULT 'legacy_admin',  -- 'legacy_admin' | 'service' | 'system' | user-id
    origin_channel TEXT,               -- channel/source where atom originated
    integrity TEXT NOT NULL DEFAULT 'untrusted' CHECK(integrity IN ('trusted', 'untrusted')),
    origin_trigger TEXT,               -- immutable server-selected trigger identity
    origin_ref TEXT,                   -- immutable concrete event/message/source reference
    origin_domain TEXT,                -- domain/namespace of origin
    visibility TEXT NOT NULL DEFAULT 'legacy_admin' CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin')),
    provenance TEXT NOT NULL DEFAULT '{}'       -- JSON: {created_by, origin_url, etc.}
);

CREATE INDEX IF NOT EXISTS idx_atoms_memory_type ON atoms(memory_type);
CREATE INDEX IF NOT EXISTS idx_atoms_agent ON atoms(agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_atoms_dedup
    ON atoms(content_hash, agent_id, owner_principal) WHERE tombstoned = 0;
CREATE INDEX IF NOT EXISTS idx_atoms_created ON atoms(created_at);
CREATE INDEX IF NOT EXISTS idx_atoms_tombstoned ON atoms(tombstoned);
-- session_id index: reflect._session_atoms + recall's recent-session
-- lookup both filter on this. Partial index (skips NULL session_id
-- rows for pre-session atoms like boundaries / observations) keeps
-- the index lean. Matters at 10k+ atoms.
CREATE INDEX IF NOT EXISTS idx_atoms_session
    ON atoms(session_id) WHERE session_id IS NOT NULL;
-- Ownership indexes (chainlink #881)
CREATE INDEX IF NOT EXISTS idx_atoms_visibility ON atoms(visibility);
CREATE INDEX IF NOT EXISTS idx_atoms_owner ON atoms(owner_principal);

-- ──────────────────────────────────────────────────────────────────
-- Access events — Petrov OL backing
-- ──────────────────────────────────────────────────────────────────

-- Each retrieval, store, feedback, and consolidation contribution
-- appends one row. The activation formula reads this table to compute
-- B_i = ln(Σ_j (now - t_j)^(-d)) on demand.
--
-- Insert-only (no UPDATE/DELETE in steady state). Volume estimate:
-- ~50 retrievals × 12 atoms = 600 events/turn × 100 turns/day = 60k/day.
-- Over a year that's ~22M rows; with the per-atom-recent projection
-- (see access_events_recent below) the activation read path doesn't
-- scan the full log.
CREATE TABLE IF NOT EXISTS access_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    atom_id TEXT NOT NULL,
    ts TEXT NOT NULL,                 -- ISO ts
    source TEXT NOT NULL,             -- 'retrieval' | 'store' | 'feedback' | 'consolidation' | 'pinned_init'
    weight REAL DEFAULT 1.0,          -- multiplier (feedback=2.0, retrieval=1.0, consolidation=0.5)
    session_id TEXT,                  -- per-session attribution for reflect
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_access_atom_ts ON access_events(atom_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_access_session ON access_events(session_id);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_events(ts);

-- Per-atom recent-K cache (Petrov hybrid): the K most recent access
-- timestamps as a JSON array, plus aggregate stats for older events.
-- Updated incrementally on each insert into access_events (via
-- trigger or app-side dual-write). Lets the activation read path
-- be O(K) instead of O(n_accesses) per atom.
--
-- This is a denormalization for read speed. Activation can fall back
-- to scanning access_events if this is stale or missing.
CREATE TABLE IF NOT EXISTS atom_access_summary (
    atom_id TEXT PRIMARY KEY,
    recent_ts_json TEXT DEFAULT '[]', -- JSON array of K most recent ISO ts (newest first)
    recent_weights_json TEXT DEFAULT '[]',  -- parallel array of weights
    old_count INTEGER DEFAULT 0,      -- number of access events displaced from recent
    old_weight_sum REAL DEFAULT 0.0,  -- sum of weights of displaced events
    old_oldest_ts TEXT,               -- timestamp of the oldest displaced event
    last_updated_ts TEXT,
    FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
);

-- ──────────────────────────────────────────────────────────────────
-- Embeddings — separate from atoms so we can swap providers without
-- rewriting the atoms table
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS embeddings (
    atom_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,           -- 'voyage' | 'openai' | 'onnx' | ...
    model TEXT NOT NULL,              -- 'voyage-4-lite' | 'text-embedding-3-small' | ...
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,                -- raw float32 bytes
    embedded_at TEXT NOT NULL,
    FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_emb_provider ON embeddings(provider);

-- ──────────────────────────────────────────────────────────────────
-- Observation metadata (sidecar; only populated for memory_type='observation')
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS observations_metadata (
    atom_id TEXT PRIMARY KEY,
    evidence_count INTEGER DEFAULT 0,
    trend TEXT,                       -- 'stable' | 'strengthening' | 'weakening' | 'stale' | NULL
    last_evidence_at TEXT,
    consolidated_at TEXT NOT NULL,    -- when the observation was first synthesized
    consolidation_session TEXT,       -- which reflect session produced it
    -- Ownership mirroring atoms (chainlink #881)
    owner_principal TEXT NOT NULL DEFAULT 'legacy_admin',
    origin_channel TEXT,
    origin_domain TEXT,
    visibility TEXT NOT NULL DEFAULT 'legacy_admin' CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin')),
    provenance TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
);

-- Ownership indexes (chainlink #881)
CREATE INDEX IF NOT EXISTS idx_obs_metadata_visibility ON observations_metadata(visibility);
CREATE INDEX IF NOT EXISTS idx_obs_metadata_owner ON observations_metadata(owner_principal);

-- ──────────────────────────────────────────────────────────────────
-- Relations between atoms (carry over from saga, simplified)
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS atom_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    -- 'evidenced_by'      : observation ←- raw (semantic claim of support)
    -- 'consolidated_into' : raw -→ observation (reverse-index of evidenced_by)
    -- 'supersedes'        : newer observation -→ older observation it replaces
    -- 'contradicts'       : symmetric — two atoms make incompatible claims
    -- 'corrects'          : explicit user-driven correction (atom_b corrects atom_a)
    relation_type TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, relation_type),
    FOREIGN KEY (source_id) REFERENCES atoms(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES atoms(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relations_source ON atom_relations(source_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_relations_target ON atom_relations(target_id, relation_type);

-- ──────────────────────────────────────────────────────────────────
-- Topics (carry over)
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS atom_topics (
    atom_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    PRIMARY KEY (atom_id, topic),
    FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_topics_topic ON atom_topics(topic);

-- ──────────────────────────────────────────────────────────────────
-- Triples — subject-predicate-object with temporal validity (carry over)
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS triples (
    -- Content-addressed: hash(subject:predicate:object) lower-cased, so
    -- identical claims from different source atoms dedup into one row.
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,         -- lower_snake_case
    object TEXT NOT NULL,
    source_atom_id TEXT,             -- the originating raw/observation
    confidence REAL DEFAULT 1.0,
    valid_from TEXT,                  -- ISO ts, NULL = "from forever"
    valid_until TEXT,                 -- ISO ts, NULL = "still valid"
    -- Embedding of "{subject} {predicate_readable} {object}" so retrieval
    -- can cosine-match the query against triple statements directly
    -- (P41-style triple_augment_v2). Same provider/dim as the atom
    -- embeddings; mismatched-dim rows get filtered by the index loader.
    embedding BLOB,
    embedding_dim INTEGER,
    tombstoned INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    -- Ownership (chainlink #881: fail-closed legacy scope)
    owner_principal TEXT NOT NULL DEFAULT 'legacy_admin',
    origin_channel TEXT,
    origin_domain TEXT,
    visibility TEXT NOT NULL DEFAULT 'legacy_admin' CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin')),
    provenance TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (source_atom_id) REFERENCES atoms(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_triples_spo ON triples(subject, predicate, object);
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject) WHERE tombstoned = 0;
CREATE INDEX IF NOT EXISTS idx_triples_current ON triples(subject, predicate) WHERE valid_until IS NULL AND tombstoned = 0;
-- Ownership indexes (chainlink #881)
CREATE INDEX IF NOT EXISTS idx_triples_visibility ON triples(visibility);
CREATE INDEX IF NOT EXISTS idx_triples_owner ON triples(owner_principal);

-- ──────────────────────────────────────────────────────────────────
-- World state (P37, derived from triples)
-- ──────────────────────────────────────────────────────────────────
--
-- Tracks the current value of every (subject, predicate) pair plus
-- its history. Built incrementally as triples land: a new triple for
-- a (subj, pred) that already has a current value end-dates the
-- previous current row (valid_until = new.valid_from) and inserts a
-- new is_current=1 row.
--
-- Production callers query ``get_current_value(subject, predicate)``
-- to answer "what is X's current Y?" without scanning atoms. Bench is
-- off (per saga); table exists so the triples writer can populate it
-- and consumers can flip on later.

CREATE TABLE IF NOT EXISTS world_state (
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    is_current INTEGER DEFAULT 1,
    source_triple_id TEXT,
    updated_at TEXT NOT NULL,
    owner_principal TEXT NOT NULL DEFAULT 'legacy_admin',
    origin_channel TEXT,
    origin_domain TEXT,
    visibility TEXT NOT NULL DEFAULT 'legacy_admin'
        CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin')),
    provenance TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (subject, predicate, valid_from),
    FOREIGN KEY (source_triple_id) REFERENCES triples(id)
);
CREATE INDEX IF NOT EXISTS idx_world_current
    ON world_state(subject, predicate) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_world_subject ON world_state(subject);
CREATE INDEX IF NOT EXISTS idx_world_visibility ON world_state(visibility);
CREATE INDEX IF NOT EXISTS idx_world_owner ON world_state(owner_principal);

-- ──────────────────────────────────────────────────────────────────
-- FTS5 keyword search on atoms (carry over)
-- ──────────────────────────────────────────────────────────────────

CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts USING fts5(
    content,
    content='atoms',
    content_rowid='rowid'
);

-- External-content FTS5 doesn't auto-sync from atoms. These three
-- triggers keep atoms_fts current. INSERT/UPDATE write the new content
-- so MATCH finds it; DELETE uses the FTS5 delete-on-rowid sentinel to
-- drop the old row before re-inserting (UPDATE) or just to remove (DELETE).
-- Tombstoning is *not* deletion — the row stays so audit reads still
-- work; the WHERE-tombstoned=0 clause in the FTS query path is what
-- excludes tombstoned atoms from search results.
CREATE TRIGGER IF NOT EXISTS atoms_fts_insert
AFTER INSERT ON atoms
BEGIN
    INSERT INTO atoms_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS atoms_fts_delete
AFTER DELETE ON atoms
BEGIN
    INSERT INTO atoms_fts(atoms_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS atoms_fts_update
AFTER UPDATE OF content ON atoms
BEGIN
    INSERT INTO atoms_fts(atoms_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
    INSERT INTO atoms_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- ──────────────────────────────────────────────────────────────────
-- Sessions (informational — sessions live in mimir's lifecycle layer
-- but a sidecar table lets reflect query "what happened this session")
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    channel_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT,
    reflected_at TEXT,
    -- Structured boundary fields (populated by reflect/end_session)
    topics_discussed TEXT NOT NULL DEFAULT '[]',   -- JSON array
    decisions_made   TEXT NOT NULL DEFAULT '[]',   -- JSON array
    unfinished       TEXT NOT NULL DEFAULT '[]',   -- JSON array
    emotional_state  TEXT,
    closed_since     TEXT NOT NULL DEFAULT '[]',   -- JSON array (chainlink #63)
    embedding        BLOB,                          -- session summary embedding (chainlink #148)
    embedding_dim    INTEGER,                        -- embedding dimension (chainlink #148)
    -- Ownership (chainlink #881: fail-closed legacy scope)
    owner_principal TEXT NOT NULL DEFAULT 'legacy_admin',
    origin_channel TEXT,
    origin_domain TEXT,
    visibility TEXT NOT NULL DEFAULT 'legacy_admin' CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin')),
    provenance TEXT NOT NULL DEFAULT '{}'
);

-- Ownership indexes (chainlink #881)
CREATE INDEX IF NOT EXISTS idx_sessions_visibility ON sessions(visibility);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_principal);
CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel_id);

-- ──────────────────────────────────────────────────────────────────
-- Schema version
-- ──────────────────────────────────────────────────────────────────
-- One row per applied migration. ``MemoryClient._ensure_conn`` writes
-- version 1 on fresh-DB init via ``INSERT OR IGNORE``. Future schema
-- changes should ship as ``mimir/memory/migrations/NNN_*.sql`` plus a
-- ``MIGRATIONS = {N: "..."}`` registry entry in ``client.py``; the
-- conn-init code applies any pending migrations whose version isn't
-- yet in this table.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
