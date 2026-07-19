"""SAGA SQLite schema migrations (chainlink #242, Phase 1).

Lifted out of ``mimir/saga/client.py`` (was an inline class-level dict +
two methods inside the 1998-line ``SagaStore`` class). The migration
registry is now editable in isolation — adding a v7 migration is a one-
location change instead of a hunt through saga/client.py.

The public surface:

- :data:`CURRENT_SCHEMA_VERSION` — what ``schema.sql`` produces.
- :data:`MIGRATIONS` — ``{version: SQL script}`` registry of post-
  greenfield transformations.  Keys must be contiguous starting at 2
  and equal :data:`CURRENT_SCHEMA_VERSION` at the latest entry.
- :func:`detect_schema_version` — PRAGMA-based introspection for
  pre-migration-era DBs (chainlink #175).
- :func:`apply_pending_migrations` — walks the registry and applies
  pending DDL statement-by-statement inside per-migration transactions.

Tests monkeypatch the registry by setting ``SagaStore.MIGRATIONS`` and
``SagaStore.CURRENT_SCHEMA_VERSION``; those class attributes still
exist on ``SagaStore`` as references to the module-level values, and
the ``SagaStore._apply_pending_migrations`` wrapper reads them at call
time so monkeypatched values are honored.
"""

from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timezone
from typing import Callable, Iterator


CURRENT_SCHEMA_VERSION: int = 10

