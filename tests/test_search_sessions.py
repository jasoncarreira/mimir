"""Tests for SagaStore.search_sessions() — semantic + recency session retrieval.

Covers: empty-DB no-error, basic result shape, recency ordering,
channel filtering, limit capping, and the schema-v3 migration path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mimir.saga.client import SagaStore


# ── helpers ──────────────────────────────────────────────────────────


def _patch_provider(monkeypatch, dim: int = 4):
    """Stub embeddings so tests don't need real Voyage credentials."""

    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float((h + i) % 17) / 17.0 for i in range(dim)]

        def dimensions(self):
            return dim

    def fake_get_provider():
        return _StubProvider()

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): f"stub-{dim}d",
            }.get((section, key), default)

        return cfg

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", fake_get_provider)
    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.fixture
def store(tmp_path, monkeypatch):
    _patch_provider(monkeypatch)
    s = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)
    yield s


# ── tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_sessions_empty_db(store):
    """search_sessions on a fresh DB returns [] without error."""
    results = await store.search_sessions("anything")
    assert results == []


@pytest.mark.asyncio
async def test_search_sessions_basic_result_shape(store):
    """After ending two sessions, search returns dicts with required keys."""
    await store.end_session("sess-a", "Python and asyncio patterns",
                            channel_id="discord-test")
    await store.end_session("sess-b", "Cooking pasta and risotto",
                            channel_id="discord-test")

    results = await store.search_sessions("programming patterns")
    assert isinstance(results, list)
    assert len(results) >= 1, (
        "expected at least one result after two end_session calls; "
        "got [] — likely a silent regression in search_sessions"
    )
    r = results[0]
    for key in ("session_id", "channel_id", "started_at", "ended_at",
                "summary", "similarity_score", "recency_score", "blended_score"):
        assert key in r, f"missing key: {key}"
    assert 0.0 <= r["similarity_score"] <= 1.0
    assert 0.0 <= r["recency_score"] <= 1.0
    assert 0.0 <= r["blended_score"] <= 1.0


@pytest.mark.asyncio
async def test_search_sessions_recency_ordering(store):
    """With alpha=0 (recency-only), the more-recent session ranks higher."""
    await store.end_session("sess-recent", "Topics A", channel_id="ch-x")
    await store.end_session("sess-older", "Topics B", channel_id="ch-x")

    # Age sess-older by patching ended_at in the sessions table.
    conn = store._ensure_conn()
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                 (old_ts, "sess-older"))
    conn.commit()

    results = await store.search_sessions("topics", alpha=0.0, limit=10)
    test_results = [r for r in results
                    if r["session_id"] in ("sess-recent", "sess-older")]
    assert len(test_results) == 2, "both test sessions must be returned"
    assert test_results[0]["session_id"] == "sess-recent", (
        "more-recent session should rank higher with alpha=0"
    )
    assert test_results[0]["recency_score"] > test_results[1]["recency_score"]


@pytest.mark.asyncio
async def test_search_sessions_channel_filter(store):
    """channel_id filter restricts results to a single channel."""
    await store.end_session("sess-alpha", "Alpha channel session",
                            channel_id="ch-alpha")
    await store.end_session("sess-beta", "Beta channel session",
                            channel_id="ch-beta")

    alpha_results = await store.search_sessions("session", channel_id="ch-alpha")
    beta_results = await store.search_sessions("session", channel_id="ch-beta")

    alpha_ids = {r["session_id"] for r in alpha_results}
    beta_ids = {r["session_id"] for r in beta_results}

    assert "sess-beta" not in alpha_ids
    assert "sess-alpha" not in beta_ids
    assert "sess-alpha" in alpha_ids
    assert "sess-beta" in beta_ids


@pytest.mark.asyncio
async def test_search_sessions_limit(store):
    """limit parameter caps the number of results."""
    for i in range(5):
        await store.end_session(f"sess-{i}", f"Session {i} summary",
                                channel_id="ch-limit")

    results = await store.search_sessions("session summary", limit=3)
    assert len(results) <= 3


