"""Smoke tests for mimir.saga.client.SagaStore — the
SagaClient-compatible facade.

Validates that the public API methods all run without error against
a fresh in-memory DB. Does NOT validate retrieval quality (FAISS
adapter is stubbed in v1; recall falls through to FTS-only for
candidates). Quality validation comes during the LongMemEval bench
port in tier 5 v2.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.saga.client import SagaStore


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "mimir.saga.db"
    c = SagaStore(db_path=db_path)
    yield c


def _patch_provider(monkeypatch):
    """Replace saga.embeddings.get_provider with a deterministic stub
    so tests don't need real voyage credentials.

    Returns a 4-dim "embedding" derived from text hash. Not useful for
    real retrieval; sufficient to exercise the embed → store → recall
    pipeline.
    """
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float(h % 7), float(h % 11), float(h % 13), float(h % 17)]

        def dimensions(self):
            return 4

    def fake_get_provider():
        return _StubProvider()

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", fake_get_provider)
    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.mark.asyncio
async def test_client_health_returns_true_on_fresh_db(client, monkeypatch):
    _patch_provider(monkeypatch)
    ok = await client.health()
    assert ok is True


@pytest.mark.asyncio
async def test_client_store_returns_atom_id(client, monkeypatch):
    _patch_provider(monkeypatch)
    result = await client.store(
        "Alice prefers concise replies", stream="semantic",
    )
    assert result["stored"] is True
    assert "atom_id" in result


@pytest.mark.asyncio
async def test_client_store_dedupes(client, monkeypatch):
    _patch_provider(monkeypatch)
    r1 = await client.store("duplicate content")
    r2 = await client.store("duplicate content")
    assert r1["atom_id"] == r2["atom_id"]
    assert r2["stored"] is False


@pytest.mark.asyncio
async def test_client_query_returns_two_tier_shape(client, monkeypatch):
    _patch_provider(monkeypatch)
    await client.store("Alice prefers concise replies")
    result = await client.query("Alice", top_k=5)
    # Saga-compatible shape.
    assert "observations" in result
    assert "raws" in result
    assert "items_returned" in result
    assert "two_tier" in result


@pytest.mark.asyncio
async def test_client_feedback_records_event(client, monkeypatch):
    _patch_provider(monkeypatch)
    r = await client.store("test atom")
    result = await client.feedback(
        [r["atom_id"]], "agent reply text", feedback="positive",
    )
    assert result["marked"] == 1
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_client_end_session_creates_boundary(client, monkeypatch):
    _patch_provider(monkeypatch)
    result = await client.end_session(
        "s1", "we discussed PR review",
        topics_discussed=["pr-review"],
    )
    assert result["boundary_atom_id"] is not None
    assert result["boundary_created"] is True


@pytest.mark.asyncio
async def test_client_end_session_idempotent(client, monkeypatch):
    _patch_provider(monkeypatch)
    r1 = await client.end_session("s1", "first call")
    r2 = await client.end_session("s1", "second call")
    assert r1["boundary_atom_id"] == r2["boundary_atom_id"]
    assert r1["boundary_created"] is True
    assert r2["boundary_created"] is False


@pytest.mark.asyncio
async def test_client_recent_session_boundaries(client, monkeypatch):
    _patch_provider(monkeypatch)
    await client.end_session("s1", "first")
    await client.end_session("s2", "second")
    boundaries = await client.recent_session_boundaries(count=10)
    assert len(boundaries) == 2


@pytest.mark.asyncio
async def test_client_forget_dry_run(client, monkeypatch):
    _patch_provider(monkeypatch)
    await client.store("stale atom")
    result = await client.forget(dry_run=True)
    assert result["dry_run"] is True
    # Returns count + preview ids without writing.


@pytest.mark.asyncio
async def test_client_most_retrieved_atoms(client, monkeypatch):
    """The mapping to access_events for "what got retrieved most"."""
    _patch_provider(monkeypatch)
    r = await client.store("atom to retrieve")
    # Fire a few retrievals.
    for _ in range(3):
        await client.query("atom to retrieve")
    top = await client.most_retrieved_atoms(days=7, count=5)
    assert isinstance(top, list)
