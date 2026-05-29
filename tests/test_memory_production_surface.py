"""Regression tests for the three production gaps closed during the
parity audit: triple surfacing in the query response, confidence_tier
assignment on each atom, and the min_confidence_tier filter.

Each test pins one contract the production prompt-rendering / credit-
attribution path depends on.
"""
from __future__ import annotations

import sqlite3
import struct
from hashlib import sha256
from pathlib import Path

import pytest


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _seed_atom(conn, atom_id: str, content: str):
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at) "
        "VALUES (?, ?, ?, '2026-05-13T00:00:00Z')",
        (atom_id, content, h),
    )
    conn.commit()


# ─── _tier_for_similarity ────────────────────────────────────────────


def test_tier_for_similarity_buckets():
    from mimir.saga.recall import _tier_for_similarity
    assert _tier_for_similarity(0.50) == "high"
    assert _tier_for_similarity(0.45) == "high"      # boundary
    assert _tier_for_similarity(0.44) == "medium"
    assert _tier_for_similarity(0.30) == "medium"    # boundary
    assert _tier_for_similarity(0.29) == "low"
    assert _tier_for_similarity(0.10) == "low"       # boundary
    assert _tier_for_similarity(0.09) == "none"
    assert _tier_for_similarity(0.0) == "none"


def test_tier_passes_min_tier():
    from mimir.saga.recall import _passes_min_tier
    # No filter accepts everything
    assert _passes_min_tier("none", None) is True
    assert _passes_min_tier("high", None) is True
    # Strictness: min="medium" → keep medium + high
    assert _passes_min_tier("high", "medium") is True
    assert _passes_min_tier("medium", "medium") is True
    assert _passes_min_tier("low", "medium") is False
    assert _passes_min_tier("none", "medium") is False
    # min="low" → keep low + medium + high
    assert _passes_min_tier("low", "low") is True
    assert _passes_min_tier("none", "low") is False
    # min="high" → keep only high
    assert _passes_min_tier("medium", "high") is False
    assert _passes_min_tier("high", "high") is True


# ─── top_triples_with_payload ────────────────────────────────────────


def test_top_triples_with_payload_returns_rich_data(conn):
    from mimir.saga.triples import store_triples, top_triples_with_payload

    _seed_atom(conn, "obs1", "obs")
    # Use a stub embed_fn that returns the same vector for every triple
    # so all triples score cosine=1.0 against a matching query embedding.
    vec_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    def embed(text):
        return vec_bytes, "stub", "stub", 4

    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise",
         "valid_from": "2024-01-15"},
        {"subject": "Bob", "predicate": "enjoys", "object": "verbose"},
    ], source_atom_id="obs1", embed_fn=embed)

    results = top_triples_with_payload(conn, [1.0, 0.0, 0.0, 0.0], top_n=10)
    assert len(results) == 2
    # Rich shape — full triple data, not collapsed by source_atom_id.
    alice = next(r for r in results if r["subject"] == "Alice")
    bob = next(r for r in results if r["subject"] == "Bob")
    assert alice["predicate"] == "prefers"
    assert alice["object"] == "concise"
    assert alice["valid_from"] == "2024-01-15"
    assert alice["source_atom_id"] == "obs1"
    assert alice["_cosine"] == pytest.approx(1.0)
    assert bob["predicate"] == "enjoys"
    # Both share source_atom_id — top_triples_with_payload doesn't
    # collapse, distinguishes from triple_augment_search.
    assert bob["source_atom_id"] == "obs1"


def test_top_triples_with_payload_skips_no_embedding(conn):
    from mimir.saga.triples import store_triples, top_triples_with_payload
    _seed_atom(conn, "obs1", "obs")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ], source_atom_id="obs1", embed_fn=None)
    results = top_triples_with_payload(conn, [1.0, 0.0, 0.0, 0.0], top_n=10)
    assert results == []


