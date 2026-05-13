"""Tests for the P42 triples + temporal world model module."""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.memory.triples import (
    detect_contradictions,
    get_current_value,
    get_history,
    make_triple_id,
    parse_triples,
    resolve_contradictions_to_supersedes,
    retrieve_by_entity,
    store_triples,
    triple_augment_search,
)


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "memory" / "schema.sql"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _stub_embed(vec_template: list[float]):
    """Return an embed_fn that always returns the same vector. Used
    for triple cosine-search tests where we want deterministic
    similarity values."""
    dim = len(vec_template)
    vec_bytes = struct.pack(f"{dim}f", *vec_template)
    def _embed(text: str):
        return vec_bytes, "stub", "stub", dim
    return _embed


# ─── make_triple_id ──────────────────────────────────────────────────


def test_make_triple_id_normalizes_case():
    a = make_triple_id("Alice", "lives_in", "Boston")
    b = make_triple_id("alice", "LIVES_IN", "boston")
    assert a == b


def test_make_triple_id_distinguishes_distinct_claims():
    a = make_triple_id("Alice", "lives_in", "Boston")
    b = make_triple_id("Alice", "lives_in", "SF")
    assert a != b


# ─── parse_triples ───────────────────────────────────────────────────


def test_parse_triples_simple():
    out = parse_triples("""TRIPLES:
(Alice, prefers, concise_replies)
(Alice, lives_in, Boston, valid_from=2024-01-15)
""")
    assert len(out) == 2
    assert out[0] == {"subject": "Alice", "predicate": "prefers", "object": "concise_replies"}
    assert out[1]["valid_from"] == "2024-01-15"


def test_parse_triples_handles_section_extraction():
    raw = """OBSERVATION:
Alice consistently prefers concise replies.

TRIPLES:
(Alice, prefers, concise_replies)

CONTRADICTIONS:
NONE
"""
    out = parse_triples(raw)
    assert len(out) == 1
    assert out[0]["subject"] == "Alice"


def test_parse_triples_returns_empty_on_none():
    out = parse_triples("TRIPLES:\nNONE")
    assert out == []


def test_parse_triples_normalizes_predicate_to_snake_case():
    out = parse_triples("TRIPLES:\n(User, LIVES IN, Boston)")
    assert out[0]["predicate"] == "lives_in"


def test_parse_triples_rejects_oversized_subject_or_object():
    huge = "A" * 50
    out = parse_triples(f"TRIPLES:\n({huge}, lives_in, Boston)\n(Alice, lives_in, {huge})")
    # Both rows should be rejected (subj > 30 / obj > 30).
    assert out == []


def test_parse_triples_accepts_valid_from_and_until():
    out = parse_triples(
        "TRIPLES:\n(Alice, employed_at, Acme, "
        "valid_from=2023-01-01, valid_until=2024-06-30)"
    )
    assert out[0]["valid_from"] == "2023-01-01"
    assert out[0]["valid_until"] == "2024-06-30"


def test_parse_triples_handles_empty_input():
    assert parse_triples("") == []
    assert parse_triples(None) == []


# ─── store_triples ───────────────────────────────────────────────────


def _seed_atom(conn, atom_id: str, content: str):
    from hashlib import sha256
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at) "
        "VALUES (?, ?, ?, '2026-05-13T00:00:00Z')",
        (atom_id, content, h),
    )
    conn.commit()


def test_store_triples_basic(conn):
    _seed_atom(conn, "obs1", "observation")
    triples = [
        {"subject": "Alice", "predicate": "prefers", "object": "concise_replies"},
    ]
    inserted = store_triples(conn, triples, source_atom_id="obs1", embed_fn=None)
    assert len(inserted) == 1
    row = conn.execute("SELECT subject, predicate, object FROM triples").fetchone()
    assert row == ("Alice", "prefers", "concise_replies")


def test_store_triples_dedupes_by_content(conn):
    _seed_atom(conn, "obs1", "first observation")
    _seed_atom(conn, "obs2", "second observation")
    triples = [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ]
    # Insert from two atoms — same content, should land once.
    store_triples(conn, triples, source_atom_id="obs1")
    re_insert = store_triples(conn, triples, source_atom_id="obs2")
    assert re_insert == []  # already present
    count = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
    assert count == 1


