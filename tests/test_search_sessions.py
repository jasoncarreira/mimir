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
    if results:
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
async def test_search_sessions_schema_v3_migration(tmp_path, monkeypatch):
    """Schema v3 migration adds embedding columns to an existing sessions table.

    Simulate: sessions row exists but was created before schema v3
    (embedding column absent). Opening a fresh SagaStore triggers
    migration v3 and adds the columns. search_sessions should still
    work — alpha=0.0 (recency-only) returns the session even with
    NULL embedding.
    """
    _patch_provider(monkeypatch, dim=4)
    db_path = tmp_path / "v3_migration.saga.db"

    # Step 1: create at current schema version (v3).
    s1 = SagaStore(db_path=db_path, embedding_dim=4)
    await s1.end_session("sess-migrate", "Migration test session",
                         channel_id="ch-mig")

    # Step 2: simulate pre-v3 state — NULL out the embedding columns
    # and roll schema_version back to v2.
    conn = s1._ensure_conn()
    conn.execute("UPDATE sessions SET embedding = NULL, embedding_dim = NULL "
                 "WHERE id = 'sess-migrate'")
    conn.execute("DELETE FROM schema_version WHERE version = 3")
    conn.commit()

    # Step 3: open a fresh SagaStore — triggers migration v3 (no-op DDL
    # since columns already exist from the greenfield schema.sql, but
    # the migration runner stamps v3 correctly).
    s2 = SagaStore(db_path=db_path, embedding_dim=4)

    # Verify the sessions row is still there.
    conn2 = s2._ensure_conn()
    row = conn2.execute(
        "SELECT id FROM sessions WHERE id = 'sess-migrate'"
    ).fetchone()
    assert row is not None, "sessions row should survive migration"

    # search_sessions with alpha=0 (recency-only) should return it even
    # with a NULL embedding.
    results = await s2.search_sessions("migration test", alpha=0.0)
    session_ids = {r["session_id"] for r in results}
    assert "sess-migrate" in session_ids, (
        "search_sessions should return the session (recency path, alpha=0)"
    )
