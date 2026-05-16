"""Tests for P12 synonym expansion in mimir.saga.fts.

Validates the expand_query_for_keyword behavior and confirms the
fts_search path picks up the synonyms argument.
"""
from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from mimir.saga.fts import (
    DEFAULT_LONGMEMEVAL_SYNONYMS,
    expand_query_for_keyword,
    fts_search,
)


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _insert(conn, atom_id: str, content: str):
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at) "
        "VALUES (?, ?, ?, '2026-05-12T00:00:00Z')",
        (atom_id, content, h),
    )
    conn.commit()


# ─── expand_query_for_keyword ────────────────────────────────────────


def test_expand_appends_matching_synonyms():
    out = expand_query_for_keyword(
        "What is my profession?",
        {"profession": ["job", "career", "work"]},
    )
    # Original query is preserved; synonyms appended.
    assert "profession" in out
    assert "job" in out
    assert "career" in out
    assert "work" in out


def test_expand_is_no_op_when_no_match():
    out = expand_query_for_keyword(
        "Tell me about quantum entanglement",
        {"profession": ["job"]},
    )
    assert out == "Tell me about quantum entanglement"


def test_expand_handles_none_synonyms():
    out = expand_query_for_keyword("anything", None)
    assert out == "anything"


def test_expand_handles_empty_synonyms():
    out = expand_query_for_keyword("anything", {})
    assert out == "anything"


def test_expand_case_insensitive_match():
    out = expand_query_for_keyword(
        "What is MY PROFESSION?",
        {"profession": ["job"]},
    )
    assert "job" in out


def test_expand_default_dict_covers_longmemeval_categories():
    """The bundled DEFAULT_LONGMEMEVAL_SYNONYMS should cover the
    canonical bench categories so out-of-box bench runs match saga's
    setup without per-call config."""
    expected_keys = {
        "profession", "home", "schedule", "family",
        "preference", "commute", "school",
    }
    assert expected_keys <= set(DEFAULT_LONGMEMEVAL_SYNONYMS.keys())


# ─── fts_search synonym integration ──────────────────────────────────


def test_fts_search_finds_atom_via_synonym(conn):
    """Without synonyms, a query for 'profession' shouldn't match an
    atom that uses 'job'. With synonyms, it should."""
    _insert(conn, "a1", "I work as a software engineer at the company")
    # No synonyms: 'profession' alone doesn't appear in the atom.
    bare = fts_search(conn, "profession", top_k=10, synonyms=None)
    assert bare == []
    # With synonyms: 'profession' expands to include 'work', which is
    # in the atom.
    expanded = fts_search(
        conn, "profession", top_k=10,
        synonyms={"profession": ["job", "career", "work"]},
    )
    ids = [aid for aid, _ in expanded]
    assert "a1" in ids


def test_fts_search_no_synonym_is_no_op(conn):
    """fts_search without synonyms behaves identically to a direct
    keyword search — synonym expansion is strict-additive."""
    _insert(conn, "a1", "Alice prefers concise replies")
    no_syn = fts_search(conn, "alice", top_k=10, synonyms=None)
    with_empty = fts_search(conn, "alice", top_k=10, synonyms={})
    assert [r[0] for r in no_syn] == [r[0] for r in with_empty]