def test_store_triples_with_embedding(conn):
    _seed_atom(conn, "obs1", "obs")
    embed_fn = _stub_embed([1.0, 0.0, 0.0, 0.0])
    triples = [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ]
    store_triples(conn, triples, source_atom_id="obs1", embed_fn=embed_fn)
    row = conn.execute(
        "SELECT embedding, embedding_dim FROM triples"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == 4


# ─── World state ─────────────────────────────────────────────────────


def test_world_state_initial_insert(conn):
    _seed_atom(conn, "obs1", "obs")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "lives_in", "object": "Boston",
         "valid_from": "2023-01-01"},
    ], source_atom_id="obs1")
    fact = get_current_value(conn, "Alice", "lives_in")
    assert fact is not None
    assert fact.value == "Boston"
    assert fact.is_current is True


def test_world_state_end_dates_prior_on_change(conn):
    _seed_atom(conn, "obs1", "obs1")
    _seed_atom(conn, "obs2", "obs2")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "lives_in", "object": "Boston",
         "valid_from": "2023-01-01"},
    ], source_atom_id="obs1")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "lives_in", "object": "SF",
         "valid_from": "2024-06-01"},
    ], source_atom_id="obs2")
    history = get_history(conn, "Alice", "lives_in")
    assert len(history) == 2
    # Oldest first — Boston should be first and now closed.
    assert history[0].value == "Boston"
    assert history[0].is_current is False
    assert history[0].valid_until == "2024-06-01"
    # Newest is SF and still current.
    assert history[1].value == "SF"
    assert history[1].is_current is True


def test_world_state_no_op_on_reassertion(conn):
    _seed_atom(conn, "obs1", "obs1")
    _seed_atom(conn, "obs2", "obs2")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "lives_in", "object": "Boston",
         "valid_from": "2023-01-01"},
    ], source_atom_id="obs1")
    # Re-assert the same fact from a different atom — no new row.
    store_triples(conn, [
        {"subject": "Alice", "predicate": "lives_in", "object": "Boston",
         "valid_from": "2023-06-01"},
    ], source_atom_id="obs2")
    history = get_history(conn, "Alice", "lives_in")
    # The dedupe at triple-storage level (same content hash) means the
    # second triple isn't even inserted; world_state has 1 entry.
    assert len(history) == 1


# ─── Triple-augment search ───────────────────────────────────────────


def test_triple_augment_search_returns_atom_ids(conn):
    _seed_atom(conn, "obs1", "obs")
    embed_fn = _stub_embed([1.0, 0.0, 0.0, 0.0])
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ], source_atom_id="obs1", embed_fn=embed_fn)
    results = triple_augment_search(conn, [1.0, 0.0, 0.0, 0.0], top_k=5)
    assert results
    assert results[0][0] == "obs1"
    # Cosine of identical vectors is 1.0.
    assert results[0][1] == pytest.approx(1.0)


def test_triple_augment_search_skips_tombstoned(conn):
    _seed_atom(conn, "obs1", "obs")
    embed_fn = _stub_embed([1.0, 0.0, 0.0, 0.0])
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ], source_atom_id="obs1", embed_fn=embed_fn)
    conn.execute("UPDATE triples SET tombstoned = 1")
    conn.commit()
    results = triple_augment_search(conn, [1.0, 0.0, 0.0, 0.0], top_k=5)
    assert results == []


def test_triple_augment_search_skips_no_embedding(conn):
    _seed_atom(conn, "obs1", "obs")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
    ], source_atom_id="obs1", embed_fn=None)
    results = triple_augment_search(conn, [1.0, 0.0, 0.0, 0.0], top_k=5)
    assert results == []


# ─── retrieve_by_entity ──────────────────────────────────────────────


def test_retrieve_by_entity_substring(conn):
    _seed_atom(conn, "obs1", "obs")
    store_triples(conn, [
        {"subject": "Alice", "predicate": "prefers", "object": "concise"},
        {"subject": "Bob", "predicate": "enjoys", "object": "verbose"},
    ], source_atom_id="obs1")
    results = retrieve_by_entity(conn, "Alice")
    assert any(r["subject"] == "Alice" for r in results)
    assert not any(r["subject"] == "Bob" for r in results)


# ─── Contradiction resolution ────────────────────────────────────────


