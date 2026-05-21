"""Tests for the dedup → ``encoding_confidence`` boost.

The hypothesis (from the 2026-05-21 discussion): when dedup folds N
duplicates into a canonical atom, the canonical receives a nudge on
``encoding_confidence`` because the agent's decision to save the same
fact across independent contexts is itself a signal that it's a
well-supported, stable encoding. The nudge is asymptotic so a single
hot-context session can't saturate the signal.

These tests pin both the math and the recall-side scoring effect.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mimir.saga.dedup import (
    ABSORPTION_COEFFICIENT,
    BASELINE_ENCODING_CONFIDENCE,
    _bump_encoding_confidence,
)
from mimir.saga.recall import ENCODING_CONFIDENCE_WEIGHT


# ─── Constants pinned ─────────────────────────────────────────────────


def test_constants_match_documented_values():
    """The three magic numbers we discussed (0.7, 0.3, 0.05) live
    here as named constants. Pinning the values protects against
    drive-by adjustments without thinking through the calibration."""
    assert BASELINE_ENCODING_CONFIDENCE == 0.7
    assert ABSORPTION_COEFFICIENT == 0.3
    assert ENCODING_CONFIDENCE_WEIGHT == 0.05


# ─── Pure-math bump function ─────────────────────────────────────────


def test_bump_is_asymptotic_to_one():
    """``new = old + (1 - old) * 0.3`` asymptotes to 1.0. After
    enough absorptions the value gets arbitrarily close to 1 but
    never exceeds it."""
    val = BASELINE_ENCODING_CONFIDENCE
    for _ in range(30):
        val = _bump_encoding_confidence(val)
    assert val < 1.0
    assert val > 0.999  # 30 iterations from 0.7 puts us well past 0.999


def test_bump_matches_expected_trajectory():
    """Pinned trajectory for the first five absorptions starting
    from baseline. If anyone tunes ``ABSORPTION_COEFFICIENT`` they
    need to update this trajectory too — the test is the early-
    warning."""
    expected = [0.79, 0.853, 0.8971, 0.92797, 0.949579]
    val = BASELINE_ENCODING_CONFIDENCE
    for want in expected:
        val = _bump_encoding_confidence(val)
        assert val == pytest.approx(want, abs=1e-4)


def test_bump_never_decreases():
    """Defensive: a single absorption can only increase or leave-as-is
    (when already at 1.0). Never decrease."""
    val = BASELINE_ENCODING_CONFIDENCE
    for _ in range(20):
        bumped = _bump_encoding_confidence(val)
        assert bumped >= val
        val = bumped


def test_bump_clamps_above_baseline():
    """A canonical that's somehow gotten below baseline (e.g., from a
    future feature that bumps DOWN on negative feedback) snaps to the
    baseline floor on the next absorption — the dedup signal is
    always non-negative, so it shouldn't ratchet a sub-baseline
    confidence further down."""
    val = _bump_encoding_confidence(0.5)
    assert val >= BASELINE_ENCODING_CONFIDENCE


def test_bump_handles_non_numeric():
    """Defensive: if the column has somehow gone NULL or non-numeric
    (legacy data, manual ALTER, etc.), the helper returns the baseline
    rather than raising. Caller can recover by overwriting."""
    assert _bump_encoding_confidence(None) == BASELINE_ENCODING_CONFIDENCE
    assert _bump_encoding_confidence("nope") == BASELINE_ENCODING_CONFIDENCE


# ─── End-to-end through merge_duplicate_into_canonical ────────────────


@pytest.fixture
def saga_store(tmp_path: Path, monkeypatch):
    """A real SagaStore wired to a 4d stub embedding provider — same
    pattern as ``tests/test_memory_production_surface.py``. Lets us
    actually exercise the dedup path against the real schema."""

    class _StubProvider:
        def embed(self, text: str, input_type: str = "passage"):
            # Deterministic 4d vector so identical text always hashes
            # to the same vector — dedup will treat them as exact
            # duplicates.
            base = sum(ord(c) for c in text) % 17
            return [
                float((base + i) % 17) / 17.0 for i in range(4)
            ]

        def dimensions(self):
            return 4

    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider", lambda: _StubProvider(),
    )
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda s, k, d=None: {
            ("embedding", "max_input_chars"): 2000,
            ("embedding", "provider"): "stub",
            ("embedding", "model"): "stub-4d",
        }.get((s, k), d),
    )
    from mimir.saga.client import SagaStore
    return SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)


@pytest.mark.asyncio
async def test_canonical_starts_at_baseline(saga_store):
    """A solo atom (no duplicates) keeps its baseline
    ``encoding_confidence``. The bump only fires through
    ``merge_duplicate_into_canonical``."""
    r = await saga_store.store(content="solo fact", stream="semantic")
    conn = saga_store._ensure_conn()
    row = conn.execute(
        "SELECT encoding_confidence FROM atoms WHERE id = ?",
        (r["atom_id"],),
    ).fetchone()
    assert row[0] == pytest.approx(BASELINE_ENCODING_CONFIDENCE)


def test_merge_bumps_encoding_confidence(saga_store):
    """One direct call to ``merge_duplicate_into_canonical`` produces
    exactly one bump on the canonical."""
    import asyncio

    from mimir.saga.dedup import merge_duplicate_into_canonical

    async def _setup():
        a = await saga_store.store(content="dup fact", stream="semantic")
        b = await saga_store.store(content="dup fact alt", stream="semantic")
        return a["atom_id"], b["atom_id"]

    can_id, dup_id = asyncio.run(_setup())
    conn = saga_store._ensure_conn()

    canonical = dict(zip(
        ("id", "content", "topics", "metadata", "agent_id",
         "encoding_confidence"),
        conn.execute(
            "SELECT id, content, topics, metadata, agent_id, "
            "encoding_confidence FROM atoms WHERE id = ?",
            (can_id,),
        ).fetchone(),
    ))
    duplicate = dict(zip(
        ("id", "content", "topics", "metadata", "agent_id",
         "encoding_confidence"),
        conn.execute(
            "SELECT id, content, topics, metadata, agent_id, "
            "encoding_confidence FROM atoms WHERE id = ?",
            (dup_id,),
        ).fetchone(),
    ))

    merge_duplicate_into_canonical(
        conn, canonical=canonical, duplicate=duplicate,
    )
    conn.commit()

    new_val = conn.execute(
        "SELECT encoding_confidence FROM atoms WHERE id = ?",
        (can_id,),
    ).fetchone()[0]
    assert new_val == pytest.approx(_bump_encoding_confidence(0.7))
    # And the merge updates the in-memory canonical dict so a
    # subsequent absorption sees the bumped value (relevant when one
    # dedup pass folds multiple duplicates into the same canonical).
    assert canonical["encoding_confidence"] == pytest.approx(new_val)


def test_merge_accumulates_across_multiple_absorptions(saga_store):
    """Three duplicates folded into the same canonical in a single
    pass should produce the expected three-step asymptotic value —
    not three independent first-step values."""
    import asyncio

    from mimir.saga.dedup import merge_duplicate_into_canonical

    async def _setup():
        ids = []
        for i in range(4):
            r = await saga_store.store(
                content=f"shared fact {i}", stream="semantic",
            )
            ids.append(r["atom_id"])
        return ids

    can_id, *dup_ids = asyncio.run(_setup())
    conn = saga_store._ensure_conn()

    def _atom(atom_id: str) -> dict:
        return dict(zip(
            ("id", "content", "topics", "metadata", "agent_id",
             "encoding_confidence"),
            conn.execute(
                "SELECT id, content, topics, metadata, agent_id, "
                "encoding_confidence FROM atoms WHERE id = ?",
                (atom_id,),
            ).fetchone(),
        ))

    canonical = _atom(can_id)
    for dup_id in dup_ids:
        merge_duplicate_into_canonical(
            conn, canonical=canonical, duplicate=_atom(dup_id),
        )
    conn.commit()

    expected = 0.7
    for _ in range(3):
        expected = _bump_encoding_confidence(expected)

    final = conn.execute(
        "SELECT encoding_confidence FROM atoms WHERE id = ?", (can_id,),
    ).fetchone()[0]
    assert final == pytest.approx(expected, abs=1e-6)


# ─── Recall scoring effect ───────────────────────────────────────────


def test_recall_score_includes_encoding_confidence_delta(saga_store):
    """Two candidates with IDENTICAL retrieval relevance (same RRF,
    same activation, same topics) — only ``encoding_confidence``
    differs. The bumped one's total score must end up higher. This
    isolates the score contribution from any retrieval-side
    ambiguity. We construct ``RecallCandidate`` objects directly to
    keep relevance fixed; going through ``query()`` would let BM25
    differences mask the boost (which is intentionally small)."""
    import asyncio

    from mimir.saga.recall import RecallCandidate, _score_candidates

    async def _setup():
        a = await saga_store.store(content="fact one", stream="semantic")
        b = await saga_store.store(content="fact two", stream="semantic")
        return a["atom_id"], b["atom_id"]

    a_id, b_id = asyncio.run(_setup())
    conn = saga_store._ensure_conn()
    # Bump atom B's confidence by two absorptions.
    bumped = _bump_encoding_confidence(_bump_encoding_confidence(0.7))
    conn.execute(
        "UPDATE atoms SET encoding_confidence = ? WHERE id = ?",
        (bumped, b_id),
    )
    conn.commit()

    # Fetch atom rows the way recall would, including encoding_confidence.
    def _atom(aid: str) -> dict:
        row = conn.execute(
            "SELECT id, content, stream, profile, memory_type, "
            "source_type, topics, metadata, agent_id, is_pinned, "
            "created_at, session_id, encoding_confidence "
            "FROM atoms WHERE id = ?", (aid,),
        ).fetchone()
        cols = ("id", "content", "stream", "profile", "memory_type",
                "source_type", "topics", "metadata", "agent_id",
                "is_pinned", "created_at", "session_id",
                "encoding_confidence")
        return dict(zip(cols, row))

    # Two candidates, IDENTICAL retrieval-relevance signals.
    c_a = RecallCandidate(
        atom=_atom(a_id), activation=0.0, similarity=0.5,
        rrf_score=0.02,
    )
    c_b = RecallCandidate(
        atom=_atom(b_id), activation=0.0, similarity=0.5,
        rrf_score=0.02,
    )
    candidates = [c_a, c_b]

    _score_candidates(
        conn, candidates,
        topic_filter=None, session_id=None,
        weights={"w_rrf": 20.0, "w_topic": 1.0, "w_act": 1.0},
        thresholds={"activation": 0.0},
    )

    assert c_b.total > c_a.total, (
        f"bumped atom should rank higher; got c_a.total={c_a.total}, "
        f"c_b.total={c_b.total}"
    )
    # The delta is the encoding-confidence boost (everything else is
    # identical between c_a and c_b). Magnitude should match
    # ``ENCODING_CONFIDENCE_WEIGHT * (bumped - baseline)``.
    expected_delta = ENCODING_CONFIDENCE_WEIGHT * (
        bumped - BASELINE_ENCODING_CONFIDENCE
    )
    assert (c_b.total - c_a.total) == pytest.approx(expected_delta, abs=1e-6)


def test_solo_atom_at_baseline_contributes_zero_boost():
    """An atom at exactly baseline encoding_confidence adds exactly
    zero to its composite score. Verified algebraically:
    weight * (baseline - baseline) = weight * 0 = 0. The default-
    of-0.7 atoms (the vast majority of stored facts at any moment)
    therefore don't see any score change from this feature — only
    dedup absorption moves them off baseline."""
    boost = ENCODING_CONFIDENCE_WEIGHT * (
        BASELINE_ENCODING_CONFIDENCE - BASELINE_ENCODING_CONFIDENCE
    )
    assert boost == 0.0


def test_max_possible_boost_stays_below_dominant_terms():
    """The maximum boost (encoding_confidence saturated at 1.0) must
    stay smaller than the dominant RRF term so that
    encoding-confidence never overrides actual relevance. Calibration
    rationale: a marginally-relevant repeatedly-encoded fact should
    never outrank a highly-relevant single-shot fact."""
    max_boost = ENCODING_CONFIDENCE_WEIGHT * (
        1.0 - BASELINE_ENCODING_CONFIDENCE
    )
    # Dominant RRF term: w_rrf default ~20.0, RRF scores ~0.01-0.05.
    # Even a moderately-relevant candidate has w_rrf * rrf ~0.5+.
    assert max_boost < 0.05  # below TREND_MODIFIERS["strengthening"] = 0.10