def test_top_triples_with_payload_respects_top_n(conn):
    from mimir.saga.triples import store_triples, top_triples_with_payload
    _seed_atom(conn, "obs1", "obs")
    vec_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    def embed(text):
        return vec_bytes, "stub", "stub", 4
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
        {"subject": "Bob", "predicate": "enjoys", "object": "verbose"},
        {"subject": "Carol", "predicate": "likes", "object": "art"},
    ], source_atom_id="obs1", embed_fn=embed)
    results = top_triples_with_payload(conn, [1.0, 0.0, 0.0, 0.0], top_n=2)
    assert len(results) == 2


def test_top_triples_with_payload_excludes_expired_triples(conn):
    """Triples with valid_until <= reference_date are excluded (chainlink #257)."""
    from datetime import datetime, timezone
    from mimir.saga.triples import store_triples, top_triples_with_payload
    _seed_atom(conn, "obs1", "atom about current job")
    _seed_atom(conn, "obs2", "atom about former job")
    vec_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    def embed(text):
        return vec_bytes, "stub", "stub", 4
    # Live triple — no valid_until
    store_triples(conn, [
        {"subject": "Alice", "predicate": "works_at", "object": "CurrentCo"},
    ], source_atom_id="obs1", embed_fn=embed)
    # Expired triple
    store_triples(conn, [
        {"subject": "Alice", "predicate": "works_at", "object": "OldCo",
         "valid_until": "2020-01-01T00:00:00+00:00"},
    ], source_atom_id="obs2", embed_fn=embed)
    ref = datetime(2026, 1, 1, tzinfo=timezone.utc)
    results = top_triples_with_payload(conn, [1.0, 0.0, 0.0, 0.0], top_n=10,
                                       reference_date=ref)
    objects = {r["object"] for r in results}
    assert "CurrentCo" in objects   # live triple surfaces
    assert "OldCo" not in objects   # expired triple excluded


# ─── End-to-end via SagaStore ─────────────────────────────────────


def _patch_provider(monkeypatch, *, dim=4):
    """Stub embedding provider returning a fixed 4d vector."""
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            return [1.0, 0.0, 0.0, 0.0]
        def dimensions(self):
            return 4
    monkeypatch.setattr("mimir.saga.embeddings.get_provider", lambda: _StubProvider())
    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub-4d",
            }.get((section, key), default)
        return cfg
    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.mark.asyncio
async def test_query_surfaces_triples_in_response(monkeypatch, tmp_path):
    """When ``include_triples_in_response=True`` (default) and the DB
    has triples with embeddings, ``query`` returns them in the
    response's ``triples`` field — populating the shape mimir's prod
    sagatools.py:_format_saga_payload expects."""
    _patch_provider(monkeypatch)
    from mimir.saga.client import SagaStore
    from mimir.saga.triples import store_triples

    db = tmp_path / "mimir.saga.db"
    client = SagaStore(db_path=db, embedding_dim=4)

    # Seed a raw and an extracted triple.
    r = await client.store("Alice prefers concise replies")
    conn = client._ensure_conn()
    vec_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    conn.execute("BEGIN IMMEDIATE")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise",
         "valid_from": "2024-01-15"},
    ], source_atom_id=r["atom_id"],
       embed_fn=lambda _t: (vec_bytes, "stub", "stub", 4))
    conn.commit()

    result = await client.query("alice", top_k=5)
    assert isinstance(result.get("triples"), list)
    assert len(result["triples"]) == 1
    t = result["triples"][0]
    assert t["subject"] == "Alice"
    assert t["predicate"] == "prefers"
    assert t["object"] == "concise"
    assert t["valid_from"] == "2024-01-15"
    assert t["source_atom_id"] == r["atom_id"]
    # Internal _cosine field is stripped from the agent-facing wire shape.
    assert "_cosine" not in t


