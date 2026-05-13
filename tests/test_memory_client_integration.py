"""End-to-end integration tests for MemoryClient with real FAISS +
FTS5 wiring. Verifies that query() returns relevant atoms — not just
that the API contract is satisfied (covered by test_memory_client.py).

These probe the recall pipeline:
  store atoms → FAISS index built lazily on first query →
  query returns atoms that match by semantic (FAISS) or keyword (FTS5).
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from mimir.memory.client import MemoryClient
from mimir.memory.vector_index import FAISS_AVAILABLE


# Deterministic 4d "embedding" derived from text hash. Tests that need
# semantic-similarity ordering use this so the relevant atom embeds
# closer to its query than to noise atoms. Real bench uses a real
# provider; this is just for unit-level wiring.
class _StubProvider:
    def embed(self, text, *, input_type="passage"):
        # Bag-of-words–style: count occurrences of a few keywords. Two
        # atoms sharing keywords will land near each other in this 4d
        # space; an unrelated atom will not.
        text_l = text.lower()
        return [
            float(text_l.count("alice")),
            float(text_l.count("bob")),
            float(text_l.count("concise")),
            float(text_l.count("verbose")),
        ]

    def dimensions(self):
        return 4


def _patch_provider(monkeypatch):
    monkeypatch.setattr(
        "saga.embeddings.get_provider", lambda: _StubProvider(),
    )

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("saga.config.get_config", fake_get_config)


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "mimir.memory.db"
    c = MemoryClient(db_path=db, embedding_dim=4)
    yield c


# ─── End-to-end recall ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(
    not FAISS_AVAILABLE, reason="faiss-cpu not installed",
)
async def test_query_returns_semantically_similar_atom(client, monkeypatch):
    _patch_provider(monkeypatch)
    r1 = await client.store("Alice prefers concise replies")
    r2 = await client.store("Bob enjoys verbose explanations")
    # Query that embeds close to atom #1.
    result = await client.query("alice concise", top_k=5)
    ids = [a["id"] for a in result["raws"]]
    assert r1["atom_id"] in ids


@pytest.mark.asyncio
async def test_query_returns_keyword_match_via_fts(client, monkeypatch):
    """Even without FAISS, FTS5 should surface a keyword match."""
    _patch_provider(monkeypatch)
    r1 = await client.store("Alice prefers concise replies")
    await client.store("Bob enjoys verbose explanations")
    result = await client.query("concise", top_k=5)
    ids = [a["id"] for a in result["raws"]]
    assert r1["atom_id"] in ids


@pytest.mark.asyncio
async def test_store_then_query_in_same_session_session_boost(
    client, monkeypatch,
):
    """Atoms accessed in the same session get a small session_boost.
    Verify the bookkeeping path doesn't crash and the session-touched
    atom still ranks."""
    _patch_provider(monkeypatch)
    r1 = await client.store("Alice prefers concise replies")
    result = await client.query(
        "alice concise", top_k=5, session_id="session-1",
    )
    ids = [a["id"] for a in result["raws"]]
    assert r1["atom_id"] in ids


@pytest.mark.asyncio
@pytest.mark.skipif(
    not FAISS_AVAILABLE, reason="faiss-cpu not installed",
)
async def test_index_rebuild_picks_up_new_atoms_after_rebuild(
    client, monkeypatch,
):
    """After rebuild_index(), atoms stored before the rebuild remain
    queryable. Sanity-check for the bench harness's between-question
    rebuild pattern."""
    _patch_provider(monkeypatch)
    r1 = await client.store("Alice prefers concise replies")
    # Trigger initial build via query.
    await client.query("alice", top_k=3)
    # Rebuild.
    client.rebuild_index()
    # New query should still find the atom.
    result = await client.query("concise", top_k=5)
    ids = [a["id"] for a in result["raws"]]
    assert r1["atom_id"] in ids


@pytest.mark.asyncio
async def test_query_no_results_when_nothing_stored(client, monkeypatch):
    _patch_provider(monkeypatch)
    result = await client.query("anything", top_k=5)
    assert result["items_returned"] == 0


@pytest.mark.asyncio
async def test_tombstoned_atoms_dropped_from_recall(client, monkeypatch):
    _patch_provider(monkeypatch)
    r = await client.store("Alice prefers concise replies")
    # Tombstone the atom directly via forget().
    from mimir.memory.forget import forget as _forget
    conn = client._ensure_conn()
    _forget(conn, [r["atom_id"]], reason="test")
    result = await client.query("alice concise", top_k=5)
    ids = [a["id"] for a in result["raws"]]
    assert r["atom_id"] not in ids
