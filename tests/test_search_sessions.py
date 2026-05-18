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

    # Step 3: Remove the embedding-migration version stamp and insert the
    # predecessor version so _apply_pending_migrations sees a non-empty
    # `applied` set with max(applied) < CURRENT_SCHEMA_VERSION and
    # actually runs the embedding-columns migration.
    # (Removing the only entry would leave `applied` empty, triggering the
    # "fresh DB" stamp-and-return path that skips all migration DDL.)
    emb_migration_version = SagaStore.CURRENT_SCHEMA_VERSION
    prev_version = emb_migration_version - 1
    conn.execute("DELETE FROM schema_version WHERE version = ?",
                 (emb_migration_version,))
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (prev_version, "2000-01-01T00:00:00+00:00"),
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
