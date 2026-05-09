"""Tests for the ``transactional()`` context manager (CR#16).

The helper wraps multi-statement saga writes in BEGIN IMMEDIATE /
COMMIT (or ROLLBACK on exception) so partial-success bug classes like
"atom row committed but atoms_fts INSERT failed silently" can't
happen. These tests pin the rollback contract using a fresh DB; the
production write paths (store_atom, store_triple, etc.) carry the
behavior end-to-end and are covered by their own tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """A clean saga DB that doesn't leak into other tests."""
    import saga.core as core
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(core, "DB_PATH", db_path)
    # Clear the module-level migration-applied set so this DB gets the
    # full schema applied on first get_db() call.
    monkeypatch.setattr(core, "_migrations_done", set())
    return db_path


def test_normal_path_commits(fresh_db: Path):
    """No exception → COMMIT. Rows are visible after the with block."""
    from saga.core import transactional, get_db

    with transactional() as conn:
        conn.execute(
            "INSERT INTO atoms (id, content, content_hash, created_at, "
            "embedding, topics, metadata, encoding_confidence) "
            "VALUES (?, ?, ?, datetime('now'), ?, '[]', '{}', 0.7)",
            ("a1", "hello", "h1", b""),
        )

    # Re-open and verify visible.
    conn = get_db()
    row = conn.execute(
        "SELECT id, content FROM atoms WHERE id = 'a1'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["content"] == "hello"


def test_exception_rolls_back(fresh_db: Path):
    """An exception inside the block triggers ROLLBACK. None of the
    INSERTs from the block stick — pins the atomicity guarantee."""
    from saga.core import transactional, get_db

    with pytest.raises(RuntimeError, match="boom"):
        with transactional() as conn:
            conn.execute(
                "INSERT INTO atoms (id, content, content_hash, created_at, "
                "embedding, topics, metadata, encoding_confidence) "
                "VALUES (?, ?, ?, datetime('now'), ?, '[]', '{}', 0.7)",
                ("a1", "first", "h1", b""),
            )
            conn.execute(
                "INSERT INTO atoms (id, content, content_hash, created_at, "
                "embedding, topics, metadata, encoding_confidence) "
                "VALUES (?, ?, ?, datetime('now'), ?, '[]', '{}', 0.7)",
                ("a2", "second", "h2", b""),
            )
            raise RuntimeError("boom")

    conn = get_db()
    rows = conn.execute("SELECT id FROM atoms WHERE id IN ('a1', 'a2')").fetchall()
    conn.close()
    assert rows == [], (
        "Both INSERTs should have rolled back when the exception fired"
    )


def test_sql_failure_inside_block_rolls_back_prior_writes(fresh_db: Path):
    """The integrity-bug case: an INSERT fails mid-batch → all earlier
    INSERTs in the same batch roll back too. Pins the exact CR#16 fix
    shape (the original ``store_atom`` had ``try/except: pass`` around
    the FTS5 write that would let the atom row stick while the FTS row
    silently dropped). Uses a foreign-key violation as the failure
    trigger — atom_topics requires the atom_id to exist in atoms; with
    foreign_keys=ON, the FK violation raises mid-batch."""
    from saga.core import transactional, get_db

    with pytest.raises(sqlite3.IntegrityError):
        with transactional() as conn:
            # First INSERT: legal.
            conn.execute(
                "INSERT INTO atoms (id, content, content_hash, created_at, "
                "embedding, topics, metadata, encoding_confidence) "
                "VALUES (?, ?, ?, datetime('now'), ?, '[]', '{}', 0.7)",
                ("a1", "good", "h1", b""),
            )
            # Second INSERT: FK violation against atoms (atom_id "ghost"
            # does not exist).
            conn.execute(
                "INSERT INTO atom_topics (atom_id, topic) VALUES (?, ?)",
                ("ghost", "topic"),
            )

    # The legal first INSERT should ALSO have rolled back — that's the
    # correctness fix.
    conn = get_db()
    rows = conn.execute("SELECT id FROM atoms WHERE id = 'a1'").fetchall()
    conn.close()
    assert rows == [], (
        "The legal-but-not-yet-committed first INSERT must roll back "
        "when a later statement in the same transaction fails — that "
        "was the bug: prior code committed atoms first then ignored "
        "atoms_fts failures, leaving the atom unsearchable."
    )


def test_passing_existing_conn_does_not_close_it(fresh_db: Path):
    """When the caller passes a connection (rare — most call sites
    open their own), ``transactional`` must NOT close it on exit. The
    caller manages the lifecycle."""
    from saga.core import transactional, get_db

    conn = get_db()
    try:
        with transactional(conn) as c:
            assert c is conn
            c.execute(
                "INSERT INTO atoms (id, content, content_hash, created_at, "
                "embedding, topics, metadata, encoding_confidence) "
                "VALUES (?, ?, ?, datetime('now'), ?, '[]', '{}', 0.7)",
                ("a1", "x", "h1", b""),
            )

        # Connection should still be usable post-block.
        row = conn.execute("SELECT id FROM atoms WHERE id = 'a1'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_owned_conn_is_closed_even_on_exception(fresh_db: Path):
    """When transactional() opens its own conn, it must close on exit
    — including when the body raises. We verify by making thousands of
    failing transactions don't leak fds."""
    from saga.core import transactional

    # 200 failing transactions; if the helper leaked the conn each
    # time, fd exhaustion or sqlite locking would surface here.
    for _ in range(200):
        with pytest.raises(RuntimeError):
            with transactional() as conn:
                conn.execute(
                    "INSERT INTO atoms (id, content, content_hash, created_at, "
                    "embedding, topics, metadata, encoding_confidence) "
                    "VALUES (?, ?, ?, datetime('now'), ?, '[]', '{}', 0.7)",
                    ("a", "x", "h", b""),
                )
                raise RuntimeError("boom")
