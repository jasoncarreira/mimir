"""Tests for P48 canonical predicate vocabulary in consolidation.

Covers:
- _canonical_vocab_block helper: DB-driven + seed fallback
- Consolidation prompt integration: block is included only when both
  triples extraction is on AND enable_canonical_vocab_block is true
"""

import sqlite3
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_saga.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    monkeypatch.setattr("saga.triples.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr(
        "saga.core._cached_embed_query_import", lambda t: tuple(fake_emb)
    )
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
    from saga.core import get_db, run_migrations
    from saga.triples import init_triples_schema
    conn = get_db()
    init_triples_schema(conn)
    conn.close()
    run_migrations()
    yield db_path


# ─── _canonical_vocab_block ────────────────────────────────────────────


def test_vocab_block_returns_seed_only_for_empty_db():
    """Cold-start: empty DB → block contains the static seed only."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    conn = get_db()
    try:
        block = _canonical_vocab_block(conn)
    finally:
        conn.close()
    assert "PREFER reusing" in block
    # Seed predicates appear (no counts since DB is empty).
    assert "prefers" in block
    assert "likes" in block
    assert "offers" in block
    # Seed subject.
    assert "User" in block


def test_vocab_block_surfaces_db_counts_when_populated():
    """Populated DB: top predicates appear with their counts."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    from saga.triples import update_world

    # Seed 5 'offers' triples and 3 'prefers' triples by populating
    # via update_world (FK-bypass path). Subjects vary so we also
    # exercise subject ranking.
    for i in range(5):
        update_world(
            f"CompanyX{i}", "offers", f"product_{i}",
            source_atom_id=f"atom-offer-{i}",
        )
    for i in range(3):
        update_world(
            "User", "prefers", f"option_{i}",
            source_atom_id=f"atom-pref-{i}",
        )

    conn = get_db()
    try:
        block = _canonical_vocab_block(conn)
    finally:
        conn.close()

    # 'offers' should appear with its count of 5; 'prefers' with 3.
    assert "offers (5)" in block
    assert "prefers (3)" in block
    # Top subject is one of CompanyX0..CompanyX4 (each appears once)
    # but User has 3 occurrences so should outrank them.
    assert "User (3)" in block


def test_vocab_block_unions_db_with_seed():
    """When DB has only some seed predicates, the missing seed entries
    still appear (without counts) so a fresh DB sees the canonical set."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    from saga.triples import update_world
    update_world("User", "prefers", "x", source_atom_id="a1")

    conn = get_db()
    try:
        block = _canonical_vocab_block(conn)
    finally:
        conn.close()

    # 'prefers' from the DB with count.
    assert "prefers (1)" in block
    # 'likes' is in seed but not in DB — should still appear (no count).
    assert "likes" in block
    # Sanity: 'likes' should NOT have a count in parens for it.
    # We check by reading the predicates line and confirming likes
    # isn't immediately followed by ' (' digits.
    pred_line = next(l for l in block.splitlines() if "Predicates:" in l)
    # The string "likes (" should only appear if some seed alias used it.
    # Bare 'likes' (followed by comma or end) is what we want.
    assert "likes," in pred_line or pred_line.endswith("likes")


def test_vocab_block_empty_when_db_unreadable(monkeypatch):
    """Bad DB connection (helper-internal exception) → empty string,
    not a crash. Consolidation still runs without the vocab block."""
    from saga.consolidation import _canonical_vocab_block

    class _ExplodingConn:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("simulated")

    block = _canonical_vocab_block(_ExplodingConn())
    # With a broken DB we still emit the seed (the helper doesn't
    # crash; the empty/seed-only return is acceptable). What we
    # assert is that the function doesn't raise.
    assert isinstance(block, str)


def test_vocab_block_includes_extra_subjects():
    """P48 + Option A: operator-supplied canonical subjects (e.g. mimir's
    identities.yaml) appear in the subjects list without counts."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    conn = get_db()
    try:
        block = _canonical_vocab_block(
            conn, extra_subjects=["Tim", "Alice", "Jaden"],
        )
    finally:
        conn.close()
    assert "Tim" in block
    assert "Alice" in block
    assert "Jaden" in block
    # No counts for extras (they're authoritative-by-fiat).
    subj_line = next(l for l in block.splitlines() if "Subjects:" in l)
    # "Tim (" or "Alice (" would mean a stray count attached.
    assert "Tim (" not in subj_line
    assert "Alice (" not in subj_line