# Registry of post-greenfield schema changes. Keys are version
# numbers (must be > 1, must be contiguous, must equal
# ``CURRENT_SCHEMA_VERSION`` at the latest entry); values are raw
# SQL scripts executed inside per-migration transactions.
MIGRATIONS: dict[int, str] = {
    2: """
-- Ensure sessions table exists on DBs created before schema.sql included it.
CREATE TABLE IF NOT EXISTS sessions (
id TEXT PRIMARY KEY,
channel_id TEXT,
started_at TEXT NOT NULL,
ended_at TEXT,
summary TEXT,
reflected_at TEXT
);

-- Backfill sessions rows from existing session_boundary atoms that have
-- a non-NULL session_id and no corresponding sessions row.
-- started_at / ended_at fall back to the atom's created_at (best available).
INSERT OR IGNORE INTO sessions (id, channel_id, started_at, ended_at, summary, reflected_at)
SELECT
a.session_id,
NULL,
a.created_at,
a.created_at,
a.content,
a.created_at
FROM atoms a
WHERE a.source_type = 'session_boundary'
  AND a.session_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = a.session_id);
""",
    3: """
        -- Add structured boundary fields to sessions table (chainlink #63).
        ALTER TABLE sessions ADD COLUMN topics_discussed TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE sessions ADD COLUMN decisions_made   TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE sessions ADD COLUMN unfinished       TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE sessions ADD COLUMN emotional_state  TEXT;
        ALTER TABLE sessions ADD COLUMN closed_since     TEXT NOT NULL DEFAULT '[]';
    """,
    4: """
        -- Add embedding columns to sessions for search_sessions() (chainlink #148).
        ALTER TABLE sessions ADD COLUMN embedding BLOB;
        ALTER TABLE sessions ADD COLUMN embedding_dim INTEGER;
    """,
    5: """
        -- Sessions migration final step: delete session_boundary atoms
        -- entirely. They've been backfilled into the sessions table since
        -- migration 2; the structured fields landed in 3; embeddings in 4.
        -- This migration completes the move by deleting the redundant
        -- atom rows + all their dependents (access_events, embeddings,
        -- atom_relations). After this runs, no atom has
        -- source_type='session_boundary'.
        --
        -- Migration 2 already backfilled most rows with the bare summary
        -- (atom.content); we top up the structured fields from
        -- atom.metadata here in case the row was inserted with empty
        -- topics_discussed/decisions_made/etc.
        UPDATE sessions
        SET
            topics_discussed = COALESCE(
                NULLIF(topics_discussed, '[]'),
                (
                    SELECT json_extract(a.metadata, '$.topics_discussed')
                    FROM atoms a
                    WHERE a.source_type = 'session_boundary'
                      AND a.session_id = sessions.id
                      AND json_extract(a.metadata, '$.topics_discussed') IS NOT NULL
                    LIMIT 1
                ),
                topics_discussed
            ),
            decisions_made = COALESCE(
                NULLIF(decisions_made, '[]'),
                (
                    SELECT json_extract(a.metadata, '$.decisions_made')
                    FROM atoms a
                    WHERE a.source_type = 'session_boundary'
                      AND a.session_id = sessions.id
                      AND json_extract(a.metadata, '$.decisions_made') IS NOT NULL
                    LIMIT 1
                ),
                decisions_made
            ),
            unfinished = COALESCE(
                NULLIF(unfinished, '[]'),
                (
                    SELECT json_extract(a.metadata, '$.unfinished')
                    FROM atoms a
                    WHERE a.source_type = 'session_boundary'
                      AND a.session_id = sessions.id
                      AND json_extract(a.metadata, '$.unfinished') IS NOT NULL
                    LIMIT 1
                ),
                unfinished
            ),
            emotional_state = COALESCE(
                emotional_state,
                (
                    SELECT json_extract(a.metadata, '$.emotional_state')
                    FROM atoms a
                    WHERE a.source_type = 'session_boundary'
                      AND a.session_id = sessions.id
                    LIMIT 1
                )
            ),
            closed_since = COALESCE(
                NULLIF(closed_since, '[]'),
                (
                    SELECT json_extract(a.metadata, '$.closed_since')
                    FROM atoms a
                    WHERE a.source_type = 'session_boundary'
                      AND a.session_id = sessions.id
                      AND json_extract(a.metadata, '$.closed_since') IS NOT NULL
                    LIMIT 1
                ),
                closed_since
            )
        WHERE EXISTS (
            SELECT 1 FROM atoms a
            WHERE a.source_type = 'session_boundary'
              AND a.session_id = sessions.id
        );

        -- Delete all dependents BEFORE deleting the atoms themselves.
        -- Every table with a FK to atoms(id) **in mimir.saga's schema**
        -- must be cleaned here or the final ``DELETE FROM atoms`` fails
        -- with ``FOREIGN KEY constraint failed`` and the whole migration
        -- rolls back. The original v5 missed ``atom_topics`` — a
        -- production DB with 1756 boundary-atom rows there caught it
        -- (silently, because ``_ensure_conn`` was caching the
        -- half-init connection — fixed below in this same commit).
        --
        -- ``triples`` is also in our schema (boundary atoms shouldn't
        -- normally have triples but the FK exists). Tables like
        -- ``access_log`` / ``corrections`` come from saga's vendored
        -- schema and aren't referenced here; if they exist on a
        -- legacy-migrated DB and have boundary refs, that's handled
        -- by saga's own migration toolchain, not this v5 step.
        --
        -- Order is enforced by SQLite FK semantics: dependents first,
        -- then the atoms.
        DELETE FROM atom_access_summary
        WHERE atom_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        );

        DELETE FROM access_events
        WHERE atom_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        );

        DELETE FROM embeddings
        WHERE atom_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        );

        -- atom_topics — many-to-many table; production saw 1756
        -- boundary-atom rows here, which is what caused the original
        -- v5 to FK-fail.
        DELETE FROM atom_topics
        WHERE atom_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        );

        -- triples — observation/raw atoms can be the source_atom_id
        -- of extracted triples. Boundary atoms shouldn't have triples
        -- in normal flow but a stray one would FK-fail.
        DELETE FROM triples
        WHERE source_atom_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        );

        -- atom_relations: source_id OR target_id pointing at a boundary
        -- atom. All session_member edges (boundary→raw) are caught here.
        DELETE FROM atom_relations
        WHERE source_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        ) OR target_id IN (
            SELECT id FROM atoms WHERE source_type = 'session_boundary'
        );

        -- Finally drop the atoms themselves. atoms_fts is kept in sync
        -- by the DELETE trigger in schema.sql.
        DELETE FROM atoms WHERE source_type = 'session_boundary';
    """,
    6: """
        -- v6: Add ON DELETE CASCADE to all FK constraints that reference
        -- atoms(id), and ON DELETE SET NULL to triples.source_atom_id.
        -- Fixes index_integrity_failed algedonic signal caused by orphaned
        -- rows in access_events / atom_access_summary (chainlink #161).
        --
        -- Root cause: the v5 migration had a partial-migration window
        -- (original v5 missed atom_topics, causing FK rollback; combined
        -- with the _ensure_conn half-init caching bug). The orphaned rows
        -- are survivors of that window. The cleanup below removes them;
        -- the CASCADE constraints prevent new orphans from any future
        -- atom deletion path.
        --
        -- SQLite does not support ALTER TABLE ... MODIFY CONSTRAINT, so
        -- we use the standard CREATE + COPY + DROP + RENAME pattern.
        -- NOTE: FK enforcement is disabled at the connection level by
        -- _apply_pending_migrations before applying this script, and
        -- restored afterwards. PRAGMA foreign_keys=OFF/ON inside the
        -- migration script would be a no-op because PRAGMAs that modify
        -- connection state cannot be set within a transaction.

        -- ── Orphan cleanup (belt + suspenders; the COPY step filters
        --    too, but explicit DELETEs make the before/after legible) ──
        DELETE FROM access_events
            WHERE atom_id NOT IN (SELECT id FROM atoms);
        DELETE FROM atom_access_summary
            WHERE atom_id NOT IN (SELECT id FROM atoms);
        DELETE FROM embeddings
            WHERE atom_id NOT IN (SELECT id FROM atoms);
        DELETE FROM observations_metadata
            WHERE atom_id NOT IN (SELECT id FROM atoms);
        DELETE FROM atom_topics
            WHERE atom_id NOT IN (SELECT id FROM atoms);
        DELETE FROM atom_relations
            WHERE source_id NOT IN (SELECT id FROM atoms)
               OR target_id NOT IN (SELECT id FROM atoms);
        -- triples: NULL out source_atom_id for orphaned references
        -- (the triple is still valid knowledge even without its source).
        UPDATE triples SET source_atom_id = NULL
            WHERE source_atom_id IS NOT NULL
              AND source_atom_id NOT IN (SELECT id FROM atoms);

        -- ── access_events ──────────────────────────────────────────
        CREATE TABLE new_access_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atom_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            session_id TEXT,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
        );
        INSERT INTO new_access_events
            SELECT id, atom_id, ts, source, weight, session_id, metadata
            FROM access_events;
        DROP TABLE access_events;
        ALTER TABLE new_access_events RENAME TO access_events;
        CREATE INDEX IF NOT EXISTS idx_access_atom_ts
            ON access_events(atom_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_access_session
            ON access_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_access_ts ON access_events(ts);

        -- ── atom_access_summary ────────────────────────────────────
        CREATE TABLE new_atom_access_summary (
            atom_id TEXT PRIMARY KEY,
            recent_ts_json TEXT DEFAULT '[]',
            recent_weights_json TEXT DEFAULT '[]',
            old_count INTEGER DEFAULT 0,
            old_weight_sum REAL DEFAULT 0.0,
            old_oldest_ts TEXT,
            last_updated_ts TEXT,
            FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
        );
        INSERT INTO new_atom_access_summary
            (atom_id, recent_ts_json, recent_weights_json, old_count, old_weight_sum, old_oldest_ts, last_updated_ts)
            SELECT atom_id, recent_ts_json, recent_weights_json, old_count, old_weight_sum, old_oldest_ts, last_updated_ts
            FROM atom_access_summary;
        DROP TABLE atom_access_summary;
        ALTER TABLE new_atom_access_summary RENAME TO atom_access_summary;

        -- ── embeddings ─────────────────────────────────────────────
        CREATE TABLE new_embeddings (
            atom_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vec BLOB NOT NULL,
            embedded_at TEXT NOT NULL,
            FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
        );
        INSERT INTO new_embeddings
            (atom_id, provider, model, dim, vec, embedded_at)
            SELECT atom_id, provider, model, dim, vec, embedded_at
            FROM embeddings;
        DROP TABLE embeddings;
        ALTER TABLE new_embeddings RENAME TO embeddings;
        CREATE INDEX IF NOT EXISTS idx_emb_provider ON embeddings(provider);

        -- ── observations_metadata ──────────────────────────────────
        CREATE TABLE new_observations_metadata (
            atom_id TEXT PRIMARY KEY,
            evidence_count INTEGER DEFAULT 0,
            trend TEXT,
            last_evidence_at TEXT,
            consolidated_at TEXT NOT NULL,
            consolidation_session TEXT,
            FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
        );
        INSERT INTO new_observations_metadata
            (atom_id, evidence_count, trend, last_evidence_at, consolidated_at, consolidation_session)
            SELECT atom_id, evidence_count, trend, last_evidence_at, consolidated_at, consolidation_session
            FROM observations_metadata;
        DROP TABLE observations_metadata;
        ALTER TABLE new_observations_metadata
            RENAME TO observations_metadata;

        -- ── atom_topics ────────────────────────────────────────────
        CREATE TABLE new_atom_topics (
            atom_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            PRIMARY KEY (atom_id, topic),
            FOREIGN KEY (atom_id) REFERENCES atoms(id) ON DELETE CASCADE
        );
        INSERT INTO new_atom_topics (atom_id, topic)
            SELECT atom_id, topic FROM atom_topics;
        DROP TABLE atom_topics;
        ALTER TABLE new_atom_topics RENAME TO atom_topics;
        CREATE INDEX IF NOT EXISTS idx_topics_topic ON atom_topics(topic);

        -- ── atom_relations ─────────────────────────────────────────
        CREATE TABLE new_atom_relations (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            PRIMARY KEY (source_id, target_id, relation_type),
            FOREIGN KEY (source_id) REFERENCES atoms(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES atoms(id) ON DELETE CASCADE
        );
        INSERT INTO new_atom_relations
            (source_id, target_id, relation_type, confidence, created_at, metadata)
            SELECT source_id, target_id, relation_type, confidence, created_at, metadata
            FROM atom_relations;
        DROP TABLE atom_relations;
        ALTER TABLE new_atom_relations RENAME TO atom_relations;
        CREATE INDEX IF NOT EXISTS idx_relations_source
            ON atom_relations(source_id, relation_type);
        CREATE INDEX IF NOT EXISTS idx_relations_target
            ON atom_relations(target_id, relation_type);

        -- ── triples ────────────────────────────────────────────────
        -- source_atom_id is nullable; ON DELETE SET NULL preserves the
        -- triple's knowledge even when its originating atom is forgotten.
        CREATE TABLE new_triples (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            source_atom_id TEXT,
            confidence REAL DEFAULT 1.0,
            valid_from TEXT,
            valid_until TEXT,
            embedding BLOB,
            embedding_dim INTEGER,
            tombstoned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (source_atom_id) REFERENCES atoms(id)
                ON DELETE SET NULL
        );
        INSERT INTO new_triples
            (id, subject, predicate, object, source_atom_id, confidence, valid_from, valid_until, embedding, embedding_dim, tombstoned, created_at, metadata)
            SELECT id, subject, predicate, object, source_atom_id, confidence, valid_from, valid_until, embedding, embedding_dim, tombstoned, created_at, metadata
            FROM triples;
        DROP TABLE triples;
        ALTER TABLE new_triples RENAME TO triples;
        CREATE INDEX IF NOT EXISTS idx_triples_spo
            ON triples(subject, predicate, object);
        CREATE INDEX IF NOT EXISTS idx_triples_subject
            ON triples(subject) WHERE tombstoned = 0;
        CREATE INDEX IF NOT EXISTS idx_triples_current
            ON triples(subject, predicate)
            WHERE valid_until IS NULL AND tombstoned = 0;
    """,
    7: """
        -- v7: Add ownership columns (chainlink #881)
        -- Add owner_principal, origin_channel, origin_domain, visibility,
        -- provenance to atoms, sessions, observations_metadata, and triples.
        -- Pre-existing rows get fail-closed 'legacy_admin' visibility.

        -- ── atoms ─────────────────────────────────────────────────
        ALTER TABLE atoms ADD COLUMN owner_principal TEXT NOT NULL DEFAULT 'legacy_admin';
        ALTER TABLE atoms ADD COLUMN origin_channel TEXT;
        ALTER TABLE atoms ADD COLUMN origin_domain TEXT;
        ALTER TABLE atoms ADD COLUMN visibility TEXT NOT NULL DEFAULT 'legacy_admin'
            CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin'));
        ALTER TABLE atoms ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}';

        -- ── sessions ───────────────────────────────────────────────
        ALTER TABLE sessions ADD COLUMN owner_principal TEXT NOT NULL DEFAULT 'legacy_admin';
        ALTER TABLE sessions ADD COLUMN origin_channel TEXT;
        ALTER TABLE sessions ADD COLUMN origin_domain TEXT;
        ALTER TABLE sessions ADD COLUMN visibility TEXT NOT NULL DEFAULT 'legacy_admin'
            CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin'));
        ALTER TABLE sessions ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}';

        -- ── observations_metadata ─────────────────────────────────
        ALTER TABLE observations_metadata ADD COLUMN owner_principal TEXT NOT NULL DEFAULT 'legacy_admin';
        ALTER TABLE observations_metadata ADD COLUMN origin_channel TEXT;
        ALTER TABLE observations_metadata ADD COLUMN origin_domain TEXT;
        ALTER TABLE observations_metadata ADD COLUMN visibility TEXT NOT NULL DEFAULT 'legacy_admin'
            CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin'));
        ALTER TABLE observations_metadata ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}';

        -- ── triples ────────────────────────────────────────────────
        ALTER TABLE triples ADD COLUMN owner_principal TEXT NOT NULL DEFAULT 'legacy_admin';
        ALTER TABLE triples ADD COLUMN origin_channel TEXT;
        ALTER TABLE triples ADD COLUMN origin_domain TEXT;
        ALTER TABLE triples ADD COLUMN visibility TEXT NOT NULL DEFAULT 'legacy_admin'
            CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin'));
        ALTER TABLE triples ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}';

        -- ── Indexes for ownership columns (chainlink #881) ────────
        CREATE INDEX IF NOT EXISTS idx_atoms_visibility ON atoms(visibility);
        CREATE INDEX IF NOT EXISTS idx_atoms_owner ON atoms(owner_principal);
        CREATE INDEX IF NOT EXISTS idx_sessions_visibility ON sessions(visibility);
        CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_principal);
        CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel_id);
        CREATE INDEX IF NOT EXISTS idx_obs_metadata_visibility ON observations_metadata(visibility);
        CREATE INDEX IF NOT EXISTS idx_obs_metadata_owner ON observations_metadata(owner_principal);
        CREATE INDEX IF NOT EXISTS idx_triples_visibility ON triples(visibility);
        CREATE INDEX IF NOT EXISTS idx_triples_owner ON triples(owner_principal);
    """,
    8: """
        -- v8: Add ownership columns to world_state (chainlink #884)
        -- Add owner_principal, origin_channel, origin_domain, visibility,
        -- provenance to world_state for ACL inheritance from source triples.

        ALTER TABLE world_state ADD COLUMN owner_principal TEXT NOT NULL DEFAULT 'legacy_admin';
        ALTER TABLE world_state ADD COLUMN origin_channel TEXT;
        ALTER TABLE world_state ADD COLUMN origin_domain TEXT;
        ALTER TABLE world_state ADD COLUMN visibility TEXT NOT NULL DEFAULT 'legacy_admin'
            CHECK(visibility IN ('public', 'private', 'service', 'legacy_admin'));
        ALTER TABLE world_state ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}';

        CREATE INDEX IF NOT EXISTS idx_world_visibility ON world_state(visibility);
        CREATE INDEX IF NOT EXISTS idx_world_owner ON world_state(owner_principal);
    """,
    9: """
        -- v9: exact-content dedup is owner-scoped (chainlink #895).
        -- The old index made a different owner's equal content conflict even
        -- though the read-side dedup lookup correctly excluded that row.
        DROP INDEX IF EXISTS idx_atoms_dedup;
        CREATE UNIQUE INDEX idx_atoms_dedup
            ON atoms(content_hash, agent_id, owner_principal)
            WHERE tombstoned = 0;
    """,
    10: """
        -- v10: Immutable, server-stamped provenance for recallable atoms.
        -- Legacy rows fail closed as untrusted and have no invented origin.
        ALTER TABLE atoms ADD COLUMN integrity TEXT NOT NULL DEFAULT 'untrusted'
            CHECK(integrity IN ('trusted', 'untrusted'));
        ALTER TABLE atoms ADD COLUMN origin_trigger TEXT;
        ALTER TABLE atoms ADD COLUMN origin_ref TEXT;
    """,
}


