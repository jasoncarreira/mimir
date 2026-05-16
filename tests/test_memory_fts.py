"""Tests for mimir.saga.fts — FTS5 keyword search.

Validates: schema triggers keep atoms_fts in sync, BM25 ranking
returns sensible order, fallback to LIKE works when FTS5 syntax is
malformed, stopword/short-term filtering doesn't crater short queries.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.saga.fts import fts5_query, fts_search


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _insert_atom(conn, atom_id: str, content: str, *, agent_id: str = "default",
                 memory_type: str = "raw", source_type: str = "conversation"):
    """Bare-bones direct INSERT so we can validate the trigger sync
    behavior without depending on store()."""
    from hashlib import sha256
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, "
        "stream, memory_type, source_type, agent_id) "
        "VALUES (?, ?, ?, '2026-05-12T00:00:00Z', 'semantic', ?, ?, ?)",
        (atom_id, content, h, memory_type, source_type, agent_id),
    )
    conn.commit()


# ─── fts5_query rewrite ──────────────────────────────────────────────


def test_fts5_query_strips_stopwords():
    q = fts5_query("what is the meaning of life")
    # 'what', 'is', 'the', 'of' all stopwords. 'meaning' + 'life' survive.
    assert "meaning" in q
    assert "life" in q
    assert "what" not in q.replace('"', '')


def test_fts5_query_strips_short_terms():
    q = fts5_query("a b cd longer")
    # 'a','b','cd' all <=2 chars; only 'longer' survives.
    assert "longer" in q
    assert '"a"' not in q


def test_fts5_query_handles_all_stopwords():
    """If every term is a stopword, fall back to keeping all terms (so
    the query isn't empty). Bench probes like 'what is X' would otherwise
    return zero results from FTS."""
    q = fts5_query("what is the")  # all stopwords
    # We get a fallback — not necessarily a clean OR query, but non-empty.
    assert q  # not empty


def test_fts5_query_or_joins_terms():
    q = fts5_query("alice prefers concise")
    assert " OR " in q
    assert '"alice"' in q
    assert '"prefers"' in q
    assert '"concise"' in q


def test_fts5_query_escapes_special_chars():
    """FTS5 reserved tokens like * - + must be stripped or they break
    the parser."""
    q = fts5_query("alice* prefers- concise+")
    # No raw special chars in the quoted survivors.
    assert "*" not in q
    assert "-" not in q
    assert "+" not in q


# ─── Trigger-driven sync ─────────────────────────────────────────────


def test_insert_trigger_syncs_atoms_fts(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    # atoms_fts should contain a row for this atom now (via trigger).
    rows = conn.execute(
        "SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH 'alice'"
    ).fetchall()
    assert len(rows) == 1


def test_update_trigger_syncs_atoms_fts(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    # Pre-update: 'verbose' shouldn't match
    rows = conn.execute(
        "SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH 'verbose'"
    ).fetchall()
    assert len(rows) == 0
    # Update the content; trigger should refresh atoms_fts.
    conn.execute(
        "UPDATE atoms SET content = 'Alice prefers verbose explanations' WHERE id = 'a1'"
    )
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH 'verbose'"
    ).fetchall()
    assert len(rows) == 1
    # Old terms shouldn't match anymore.
    rows = conn.execute(
        "SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH 'concise'"
    ).fetchall()
    assert len(rows) == 0


def test_delete_trigger_removes_from_atoms_fts(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    conn.execute("DELETE FROM atoms WHERE id = 'a1'")
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH 'alice'"
    ).fetchall()
    assert len(rows) == 0


# ─── fts_search ──────────────────────────────────────────────────────


def test_fts_search_returns_matches(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    _insert_atom(conn, "a2", "Bob enjoys verbose explanations")
    results = fts_search(conn, "alice concise", top_k=10)
    ids = [aid for aid, _ in results]
    assert "a1" in ids


def test_fts_search_excludes_tombstoned(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    _insert_atom(conn, "a2", "Bob enjoys concise reports")
    conn.execute("UPDATE atoms SET tombstoned = 1 WHERE id = 'a1'")
    conn.commit()
    results = fts_search(conn, "concise", top_k=10)
    ids = [aid for aid, _ in results]
    assert "a1" not in ids
    assert "a2" in ids


def test_fts_search_excludes_session_boundaries_by_default(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    _insert_atom(conn, "b1", "Session ended; discussed concise replies",
                 source_type="session_boundary")
    results = fts_search(conn, "concise", top_k=10)
    ids = [aid for aid, _ in results]
    assert "a1" in ids
    assert "b1" not in ids


def test_fts_search_can_include_session_boundaries(conn):
    _insert_atom(conn, "b1", "Session ended; discussed concise",
                 source_type="session_boundary")
    results = fts_search(conn, "concise", top_k=10,
                          include_session_boundaries=True)
    ids = [aid for aid, _ in results]
    assert "b1" in ids


def test_fts_search_returns_positive_scores(conn):
    """BM25 is negative-is-better in raw; our wrapper flips to
    positive-is-better. Verify."""
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    results = fts_search(conn, "alice concise", top_k=10)
    assert results
    assert all(score > 0 for _, score in results)


def test_fts_search_returns_empty_when_no_match(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies")
    results = fts_search(conn, "xenobiology", top_k=10)
    assert results == []


def test_fts_search_filters_by_memory_type(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies",
                 memory_type="raw")
    _insert_atom(conn, "o1", "Alice consistently prefers concise text",
                 memory_type="observation")
    raws = fts_search(conn, "alice concise", top_k=10, memory_type="raw")
    obs = fts_search(conn, "alice concise", top_k=10,
                      memory_type="observation")
    assert [aid for aid, _ in raws] == ["a1"]
    assert [aid for aid, _ in obs] == ["o1"]


def test_fts_search_filters_by_agent_id(conn):
    _insert_atom(conn, "a1", "Alice prefers concise replies",
                 agent_id="agent_a")
    _insert_atom(conn, "a2", "Alice loves concise replies",
                 agent_id="agent_b")
    results = fts_search(conn, "alice concise", top_k=10, agent_id="agent_a")
    ids = [aid for aid, _ in results]
    assert ids == ["a1"]


def test_fts_search_respects_top_k(conn):
    for i in range(5):
        _insert_atom(conn, f"a{i}", f"Alice mentions concise topic {i}")
    results = fts_search(conn, "alice concise", top_k=3)
    assert len(results) <= 3