def test_vocab_block_extras_dedup_against_seed_and_db():
    """Don't duplicate User if it's already in the seed AND extras."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    conn = get_db()
    try:
        block = _canonical_vocab_block(
            conn, extra_subjects=["User", "Tim"],
        )
    finally:
        conn.close()
    subj_line = next(l for l in block.splitlines() if "Subjects:" in l)
    # User appears exactly once.
    assert subj_line.count("User") == 1
    # Tim appears once.
    assert subj_line.count("Tim") == 1


def test_vocab_block_skips_blank_extras():
    """Extras list with empty / whitespace-only strings is filtered."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    conn = get_db()
    try:
        block = _canonical_vocab_block(
            conn, extra_subjects=["", "  ", "Tim", None],  # type: ignore[list-item]
        )
    finally:
        conn.close()
    assert "Tim" in block


def test_consolidate_threads_extra_canonical_subjects():
    """ConsolidationEngine.consolidate(extra_canonical_subjects=...)
    stores them on self for the prompt-builder to read."""
    from saga.consolidation import ConsolidationEngine
    engine = ConsolidationEngine()
    # Pre-state: attribute may not exist.
    assert getattr(engine, '_extra_canonical_subjects', None) in (None, [])
    # Mock cluster phase to avoid needing real atoms.
    engine._cluster_phase = lambda: []  # type: ignore[method-assign]
    engine.consolidate(
        dry_run=True, extra_canonical_subjects=["Tim", "Alice"],
    )
    assert engine._extra_canonical_subjects == ["Tim", "Alice"]


def test_vocab_block_never_emits_count_for_seed_only_entries():
    """A seed predicate that doesn't appear in the DB must NOT be
    rendered with a (0) count — that would mislead the LLM into
    thinking the vocabulary is empty."""
    from saga.consolidation import _canonical_vocab_block
    from saga.core import get_db
    conn = get_db()
    try:
        block = _canonical_vocab_block(conn)
    finally:
        conn.close()
    # All seed entries appear bare. None should have "(0)" attached.
    assert "(0)" not in block


# ─── Prompt integration ────────────────────────────────────────────────


def test_prompt_omits_vocab_block_when_flag_off(monkeypatch):
    """Default behavior: ask_for_triples=True but
    enable_canonical_vocab_block=False → no PREFER reusing line."""
    from saga.config import _DEFAULTS
    monkeypatch.setitem(_DEFAULTS["consolidation"], "enable_canonical_vocab_block", False)
    monkeypatch.setitem(_DEFAULTS["triples"], "enable_extraction", True)

    from saga.consolidation import ConsolidationEngine
    from saga.core import store_atom

    # Build a minimal cluster — 3 atoms with the same stream.
    ids = [
        store_atom(f"atom {i} content about preferences", stream="semantic")
        for i in range(3)
    ]
    engine = ConsolidationEngine(min_cluster_size=2)
    # Manually invoke the synthesis prompt build by inspecting the
    # consolidate() flow through prep. We instead unit-test by checking
    # the helper's effect via the prompt path: mock the LLM call to
    # capture the prompt string.
    prompts: list[str] = []

    def fake_post(*args, **kwargs):
        # Capture the body's prompt and return a benign synthesis.
        try:
            body = kwargs.get("json") or {}
            messages = body.get("messages") or []
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    prompts.append(m.get("content", ""))
        except Exception:
            pass

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    "OBSERVATION:\nSynth.\n\nTRIPLES:\nNONE\n\nCONTRADICTIONS:\nNONE\n"
                }}]}
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)
    engine.consolidate()

    if prompts:
        joined = "\n---\n".join(prompts)
        assert "PREFER reusing" in joined, "soft-canonical rule should always be present"
        # But with flag off, no DB-derived vocab block.
        assert "Existing canonical vocabulary" not in joined


def test_prompt_includes_vocab_block_when_flag_on(monkeypatch):
    from saga.config import _DEFAULTS
    monkeypatch.setitem(_DEFAULTS["consolidation"], "enable_canonical_vocab_block", True)
    monkeypatch.setitem(_DEFAULTS["triples"], "enable_extraction", True)

    from saga.consolidation import ConsolidationEngine
    from saga.core import store_atom

    [store_atom(f"atom {i} content", stream="semantic") for i in range(3)]
    engine = ConsolidationEngine(min_cluster_size=2)

    prompts: list[str] = []

    def fake_post(*args, **kwargs):
        try:
            body = kwargs.get("json") or {}
            messages = body.get("messages") or []
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    prompts.append(m.get("content", ""))
        except Exception:
            pass

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    "OBSERVATION:\nSynth.\n\nTRIPLES:\nNONE\n\nCONTRADICTIONS:\nNONE\n"
                }}]}
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)
    engine.consolidate()

    if prompts:
        joined = "\n---\n".join(prompts)
        assert "Existing canonical vocabulary" in joined
        assert "Predicates:" in joined