def detect_schema_version(conn: sqlite3.Connection) -> int:
    """Infer the structural schema version of an existing DB by
    inspecting PRAGMA table_info / foreign_key_list. Used by
    :func:`apply_pending_migrations` when ``schema_version`` is empty
    and we can't trust the stamped value — closes chainlink #175
    (the "fresh=False + empty applied" footgun where the harness
    would stamp ``CURRENT_SCHEMA_VERSION`` and silently skip every
    migration).

    Detection markers, highest version first:

    - **v8**: ``world_state`` has ``visibility`` column. The v8 migration
      added ownership columns (owner_principal, origin_channel, origin_domain,
      visibility, provenance) to world_state for ACL inheritance from
      source triples. ``PRAGMA table_info(world_state)`` returns the
      visibility column iff v8 ran.
    - **v7**: ``atoms`` has ``visibility`` column. The v7 migration added
      ownership columns (owner_principal, origin_channel, origin_domain,
      visibility, provenance) to atoms, sessions, observations_metadata,
      and triples. ``PRAGMA table_info(atoms)`` returns the visibility
      column iff v7 ran.
    - **v6**: ``access_events`` carries a foreign key onto
      ``atoms(id)``. The v6 migration rebuilt every dependent
      table to add FK + ON DELETE CASCADE; pre-v6 they were
      standalone. ``PRAGMA foreign_key_list(access_events)``
      returns the FK row iff v6 ran.
    - **v4** (collapses v4/v5; v5 is a data-only delete that's
      schema-indistinguishable from v4 and idempotent under
      re-run): ``sessions`` has ``embedding_dim`` column.
    - **v3**: ``sessions`` has ``topics_discussed`` column.
    - **v2**: ``sessions`` table exists at all.
    - **v1**: no ``sessions`` table — pre-migration era.

    Returns the highest detected version. The caller stamps
    baselines 1..detected, then applies migrations beyond.
    Missing tables return empty rows from PRAGMA (no error),
    so this is robust to bare-bones DBs (like the in-memory
    fixtures used by unit tests).
    """
    # v10 marker: recallable-write integrity provenance (chainlink #948).
    try:
        atoms_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(atoms)").fetchall()
        }
    except sqlite3.OperationalError:
        atoms_cols = set()
    if {"integrity", "origin_trigger", "origin_ref"} <= atoms_cols:
        return 10

    # v9 marker: owner-scoped exact-content dedup index (chainlink #895).
    try:
        dedup_columns = [
            row[2]
            for row in conn.execute("PRAGMA index_info(idx_atoms_dedup)").fetchall()
        ]
    except sqlite3.OperationalError:
        dedup_columns = []
    if dedup_columns == ["content_hash", "agent_id", "owner_principal"]:
        return 9

    # v8 marker: world_state.visibility column exists (chainlink #884).
    try:
        world_state_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(world_state)").fetchall()
        }
    except sqlite3.OperationalError:
        world_state_cols = set()
    if "visibility" in world_state_cols:
        return 8

    # v7 marker: atoms.visibility column exists.
    try:
        atoms_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(atoms)").fetchall()
        }
    except sqlite3.OperationalError:
        atoms_cols = set()
    if "visibility" in atoms_cols:
        return 7

    # v6 marker: FK on access_events.atom_id → atoms(id).
    try:
        fks = conn.execute(
            "PRAGMA foreign_key_list(access_events)"
        ).fetchall()
    except sqlite3.OperationalError:
        fks = []
    if any(row[2] == "atoms" for row in fks):
        return 6

    # Below v6 — inspect sessions table presence + columns.
    has_sessions = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='sessions'"
    ).fetchone() is not None
    if not has_sessions:
        return 1

    sessions_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "embedding_dim" in sessions_cols:
        # v4 or v5 (v5 is a data-only delete + UPDATEs, idempotent
        # under re-run; treating as v4 is safe and lets v5 fire as
        # a no-op).
        return 4
    if "topics_discussed" in sessions_cols:
        return 3
    return 2  # sessions exists but no structured columns