@pytest.mark.asyncio
async def test_search_sessions_schema_migration_adds_embedding_columns(
    tmp_path, monkeypatch
):
    """Embedding-columns ALTER TABLE migration genuinely runs on a pre-migration DB.

    Builds a DB without the embedding columns by: (1) letting SagaStore create
    the full schema, (2) using SQLite's table-swap pattern to recreate ``sessions``
    without the embedding columns, (3) removing the embedding-migration version
    stamp so SagaStore re-applies it on next open.  Verifies via
    ``PRAGMA table_info(sessions)`` that the columns are present after migration —
    a broken ALTER TABLE would not be caught by the old NULL-and-rollback approach.
    """
    _patch_provider(monkeypatch, dim=4)
    db_path = tmp_path / "pre_emb.saga.db"

    # Step 1: Initialize via SagaStore so all tables and full schema exist.
    s1 = SagaStore(db_path=db_path, embedding_dim=4)
    await s1.end_session("sess-migrate", "Migration test session",
                         channel_id="ch-mig")

    # Step 2: Drop the embedding columns by recreating the sessions table without
    # them.  SQLite doesn't support DROP COLUMN before 3.35, so use the
    # portable rename-recreate pattern.
    conn = s1._ensure_conn()
    conn.executescript("""
        BEGIN;
        CREATE TABLE sessions_pre_emb (
            id               TEXT PRIMARY KEY,
            channel_id       TEXT,
            started_at       TEXT NOT NULL,
            ended_at         TEXT,
            summary          TEXT,
            reflected_at     TEXT,
            topics_discussed TEXT NOT NULL DEFAULT '[]',
            decisions_made   TEXT NOT NULL DEFAULT '[]',
            unfinished       TEXT NOT NULL DEFAULT '[]',
            emotional_state  TEXT,
            closed_since     TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO sessions_pre_emb
            SELECT id, channel_id, started_at, ended_at, summary, reflected_at,
                   topics_discussed, decisions_made, unfinished,
                   emotional_state, closed_since
            FROM sessions;
        DROP TABLE sessions;
        ALTER TABLE sessions_pre_emb RENAME TO sessions;
        COMMIT;
    """)

    # Step 3: Stamp the schema_version table at v3 (post-structured-fields,
    # pre-embedding-columns) so _apply_pending_migrations runs migration 4
    # (the embedding-columns ALTER TABLE) and any later migrations the
    # framework has registered. Hardcoded to 3 because this test
    # specifically verifies migration 4's ALTER TABLE shape — bumping
    # CURRENT_SCHEMA_VERSION shouldn't change what this test exercises.
    # (Removing all stamps would trigger the fresh-DB stamp-and-return
    # path that skips DDL entirely.)
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (3, ?)",
        ("2000-01-01T00:00:00+00:00",),
    )
    conn.commit()

    # Verify the columns are genuinely absent before the migration runs.
    pre_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(sessions)"
    ).fetchall()}
    assert "embedding" not in pre_cols, \
        "test setup failed: 'embedding' column still present before migration"
    assert "embedding_dim" not in pre_cols, \
        "test setup failed: 'embedding_dim' column still present before migration"

    # Step 4: Open a fresh SagaStore — triggers the embedding-columns migration.
    s2 = SagaStore(db_path=db_path, embedding_dim=4)
    conn2 = s2._ensure_conn()

    # Step 5: Verify ALTER TABLE added the columns.
    post_cols = {row[1] for row in conn2.execute(
        "PRAGMA table_info(sessions)"
    ).fetchall()}
    assert "embedding" in post_cols, \
        "migration should have added 'embedding' column via ALTER TABLE"
    assert "embedding_dim" in post_cols, \
        "migration should have added 'embedding_dim' column via ALTER TABLE"

    # Step 6: Verify the original session row survived and is searchable
    # (recency path, alpha=0 avoids needing an embedding).
    results = await s2.search_sessions("migration test", alpha=0.0)
    session_ids = {r["session_id"] for r in results}
    assert "sess-migrate" in session_ids, \
        "search_sessions should return the session via the recency path after migration"