@pytest.mark.asyncio
async def test_query_with_triples_disabled_returns_empty_list(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    from mimir.saga.client import SagaStore
    from mimir.saga.triples import store_triples

    db = tmp_path / "mimir.saga.db"
    client = SagaStore(
        db_path=db, embedding_dim=4,
        include_triples_in_response=False,
    )

    r = await client.store("Alice prefers concise replies")
    conn = client._ensure_conn()
    vec_bytes = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    conn.execute("BEGIN IMMEDIATE")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ], source_atom_id=r["atom_id"],
       embed_fn=lambda _t: (vec_bytes, "stub", "stub", 4))
    conn.commit()

    result = await client.query("alice", top_k=5)
    assert result["triples"] == []


@pytest.mark.asyncio
async def test_atoms_carry_confidence_tier(monkeypatch, tmp_path):
    """Both ``confidence_tier`` and ``_confidence_tier`` are populated
    on every atom returned by ``query`` — what mimir's
    sagatools.py:_atom_label reads."""
    _patch_provider(monkeypatch)
    from mimir.saga.client import SagaStore

    db = tmp_path / "mimir.saga.db"
    client = SagaStore(db_path=db, embedding_dim=4)
    await client.store("Alice prefers concise replies")
    result = await client.query("alice", top_k=5)
    atoms = result["raws"] + result["observations"]
    assert atoms
    for a in atoms:
        assert "confidence_tier" in a
        assert "_confidence_tier" in a
        assert a["confidence_tier"] in ("none", "low", "medium", "high")
        assert a["confidence_tier"] == a["_confidence_tier"]


@pytest.mark.asyncio
async def test_min_confidence_tier_filter_drops_weak_atoms(monkeypatch, tmp_path):
    """``min_confidence_tier="medium"`` should drop atoms whose
    similarity is below 0.30. The stub provider returns the same
    vector for all queries → atoms with identical content embed to
    the same vector → cosine = 1.0 → tier = high → kept. Atoms with
    different content embed to a different vector pattern — we
    construct a case where the per-atom similarity differs."""
    # A 4d stub provider that returns different vectors based on text —
    # so we can construct atoms with intentionally low query-similarity.
    class _ContentProvider:
        def embed(self, text, *, input_type="passage"):
            if "alice" in text.lower():
                return [1.0, 0.0, 0.0, 0.0]
            return [0.0, 1.0, 0.0, 0.0]   # orthogonal → cosine 0.0
        def dimensions(self):
            return 4
    monkeypatch.setattr("mimir.saga.embeddings.get_provider", lambda: _ContentProvider())
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda section, key, default=None: {
            ("embedding", "max_input_chars"): 2000,
            ("embedding", "provider"): "stub",
            ("embedding", "model"): "stub-4d",
        }.get((section, key), default),
    )

    from mimir.saga.client import SagaStore
    db = tmp_path / "mimir.saga.db"
    client = SagaStore(db_path=db, embedding_dim=4)
    # "alice" content → embeds to [1,0,0,0] → matches "alice" query at cosine=1.0 → high
    r_strong = await client.store("Alice prefers concise replies")
    # "bob" content → embeds to [0,1,0,0] → matches "alice" query at cosine=0.0 → none
    r_weak = await client.store("Bob enjoys verbose explanations")

    # Without filter: both atoms surface (BM25 may rank both).
    bare = await client.query("alice", top_k=5)
    bare_ids = {a["id"] for a in bare["raws"] + bare["observations"]}
    assert r_strong["atom_id"] in bare_ids

    # With min="medium": only atoms whose cosine ≥ 0.30 survive. The
    # Bob atom's cosine to "alice" is 0.0 → tier none → dropped.
    filtered = await client.query("alice", top_k=5, min_confidence_tier="medium")
    filtered_ids = {a["id"] for a in filtered["raws"] + filtered["observations"]}
    assert r_strong["atom_id"] in filtered_ids
    assert r_weak["atom_id"] not in filtered_ids
