"""Tests for mimir.saga.vector_index — FAISS-backed ANN.

If faiss-cpu isn't available these tests skip — the recall fallback
path is exercised by test_memory_tier2.py instead.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import numpy as np
import pytest

from mimir.saga.vector_index import FAISS_AVAILABLE, VectorIndex


pytestmark = pytest.mark.skipif(
    not FAISS_AVAILABLE,
    reason="faiss-cpu not installed; FAISS path tested via fallback elsewhere",
)


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _vec_bytes(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _insert_atom_with_embedding(
    conn: sqlite3.Connection,
    atom_id: str,
    content: str,
    vec: list[float],
    *,
    tombstoned: int = 0,
):
    from hashlib import sha256
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, tombstoned) "
        "VALUES (?, ?, ?, '2026-05-12T00:00:00Z', ?)",
        (atom_id, content, h, tombstoned),
    )
    conn.execute(
        "INSERT INTO embeddings (atom_id, provider, model, dim, vec, embedded_at) "
        "VALUES (?, 'test', 'test-3d', ?, ?, '2026-05-12T00:00:00Z')",
        (atom_id, len(vec), _vec_bytes(vec)),
    )
    conn.commit()


# ─── build_from_db ───────────────────────────────────────────────────


def test_build_from_db_empty(conn):
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    assert idx.built
    assert idx.total_vectors == 0


def test_build_from_db_loads_all_live_atoms(conn):
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    _insert_atom_with_embedding(conn, "a2", "beta", [0.0, 1.0, 0.0])
    _insert_atom_with_embedding(conn, "a3", "gamma", [0.0, 0.0, 1.0])
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    assert idx.total_vectors == 3


def test_build_from_db_skips_tombstoned(conn):
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    _insert_atom_with_embedding(conn, "a2", "beta", [0.0, 1.0, 0.0], tombstoned=1)
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    assert idx.total_vectors == 1
    results = idx.search([1.0, 0.0, 0.0], top_k=10)
    ids = [aid for aid, _ in results]
    assert "a2" not in ids


def test_build_from_db_skips_dim_mismatch(conn):
    """An atom embedded with a different dim (provider switch) gets
    skipped silently — re-embedding pass would re-add it."""
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    _insert_atom_with_embedding(conn, "a2", "wrong", [1.0, 0.0])  # 2d
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    assert idx.total_vectors == 1


# ─── search ──────────────────────────────────────────────────────────


def test_search_returns_top_k(conn):
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    _insert_atom_with_embedding(conn, "a2", "beta", [0.9, 0.1, 0.0])
    _insert_atom_with_embedding(conn, "a3", "gamma", [0.0, 0.0, 1.0])
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    results = idx.search([1.0, 0.0, 0.0], top_k=2)
    ids = [aid for aid, _ in results]
    assert ids[0] == "a1"
    assert "a3" not in ids  # least similar; should not be in top-2


def test_search_ordering_by_similarity(conn):
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    _insert_atom_with_embedding(conn, "a2", "beta", [0.5, 0.5, 0.0])
    _insert_atom_with_embedding(conn, "a3", "gamma", [0.0, 1.0, 0.0])
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    results = idx.search([1.0, 0.0, 0.0], top_k=3)
    # Sorted desc by similarity.
    sims = [s for _, s in results]
    assert sims == sorted(sims, reverse=True)


def test_search_empty_index_returns_empty(conn):
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)  # empty DB
    results = idx.search([1.0, 0.0, 0.0], top_k=5)
    assert results == []


def test_search_unbuilt_index_returns_empty():
    idx = VectorIndex(dimension=3)
    # Don't call build_from_db
    results = idx.search([1.0, 0.0, 0.0], top_k=5)
    assert results == []


# ─── incremental add ─────────────────────────────────────────────────


def test_add_grows_index(conn):
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    assert idx.total_vectors == 1
    idx.add("a2", _vec_bytes([0.0, 1.0, 0.0]))
    assert idx.total_vectors == 2
    results = idx.search([0.0, 1.0, 0.0], top_k=2)
    ids = [aid for aid, _ in results]
    assert "a2" in ids


def test_add_before_build_is_noop(conn):
    """If build_from_db hasn't run, add() does nothing — the lazy build
    will pick up the new atom from disk."""
    idx = VectorIndex(dimension=3)
    idx.add("a2", _vec_bytes([0.0, 1.0, 0.0]))
    assert idx.total_vectors == 0


# ─── remove (soft) ───────────────────────────────────────────────────


def test_remove_filters_from_search(conn):
    _insert_atom_with_embedding(conn, "a1", "alpha", [1.0, 0.0, 0.0])
    _insert_atom_with_embedding(conn, "a2", "alpha2", [0.99, 0.01, 0.0])
    idx = VectorIndex(dimension=3)
    idx.build_from_db(conn)
    idx.remove("a1")
    results = idx.search([1.0, 0.0, 0.0], top_k=5)
    ids = [aid for aid, _ in results]
    assert "a1" not in ids
    assert "a2" in ids
