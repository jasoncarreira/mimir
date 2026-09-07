"""Embedding health parity and process-level vector-drop diagnostics."""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from mimir.saga.embedding_status import embedding_status
from mimir.saga.vector_index import FAISS_AVAILABLE, VectorIndex


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE atoms (id TEXT PRIMARY KEY, tombstoned INTEGER);
        CREATE TABLE embeddings (atom_id TEXT, vec BLOB, dim INTEGER);
        CREATE TABLE sessions (id TEXT, embedding BLOB, embedding_dim INTEGER);
    """)
    yield db
    db.close()


@pytest.mark.parametrize("kind", ["atoms", "sessions"])
@pytest.mark.parametrize("dim,blob,excluded", [
    (2, bytes(8), 0), (3, bytes(12), 1),
    (2, b"bad", 1), (None, bytes(8), 1), (2, None, 1),
])
def test_health_exclusions_independently(conn, kind, dim, blob, excluded, monkeypatch):
    # Diagnostics have a separate process-lifetime test below; this test owns
    # only health/build parity and must not consume another test's warning.
    monkeypatch.setattr("mimir.saga.vector_index._warn_drops", lambda *args: None)
    if kind == "atoms":
        conn.execute("INSERT INTO atoms VALUES ('candidate', 0)")
        conn.execute("INSERT INTO embeddings VALUES ('candidate', ?, ?)", (blob, dim))
        # Neither tombstoned, missing, nor orphan embeddings are candidates.
        conn.execute("INSERT INTO atoms VALUES ('dead', 1), ('missing', 0)")
        conn.execute("INSERT INTO embeddings VALUES ('dead', X'00', 9), ('orphan', X'00', 9)")
    else:
        conn.execute("INSERT INTO sessions VALUES ('candidate', ?, ?)", (blob, dim))
        if blob is None:
            excluded = 0
    health = embedding_status(conn, 2)
    expected_dims = {} if kind == "sessions" and blob is None else {
        str(dim) if dim is not None else "unknown": 1,
    }
    assert health[kind] == {"dimensions": expected_dims, "excluded_from_recall": excluded}
    assert health["status"] == ("degraded" if excluded else "healthy")
    if FAISS_AVAILABLE:
        index = VectorIndex(dimension=2)
        build = index.build_from_db if kind == "atoms" else index.build_from_sessions
        build(conn)
        assert index.dimension_mismatch_count == excluded
        assert index.total_vectors == (1 if blob is not None and not excluded else 0)
        conn.execute("DELETE FROM embeddings")
        conn.execute("DELETE FROM sessions")
        build(conn)
        assert index.dimension_mismatch_count == 0
        assert index.total_vectors == 0


@pytest.mark.skipif(not FAISS_AVAILABLE, reason="requires FAISS")
def test_drop_warnings_once_per_kind_across_instances():
    # A child process owns the entire warning lifetime, independent of suite order.
    subprocess.run([sys.executable, "-c", '''
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from mimir.saga.vector_index import VectorIndex

records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logger = logging.getLogger("mimir.saga.vector_index")
logger.addHandler(Capture())
logger.setLevel(logging.WARNING)
def build(_):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE atoms (id TEXT, tombstoned INTEGER);
        CREATE TABLE embeddings (atom_id TEXT, vec BLOB, dim INTEGER);
        CREATE TABLE sessions (id TEXT, embedding BLOB, embedding_dim INTEGER);
        INSERT INTO atoms VALUES ('a', 0);
        INSERT INTO embeddings VALUES ('a', X'00', 3);
        INSERT INTO sessions VALUES ('s', X'00', 3);
    """)
    for _ in range(3):
        index = VectorIndex(dimension=2)
        index.build_from_db(conn)
        assert index.dimension_mismatch_count == 1
        index.build_from_sessions(conn)
        assert index.dimension_mismatch_count == 1
    conn.close()
with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(build, range(16)))
assert len(records) == 2, records
assert sum("atom vectors" in r for r in records) == 1
assert sum("session vectors" in r for r in records) == 1
'''], check=True, timeout=60)
