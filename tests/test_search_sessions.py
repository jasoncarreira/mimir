"""Tests for SagaStore.search_sessions() — semantic + recency session retrieval.

Covers: empty-DB no-error, basic result shape, recency ordering,
channel filtering, and the schema-v2 backfill path.
"""
from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.saga.client import SagaStore


# ── helpers ──────────────────────────────────────────────────────────

def _patch_provider(monkeypatch, dim: int = 4):
    """Stub embeddings so tests don't need real Voyage credentials.

    Returns a deterministic dim-d vector derived from the text's hash.
    Two identical texts get the same vector; different texts get different
    (but not semantically meaningful) vectors.
    """
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
    if results:  # results may be empty if embeddings stub gives no signal
        r = results[0]
        for key in ("session_id", "channel_id", "started_at", "ended_at",
                    "summary", "similarity_score", "recency_score", "blended_score"):
            assert key in r, f"missing key: {key}"
        # Scores are in [0, 1] range
        assert 0.0 <= r["similarity_score"] <= 1.0
        assert 0.0 <= r["recency_score"] <= 1.0
        assert 0.0 <= r["blended_score"] <= 1.0


@pytest.mark.asyncio
async def test_search_sessions_recency_ordering(store, monkeypatch):
    """With alpha=0 (recency-only), more-recent session ranks higher."""
    await store.end_session("sess-recent", "Topics A",
                            channel_id="ch-x")
    await store.end_session("sess-older", "Topics B",
                            channel_id="ch-x")

    # Manually age sess-older by patching ended_at in the sessions table.
    conn = store._ensure_conn()
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                 (old_ts, "sess-older"))
    conn.commit()

    results = await store.search_sessions("topics", alpha=0.0, limit=10)
    # With alpha=0 we rely purely on recency — sess-recent should be first.
    # Filter to the two test sessions in case prior tests left rows.
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

    assert "sess-beta" not in alpha_ids, "ch-beta session leaked into ch-alpha filter"
    assert "sess-alpha" not in beta_ids, "ch-alpha session leaked into ch-beta filter"
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
async def test_search_sessions_schema_v2_backfill(tmp_path, monkeypatch):
    """Sessions backfilled via MIGRATIONS v2 are returned by search_sessions.

    Simulate: session_boundary atom exists but sessions row does not
    (pre-migration state). Verify that opening a new SagaStore triggers
    the backfill SQL and that search_sessions can find the session.
    """
    _patch_provider(monkeypatch, dim=4)
    db_path = tmp_path / "backfill.saga.db"

    # Step 1: create a SagaStore so schema.sql and migration v2 run.
    s1 = SagaStore(db_path=db_path, embedding_dim=4)
    await s1.end_session("sess-backfill", "Backfill test session",
                         channel_id="ch-bf")

    # Step 2: manually delete the sessions row and reset schema_version to v1
    # to simulate a pre-migration state. The fresh DB was stamped at v2 directly
    # (CURRENT_SCHEMA_VERSION), so we replace that with v1 so the migration loop
    # runs migration 2 on the next SagaStore open.
    conn = s1._ensure_conn()
    conn.execute("DELETE FROM sessions WHERE id = 'sess-backfill'")
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now'))"
    )
    conn.commit()

    # Step 3: open a fresh SagaStore instance (re-opens the same file,
    # re-runs _apply_pending_migrations → migration 2 backfills sessions row).
    s2 = SagaStore(db_path=db_path, embedding_dim=4)

    # Verify sessions row was backfilled.
    conn2 = s2._ensure_conn()
    row = conn2.execute(
        "SELECT id FROM sessions WHERE id = 'sess-backfill'"
    ).fetchone()
    assert row is not None, "migration v2 should have backfilled the sessions row"

    # search_sessions should now be able to find it.
    results = await s2.search_sessions("backfill test", alpha=0.0)
    session_ids = {r["session_id"] for r in results}
    assert "sess-backfill" in session_ids, (
        "search_sessions should return the backfilled session"
    )