def _statement_has_sql(statement: str) -> bool:
    without_line_comments = re.sub(r"--[^\n]*", "", statement)
    without_comments = re.sub(
        r"/\*.*?\*/", "", without_line_comments, flags=re.DOTALL
    )
    return bool(without_comments.strip())


def _iter_sql_statements(script: str) -> Iterator[str]:
    pending: list[str] = []
    for char in script:
        pending.append(char)
        statement = "".join(pending)
        if sqlite3.complete_statement(statement):
            if _statement_has_sql(statement):
                yield statement
            pending.clear()

    statement = "".join(pending)
    if _statement_has_sql(statement):
        yield statement


_ADD_COLUMN_RE = re.compile(
    r"""
    \A\s*
    (?:--[^\n]*\n\s*)*
    ALTER\s+TABLE\s+
    (?:"(?P<table_dq>[^"]+)"|`(?P<table_bt>[^`]+)`|\[(?P<table_br>[^\]]+)\]|(?P<table>\w+))
    \s+ADD\s+(?:COLUMN\s+)?
    (?:"(?P<col_dq>[^"]+)"|`(?P<col_bt>[^`]+)`|\[(?P<col_br>[^\]]+)\]|(?P<col>\w+))
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _identifier_from_match(match: re.Match[str], *names: str) -> str:
    for name in names:
        value = match.group(name)
        if value is not None:
            return value
    raise AssertionError("missing SQL identifier capture")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table!r})").fetchall()
    return any(str(row[1]).casefold() == column.casefold() for row in rows)


def _add_column_target(statement: str) -> tuple[str, str] | None:
    match = _ADD_COLUMN_RE.match(statement)
    if match is None:
        return None
    table = _identifier_from_match(
        match, "table_dq", "table_bt", "table_br", "table"
    )
    column = _identifier_from_match(
        match, "col_dq", "col_bt", "col_br", "col"
    )
    return table, column


def _is_duplicate_add_column(
    conn: sqlite3.Connection,
    statement: str,
    exc: sqlite3.OperationalError,
) -> bool:
    if "duplicate column name" not in str(exc).casefold():
        return False
    target = _add_column_target(statement)
    if target is None:
        return False
    table, column = target
    return _column_exists(conn, table, column)


def _execute_migration_script(conn: sqlite3.Connection, ddl: str) -> None:
    for statement in _iter_sql_statements(ddl):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if _is_duplicate_add_column(conn, statement, exc):
                continue
            target = _add_column_target(statement)
            if "no such table" in str(exc).casefold() and target is not None:
                table, column = target
                raise sqlite3.OperationalError(
                    f"migration cannot add column {column!r}: "
                    f"required table {table!r} does not exist"
                ) from exc
            raise


_OWNERSHIP_COLUMNS = {
    "owner_principal",
    "origin_channel",
    "origin_domain",
    "visibility",
    "provenance",
}

_ATOM_PROVENANCE_COLUMNS = {"integrity", "origin_trigger", "origin_ref"}


def _validate_ownership_schema(
    conn: sqlite3.Connection,
    *,
    stamped_version: int,
    target_version: int,
) -> None:
    required_tables = (
        ("atoms", 7),
        ("sessions", 7),
        ("observations_metadata", 7),
        ("triples", 7),
        ("world_state", 8),
    )
    missing: list[str] = []
    for table, introduced_in in required_tables:
        if target_version < introduced_in:
            continue
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table!r})").fetchall()
        }
        if not columns:
            missing.append(f"{table} (table missing)")
            continue
        missing.extend(
            f"{table}.{column}" for column in sorted(_OWNERSHIP_COLUMNS - columns)
        )

    if target_version >= 10:
        atom_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info('atoms')").fetchall()
        }
        missing.extend(
            f"atoms.{column}"
            for column in sorted(_ATOM_PROVENANCE_COLUMNS - atom_columns)
        )

    if missing:
        raise RuntimeError(
            f"schema_version reports version {stamped_version}, but the "
            f"ownership schema required by version {target_version} is incomplete: "
            + ", ".join(missing)
        )


def apply_pending_migrations(
    conn: sqlite3.Connection,
    *,
    fresh: bool,
    target_version: int = CURRENT_SCHEMA_VERSION,
    migrations: dict[int, str] | None = None,
    detector: Callable[[sqlite3.Connection], int] | None = None,
) -> None:
    """Apply any pending schema migrations and stamp the version row.

    First run on a fresh DB stamps *target_version* after the greenfield
    ``schema.sql`` script has run. Subsequent opens on an existing DB
    check the table; if the current version is older than
    *target_version*, every missing migration in *migrations* is
    applied in order. Tolerates the pre-migration era (DBs that were
    created before this table was populated): treats them as version 1
    and stamps if absent.

    *target_version* and *migrations* default to the module-level
    :data:`CURRENT_SCHEMA_VERSION` / :data:`MIGRATIONS`. Tests can pass
    custom values to exercise migration logic without touching the
    real registry.

    *detector* is the schema-version introspection callable used in
    the "empty schema_version table + not fresh" scenario.  Defaults
    to :func:`detect_schema_version`. ``SagaStore._apply_pending_migrations``
    passes a bound method so a test patching the detector on an
    instance is honored.
    """
    if migrations is None:
        migrations = MIGRATIONS
    if detector is None:
        detector = detect_schema_version

    applied: set[int] = set()
    try:
        for (v,) in conn.execute(
            "SELECT version FROM schema_version"
        ).fetchall():
            applied.add(int(v))
    except sqlite3.OperationalError:
        # schema.sql guarantees the table exists, but a pre-1.0
        # DB might be missing it. Create it lazily then stamp.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )

    if not applied:
        # Two scenarios land here:
        #
        #   A) Fresh DB created via ``schema.sql`` — tables are at
        #      ``target_version`` shape; just stamp + return.
        #
        #   B) Existing DB with an empty ``schema_version`` table —
        #      either a true pre-migration-era DB (tables at some
        #      historic shape), or a mid-init retry case where
        #      ``schema.sql`` already ran but the stamp INSERT
        #      never landed (e.g., the migration step raised
        #      mid-init and ``_ensure_conn`` closed the partial
        #      connection — see
        #      ``test_ensure_conn_does_not_cache_half_initialized_connection``).
        #
        # Distinguishing A from B requires PRAGMA introspection of
        # the actual table shape; :func:`detect_schema_version` does
        # that. After detection, we stamp the inferred version's
        # baselines so the subsequent migrations loop only runs
        # changes beyond what's already there.
        now = datetime.now(tz=timezone.utc).isoformat()
        if fresh:
            # Scenario A: schema.sql just ran — DB is at target.
            conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at) VALUES (?, ?)",
                (target_version, now),
            )
            conn.commit()
            return

        # Scenario B: introspect to figure out where we actually are.
        inferred = detector(conn)
        for v in range(1, inferred + 1):
            conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at) VALUES (?, ?)",
                (v, now),
            )
        conn.commit()
        applied = set(range(1, inferred + 1))
        # Else fall through to the migrations loop below, which
        # will apply versions max(applied)+1 .. target_version.

    stamped_version = max(applied)
    if stamped_version >= target_version:
        # A stamp from another lineage or a manually edited/restored DB is
        # not proof that the security-sensitive ownership schema exists.
        _validate_ownership_schema(
            conn,
            stamped_version=stamped_version,
            target_version=target_version,
        )
        return

    for version, ddl in sorted(migrations.items()):
        if version <= max(applied):
            continue
        if version > target_version:
            break
        # PRAGMA foreign_keys=ON/OFF inside migration scripts is a no-op
        # once the per-migration transaction has started.
        # ``executescript`` would issue an implicit COMMIT before the
        # script and leave a process-crash window where DDL is durable
        # but the schema_version stamp is not. Execute statements inside
        # our own per-migration transaction instead, so the DDL and stamp
        # commit at the same boundary. Table-restructuring migrations
        # (CREATE + COPY + DROP + RENAME) still need FK=OFF during the
        # intermediate state where both old and new tables exist.
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            _execute_migration_script(conn, ddl)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (?, ?)",
                (version, datetime.now(tz=timezone.utc).isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