@pytest.mark.asyncio
async def test_migration_v5_clears_atom_topics_before_deleting_boundaries(
    tmp_path, monkeypatch
):
    """Regression: migration v5 must delete from EVERY FK-referencing
    table before ``DELETE FROM atoms WHERE source_type='session_boundary'``.

    The original v5 missed ``atom_topics`` (and the defensive set
    ``triples`` / ``access_log`` / ``corrections``). On a production DB
    with 1756 atom_topics rows pointing at boundary atoms, the migration
    raised ``sqlite3.IntegrityError: FOREIGN KEY constraint failed`` and
    rolled back. Combined with ``_ensure_conn``'s half-init caching
    (separate fix in this PR), the failure was silent — saga calls kept
    working but boundary atoms never got cleaned.

    This test reproduces by inserting a boundary atom with rows in every
    FK-referencing table, stamping the DB at v4, then opening a fresh
    SagaStore and asserting migration v5 lands successfully.
    """
    _patch_provider(monkeypatch, dim=4)
    db_path = tmp_path / "boundary_cleanup.saga.db"

    # Step 1: SagaStore creates the full schema.
    s1 = SagaStore(db_path=db_path, embedding_dim=4)
    conn = s1._ensure_conn()

    # Step 2: Insert a boundary atom plus a row in every FK-referencing
    # table — the migration's deletes must clear all of these before
    # the final ``DELETE FROM atoms``.
    now = "2026-05-19T00:00:00+00:00"
    conn.executescript(f"""
        BEGIN;
        INSERT INTO atoms (id, content, content_hash, source_type, agent_id, created_at)
            VALUES ('boundary-1', 'session ended', 'h1', 'session_boundary', 'default', '{now}');
        INSERT INTO atoms (id, content, content_hash, source_type, agent_id, created_at)
            VALUES ('raw-1',      'normal atom',  'h2', 'conversation',     'default', '{now}');
        INSERT INTO sessions (id, channel_id, started_at, ended_at, summary, reflected_at)
            VALUES ('sess-1', 'ch-1', '{now}', '{now}', 'existing summary', '{now}');

        -- One row per FK-referencing table that points at the boundary.
        INSERT INTO atom_access_summary (atom_id, recent_ts_json, recent_weights_json)
            VALUES ('boundary-1', '[]', '[]');
        INSERT INTO access_events (atom_id, ts, source)
            VALUES ('boundary-1', '{now}', 'store');
        INSERT INTO embeddings (atom_id, provider, model, dim, vec, embedded_at)
            VALUES ('boundary-1', 'stub', 'stub-4d', 4, x'00000000000000000000000000000000', '{now}');
        INSERT INTO atom_topics (atom_id, topic) VALUES ('boundary-1', 'test_topic');
        INSERT INTO triples (id, subject, predicate, object, source_atom_id, created_at)
            VALUES ('t1', 's', 'p', 'o', 'boundary-1', '{now}');
        INSERT INTO atom_relations (source_id, target_id, relation_type, created_at)
            VALUES ('raw-1', 'boundary-1', 'session_member', '{now}');
        COMMIT;
    """)

    # Step 3: Stamp at v4 so opening a fresh store reruns migration 5.
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (4, ?)",
        ("2000-01-01T00:00:00+00:00",),
    )
    conn.commit()
    s1._conn = None  # force re-open path

    # Step 4: Open a fresh SagaStore — migration v5 should land cleanly.
    s2 = SagaStore(db_path=db_path, embedding_dim=4)
    conn2 = s2._ensure_conn()  # would raise on the original buggy v5

    # Step 5: Verify the boundary atom + its FK refs are all gone.
    counts = {}
    for table, where in [
        ("atoms",                "id = 'boundary-1'"),
        ("atom_access_summary",  "atom_id = 'boundary-1'"),
        ("access_events",        "atom_id = 'boundary-1'"),
        ("embeddings",           "atom_id = 'boundary-1'"),
        ("atom_topics",          "atom_id = 'boundary-1'"),
        ("triples",              "source_atom_id = 'boundary-1'"),
        ("atom_relations",       "source_id = 'boundary-1' OR target_id = 'boundary-1'"),
    ]:
        counts[table] = conn2.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}"
        ).fetchone()[0]
    assert counts == {t: 0 for t in counts}, (
        f"every FK-referencing table should be empty for the boundary atom; "
        f"residuals: {counts}"
    )

    # Step 6: Verify migration was actually stamped at v5.
    v = conn2.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert v == 5, f"schema_version should be 5 post-migration, got {v}"

    # Step 7: Non-boundary atom survives untouched.
    raw_exists = conn2.execute(
        "SELECT COUNT(*) FROM atoms WHERE id = 'raw-1'"
    ).fetchone()[0]
    assert raw_exists == 1, "non-boundary atoms must survive the migration"


def test_ensure_conn_does_not_cache_half_initialized_connection(tmp_path):
    """Regression: if ``_apply_pending_migrations`` raises, ``_ensure_conn``
    must NOT leave ``self._conn`` populated. Otherwise the next call
    short-circuits to a cached half-initialized connection without
    retrying the migration — the failure becomes silent.

    Reproduces by monkey-patching ``_apply_pending_migrations`` to raise,
    asserting the first ``_ensure_conn`` re-raises, asserting ``self._conn``
    is reset to None, and asserting the second ``_ensure_conn`` re-raises
    again instead of returning a stale connection.
    """
    import sqlite3
    db_path = tmp_path / "halfinit.saga.db"
    store = SagaStore(db_path=db_path)

    boom_calls: list[int] = []
    orig_apply = store._apply_pending_migrations

    def _boom(conn, *, fresh):
        boom_calls.append(1)
        raise sqlite3.IntegrityError("simulated FK failure during migration")

    store._apply_pending_migrations = _boom  # type: ignore[method-assign]

    # First call: must raise the migration error.
    with pytest.raises(sqlite3.IntegrityError):
        store._ensure_conn()
    assert store._conn is None, (
        "after a failed migration _ensure_conn must NOT cache the "
        "half-initialized connection — caching would silently mask the "
        "failure on subsequent calls"
    )

    # Second call: must re-attempt and raise again. (Previously it would
    # have returned the cached connection and the migration would never
    # land.)
    with pytest.raises(sqlite3.IntegrityError):
        store._ensure_conn()
    assert len(boom_calls) == 2, (
        "_apply_pending_migrations must be called on every _ensure_conn "
        "until it succeeds; got " + str(len(boom_calls)) + " calls"
    )

    # After restoring the real apply, _ensure_conn must succeed.
    store._apply_pending_migrations = orig_apply  # type: ignore[method-assign]
    conn = store._ensure_conn()
    assert conn is not None
    assert store._conn is conn
