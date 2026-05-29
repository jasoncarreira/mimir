"""Tests for the P42 triples + temporal world model module."""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.saga.triples import (
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


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "saga" / "schema.sql"


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


def test_triple_augment_search_excludes_expired_triples(conn):
    """Triples whose valid_until <= reference_date are excluded (chainlink #257)."""
    from datetime import datetime, timezone
    _seed_atom(conn, "obs1", "atom about current employer")
    _seed_atom(conn, "obs2", "atom about past employer")
    embed_fn = _stub_embed([1.0, 0.0, 0.0, 0.0])
    # Live triple — no valid_until
    store_triples(conn, [
        {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
    ], source_atom_id="obs1", embed_fn=embed_fn)
    # Expired triple — valid_until in the past
    store_triples(conn, [
        {"subject": "Alice", "predicate": "works_at", "object": "OldCo",
         "valid_until": "2020-01-01T00:00:00+00:00"},
    ], source_atom_id="obs2", embed_fn=embed_fn)
    ref = datetime(2026, 1, 1, tzinfo=timezone.utc)
    results = triple_augment_search(conn, [1.0, 0.0, 0.0, 0.0], top_k=5,
                                    reference_date=ref)
    atom_ids = [r[0] for r in results]
    assert "obs1" in atom_ids   # live triple surfaces
    assert "obs2" not in atom_ids  # expired triple excluded


def test_triple_augment_search_includes_future_valid_until(conn):
    """Triples with valid_until in the future are included."""
    from datetime import datetime, timezone
    _seed_atom(conn, "obs1", "atom about future employer")
    embed_fn = _stub_embed([1.0, 0.0, 0.0, 0.0])
    store_triples(conn, [
        {"subject": "Alice", "predicate": "works_at", "object": "FutureCo",
         "valid_until": "2099-12-31T00:00:00+00:00"},
    ], source_atom_id="obs1", embed_fn=embed_fn)
    ref = datetime(2026, 1, 1, tzinfo=timezone.utc)
    results = triple_augment_search(conn, [1.0, 0.0, 0.0, 0.0], top_k=5,
                                    reference_date=ref)
    assert results  # not expired


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
    from mimir.saga.synthesize import _parse_contradictions
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
    from mimir.saga.synthesize import _parse_contradictions
    out = _parse_contradictions("CONTRADICTIONS:\nNONE")
    assert out == []


def test_parse_contradictions_returns_empty_when_no_section():
    from mimir.saga.synthesize import _parse_contradictions
    out = _parse_contradictions("OBSERVATION: just an observation.")
    assert out == []


# ─── P48: build_vocab_block ──────────────────────────────────────────


def test_build_vocab_block_seed_only_on_cold_db(conn):
    """Cold DB → block contains the static predicate + subject seed.
    Bench-OFF callers (and the first consolidate pass ever) hit this
    path. The non-empty seed guarantees the LLM always sees a canonical
    set, not an empty hint."""
    from mimir.saga.synthesize import build_vocab_block
    block = build_vocab_block(conn)
    # Seed predicates surface as bare names (no counts).
    assert "prefers" in block
    assert "lives_in" in block
    # Seed subjects surface as bare names.
    assert "User" in block
    assert "Assistant" in block
    # Header is the load-bearing instruction to the LLM.
    assert "Existing canonical vocabulary" in block
    # Trailing double-newline lets the prompt template flow into the
    # next section cleanly.
    assert block.endswith("\n\n")


def test_build_vocab_block_surfaces_db_top_n_with_counts(conn):
    """Predicates and subjects present in the live triples table land
    in the block annotated with their count, ordered most-frequent
    first. The seed unions in below."""
    from mimir.saga.synthesize import build_vocab_block
    _seed_atom(conn, "obs1", "obs1")
    _seed_atom(conn, "obs2", "obs2")
    _seed_atom(conn, "obs3", "obs3")
    # 3 'manufactures' predicates → highest count.
    store_triples(conn, [
        {"subject": "ACME", "predicate": "manufactures", "object": "widgets"},
    ], source_atom_id="obs1")
    store_triples(conn, [
        {"subject": "ACME", "predicate": "manufactures", "object": "gizmos"},
    ], source_atom_id="obs2")
    store_triples(conn, [
        {"subject": "ACME", "predicate": "manufactures", "object": "thingamajigs"},
    ], source_atom_id="obs3")
    # 1 'employs' predicate → lower count, still surfaces.
    store_triples(conn, [
        {"subject": "ACME", "predicate": "employs", "object": "engineers"},
    ], source_atom_id="obs1")
    block = build_vocab_block(conn)
    # DB-derived predicates carry parenthesized counts.
    assert "manufactures (3)" in block
    assert "employs (1)" in block
    # DB-derived subject surfaces with its count.
    assert "ACME (4)" in block


def test_build_vocab_block_includes_extra_subjects(conn):
    """Operator-supplied subjects (identities.yaml entries) land in the
    subject list as bare names, distinguishing them from DB-derived
    entries. Surface for production callers that want to inject custom
    canonical identities into the LLM's view."""
    from mimir.saga.synthesize import build_vocab_block
    block = build_vocab_block(conn, extra_subjects=["MyCompany", "MyTeam"])
    assert "MyCompany" in block
    assert "MyTeam" in block


def test_build_vocab_block_dedupes_extras_against_seed(conn):
    """Passing a subject that's already in the seed (e.g. ``User``)
    doesn't duplicate it. Defensive: operators shouldn't have to
    remember which subjects are seeded."""
    from mimir.saga.synthesize import build_vocab_block
    block = build_vocab_block(conn, extra_subjects=["User", "MyTeam"])
    subj_line = [l for l in block.split("\n") if l.startswith("Subjects:")][0]
    # Strip the "Subjects: " header before splitting by comma.
    entries = [s.strip() for s in subj_line[len("Subjects:"):].split(",")]
    user_entries = [s for s in entries if s == "User"]
    assert len(user_entries) == 1
    # MyTeam landed.
    assert "MyTeam" in entries


# ─── P47: build_prior_block ──────────────────────────────────────────


def test_build_prior_block_empty_when_no_priors(conn):
    """Cluster with no subset observations → empty block. The prompt
    placeholder gracefully renders nothing."""
    from mimir.saga.synthesize import build_prior_block
    _seed_atom(conn, "raw1", "raw1")
    _seed_atom(conn, "raw2", "raw2")
    assert build_prior_block(conn, ["raw1", "raw2"]) == ""


def test_build_prior_block_surfaces_strict_subset_observation_triples(conn):
    """An older observation built from raws ⊂ the new cluster's raws
    surfaces its triples in the prior block. Equal-evidence (not strict
    subset) is excluded — equal-evidence reuse is handled separately."""
    from mimir.saga.synthesize import build_prior_block
    # Two raws — earlier observation evidenced by just raw1.
    _seed_atom(conn, "raw1", "raw1")
    _seed_atom(conn, "raw2", "raw2")
    _seed_atom(conn, "obs_old", "old observation")
    conn.execute(
        "UPDATE atoms SET memory_type = 'observation' WHERE id = 'obs_old'"
    )
    conn.execute(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES ('obs_old', 'raw1', 'evidenced_by', 1.0, '2026-05-13T00:00:00Z')"
    )
    conn.commit()
    store_triples(conn, [
        {"subject": "User", "predicate": "prefers", "object": "tea"},
    ], source_atom_id="obs_old")
    # New cluster pulls in raw1 + raw2 → obs_old's evidence ({raw1}) is
    # a strict subset of {raw1, raw2}.
    block = build_prior_block(conn, ["raw1", "raw2"])
    assert "(User, prefers, tea)" in block
    assert "Previous beliefs" in block


def test_build_prior_block_excludes_equal_and_superset_observations(conn):
    """Observations with evidence ⊇ the cluster (equal or superset)
    are NOT priors — they're either the equal-evidence-skip case or
    can't be revised by a smaller cluster. Only strict subsets count."""
    from mimir.saga.synthesize import build_prior_block
    _seed_atom(conn, "raw1", "raw1")
    _seed_atom(conn, "raw2", "raw2")
    _seed_atom(conn, "raw3", "raw3")
    _seed_atom(conn, "obs_equal", "equal-evidence obs")
    _seed_atom(conn, "obs_super", "superset obs")
    conn.execute(
        "UPDATE atoms SET memory_type = 'observation' "
        "WHERE id IN ('obs_equal', 'obs_super')"
    )
    # obs_equal: evidence = {raw1, raw2} (equal).
    conn.executemany(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, '2026-05-13T00:00:00Z')",
        [("obs_equal", "raw1"), ("obs_equal", "raw2")],
    )
    # obs_super: evidence = {raw1, raw2, raw3} (superset).
    conn.executemany(
        "INSERT INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, created_at) "
        "VALUES (?, ?, 'evidenced_by', 1.0, '2026-05-13T00:00:00Z')",
        [("obs_super", "raw1"), ("obs_super", "raw2"), ("obs_super", "raw3")],
    )
    conn.commit()
    store_triples(conn, [
        {"subject": "User", "predicate": "prefers", "object": "equal_obj"},
    ], source_atom_id="obs_equal")
    store_triples(conn, [
        {"subject": "User", "predicate": "prefers", "object": "super_obj"},
    ], source_atom_id="obs_super")
    block = build_prior_block(conn, ["raw1", "raw2"])
    # Neither observation's triples should appear.
    assert "equal_obj" not in block
    assert "super_obj" not in block
    # And with no strict-subset priors at all, the block is empty.
    assert block == ""


def test_build_prior_block_skips_when_cluster_too_small(conn):
    """A 1-atom cluster can't have any strict-subset priors (subset of
    a 1-element set is the empty set, which we don't track as
    evidence). Guard returns empty without hitting the DB."""
    from mimir.saga.synthesize import build_prior_block
    _seed_atom(conn, "raw1", "raw1")
    assert build_prior_block(conn, ["raw1"]) == ""


def test_rich_prompt_renders_with_blocks_populated():
    """End-to-end: ``RICH_PROMPT.format`` accepts vocab_block and
    prior_block as kwargs without KeyError or stray braces. Pins the
    template format-string contract."""
    from mimir.saga.synthesize import RICH_PROMPT
    out = RICH_PROMPT.format(
        n=2,
        indexed_atoms="[1] hello\n[2] world",
        prior_block="Previous beliefs: foo\n\n",
        vocab_block="Existing canonical vocabulary: bar\n\n",
    )
    assert "Previous beliefs: foo" in out
    assert "Existing canonical vocabulary: bar" in out
    assert "[1] hello" in out


def test_rich_prompt_renders_with_blocks_empty():
    """Bench-neutrality: empty blocks render to literal empty in the
    template — no leftover placeholder strings, no double newlines that
    would change the prompt's structure for the bench-OFF path."""
    from mimir.saga.synthesize import RICH_PROMPT
    out = RICH_PROMPT.format(
        n=2, indexed_atoms="[1] hello\n[2] world",
        prior_block="", vocab_block="",
    )
    assert "{prior_block}" not in out
    assert "{vocab_block}" not in out
    # Atoms section still flows cleanly even with empty prior block.
    assert "Atoms:\n[1] hello" in out


# ─── vectorized cosine helper (chainlink #257 perf half) ─────────────


class TestCosineScoresVectorized:
    """_cosine_scores replaces the former O(N·dim) Python loop with one
    numpy matmul; these lock in the ranking + the row-skip semantics it
    must preserve."""

    @staticmethod
    def _blob(vec):
        import numpy as np
        return np.asarray(vec, dtype=np.float32).tobytes()

    def test_ranks_by_cosine(self):
        from mimir.saga.triples import _cosine_scores
        q = [1.0, 0.0, 0.0, 0.0]
        cands = [
            (self._blob([1.0, 0.0, 0.0, 0.0]), 4),       # identical → 1.0
            (self._blob([0.0, 1.0, 0.0, 0.0]), 4),       # orthogonal → 0.0
            (self._blob([0.7071, 0.7071, 0.0, 0.0]), 4),  # 45° → ~0.707
        ]
        scores = dict(_cosine_scores(q, cands, dim=4))
        assert scores[0] == pytest.approx(1.0, abs=1e-4)
        assert scores[1] == pytest.approx(0.0, abs=1e-4)
        assert scores[2] == pytest.approx(0.7071, abs=1e-3)

    def test_skips_dim_mismatch(self):
        from mimir.saga.triples import _cosine_scores
        q = [1.0, 0.0, 0.0, 0.0]
        cands = [
            (self._blob([1.0, 0.0, 0.0, 0.0]), 4),
            (self._blob([1.0, 0.0, 0.0]), 3),  # t_dim 3 ≠ requested dim 4
        ]
        assert [i for i, _ in _cosine_scores(q, cands, dim=4)] == [0]

    def test_skips_short_blob(self):
        from mimir.saga.triples import _cosine_scores
        # 4 bytes (one float) for a t_dim=4 row → too short → skipped.
        assert _cosine_scores([1.0, 0.0, 0.0, 0.0], [(b"\x00\x00\x00\x00", 4)],
                              dim=4) == []

    def test_zero_norm_query_returns_empty(self):
        from mimir.saga.triples import _cosine_scores
        cands = [(self._blob([1.0, 0.0, 0.0, 0.0]), 4)]
        assert _cosine_scores([0.0, 0.0, 0.0, 0.0], cands, dim=4) == []

    def test_skips_zero_norm_vector(self):
        from mimir.saga.triples import _cosine_scores
        q = [1.0, 0.0, 0.0, 0.0]
        cands = [
            (self._blob([0.0, 0.0, 0.0, 0.0]), 4),  # zero vector → skipped
            (self._blob([1.0, 0.0, 0.0, 0.0]), 4),
        ]
        assert [i for i, _ in _cosine_scores(q, cands, dim=4)] == [1]

    def test_none_dim_uses_query_dim(self):
        from mimir.saga.triples import _cosine_scores
        # t_dim None → assume query dim; no dim filter applied.
        scores = _cosine_scores([1.0, 0.0, 0.0, 0.0],
                                [(self._blob([1.0, 0.0, 0.0, 0.0]), None)], dim=None)
        assert scores and scores[0][1] == pytest.approx(1.0, abs=1e-4)