def test_resolve_contradictions_to_supersedes_writes_edges(conn):
    _seed_atom(conn, "old", "old fact")
    _seed_atom(conn, "new", "newer correction")
    # Backdate the older atom.
    conn.execute("UPDATE atoms SET created_at = '2025-01-01' WHERE id = 'old'")
    conn.execute("UPDATE atoms SET created_at = '2026-01-01' WHERE id = 'new'")
    # Insert a contradicts relation.
    conn.execute(
        "INSERT INTO atom_relations (source_id, target_id, relation_type, "
        "confidence, created_at) "
        "VALUES ('old', 'new', 'contradicts', 1.0, '2026-01-02')"
    )
    conn.commit()
    n = resolve_contradictions_to_supersedes(conn)
    assert n == 1
    row = conn.execute(
        "SELECT source_id, target_id FROM atom_relations "
        "WHERE relation_type = 'supersedes'"
    ).fetchone()
    # The newer atom supersedes the older.
    assert row == ("new", "old")


def test_resolve_contradictions_does_not_leak_transaction(conn):
    """Regression: before the fix, the function did its INSERTs in
    Python's sqlite3 implicit-transaction mode and never committed,
    leaving subsequent BEGIN IMMEDIATE callers to crash with
    'cannot start a transaction within a transaction'. The v4 bench
    hit this end-to-end on Q1 right after consolidate completed."""
    _seed_atom(conn, "old", "old fact")
    _seed_atom(conn, "new", "newer correction")
    conn.execute("UPDATE atoms SET created_at = '2025-01-01' WHERE id = 'old'")
    conn.execute("UPDATE atoms SET created_at = '2026-01-01' WHERE id = 'new'")
    conn.execute(
        "INSERT INTO atom_relations (source_id, target_id, relation_type, "
        "confidence, created_at) "
        "VALUES ('old', 'new', 'contradicts', 1.0, '2026-01-02')"
    )
    conn.commit()
    resolve_contradictions_to_supersedes(conn)
    # Now BEGIN IMMEDIATE should succeed (no dangling implicit txn).
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO atoms (id, content, content_hash, created_at) "
                 "VALUES ('z', 'z', 'zhz', '2026-01-03')")
    conn.commit()
    row = conn.execute("SELECT id FROM atoms WHERE id = 'z'").fetchone()
    assert row is not None


def test_resolve_contradictions_no_pairs_no_op(conn):
    """When there are no contradicts edges, the function should not
    open a transaction at all (so subsequent BEGIN IMMEDIATE works
    immediately)."""
    resolve_contradictions_to_supersedes(conn)
    conn.execute("BEGIN IMMEDIATE")  # would crash if a txn was open
    conn.commit()


def test_resolve_contradictions_is_idempotent(conn):
    _seed_atom(conn, "old", "old fact")
    _seed_atom(conn, "new", "newer correction")
    conn.execute("UPDATE atoms SET created_at = '2025-01-01' WHERE id = 'old'")
    conn.execute("UPDATE atoms SET created_at = '2026-01-01' WHERE id = 'new'")
    conn.execute(
        "INSERT INTO atom_relations (source_id, target_id, relation_type, "
        "confidence, created_at) "
        "VALUES ('old', 'new', 'contradicts', 1.0, '2026-01-02')"
    )
    conn.commit()
    n1 = resolve_contradictions_to_supersedes(conn)
    n2 = resolve_contradictions_to_supersedes(conn)
    assert n1 == 1
    assert n2 == 0  # already in place; INSERT OR IGNORE skips


# ─── parse_contradictions (in synthesize) ────────────────────────────


def test_parse_contradictions_basic():
    from mimir.memory.synthesize import _parse_contradictions
    raw = """OBSERVATION:
Some observation.

TRIPLES:
NONE

CONTRADICTIONS:
3 vs 7: User said they live in Boston in atom 3 but SF in atom 7
5 vs 9: Conflicting commute durations
"""
    out = _parse_contradictions(raw)
    assert len(out) == 2
    assert out[0]["atom_index_a"] == 3
    assert out[0]["atom_index_b"] == 7
    assert "Boston" in out[0]["summary"]


def test_parse_contradictions_handles_none():
    from mimir.memory.synthesize import _parse_contradictions
    out = _parse_contradictions("CONTRADICTIONS:\nNONE")
    assert out == []


def test_parse_contradictions_returns_empty_when_no_section():
    from mimir.memory.synthesize import _parse_contradictions
    out = _parse_contradictions("OBSERVATION: just an observation.")
    assert out == []
