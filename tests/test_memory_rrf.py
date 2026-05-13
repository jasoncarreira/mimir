"""Tests for mimir.memory.retrieval_fusion — Reciprocal Rank Fusion.

Validates the canonical RRF formula and confirms the recall path
exposes per-candidate rank diagnostics.
"""
from __future__ import annotations

from mimir.memory.retrieval_fusion import DEFAULT_K, reciprocal_rank_fusion


# ─── Formula ─────────────────────────────────────────────────────────


def test_rrf_single_list_is_ordering_only():
    """One pathway, weight 1.0: score is 1/(k + 1-based-rank).
    Verifies the rank-1 = 1/(k+1) constant matches saga's docs."""
    result = reciprocal_rank_fusion(
        {"semantic": ["a", "b", "c"]}, k=60,
    )
    assert result[0][0] == "a"
    assert result[0][1] == 1 / 61
    assert result[1][1] == 1 / 62
    assert result[2][1] == 1 / 63


def test_rrf_two_lists_unions_scores():
    """Doc 'a' at rank 1 in both lists should score 1/61 + 1/61."""
    result = reciprocal_rank_fusion({
        "semantic": ["a", "b"],
        "keyword":  ["a", "c"],
    }, k=60)
    scores = dict(result)
    assert scores["a"] == 2 / 61
    assert scores["b"] == 1 / 62
    assert scores["c"] == 1 / 62
    # 'a' wins (top of both).
    assert result[0][0] == "a"


def test_rrf_weights_bias_pathways():
    """Doubling the semantic weight: semantic-only hit beats keyword-only hit."""
    # Without weights: both at rank 1 = tie at 1/61.
    result = reciprocal_rank_fusion(
        {"semantic": ["a"], "keyword": ["b"]},
        k=60, weights={"semantic": 2.0, "keyword": 1.0},
    )
    scores = dict(result)
    assert scores["a"] > scores["b"]
    assert scores["a"] == 2 / 61
    assert scores["b"] == 1 / 61


def test_rrf_zero_k_collapses_to_inverse_rank():
    """k=0 is allowed: score = 1/rank. Rank-1 = 1.0."""
    result = reciprocal_rank_fusion({"semantic": ["a", "b"]}, k=0)
    assert dict(result)["a"] == 1.0
    assert dict(result)["b"] == 0.5


def test_rrf_negative_k_raises():
    import pytest
    with pytest.raises(ValueError):
        reciprocal_rank_fusion({"semantic": ["a"]}, k=-1)


def test_rrf_empty_lists_return_empty():
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"semantic": []}) == []


def test_rrf_default_k_is_60():
    assert DEFAULT_K == 60


# ─── Integration: RRF surfaces in RecallCandidate ────────────────────


def test_recall_candidate_carries_rrf_diagnostics():
    """Per-candidate rrf_score, semantic_rank, keyword_rank must be
    populated so the turn viewer can show why an atom ranked where it did."""
    from mimir.memory.recall import RecallCandidate
    c = RecallCandidate(
        atom={"id": "x"}, activation=0.0, similarity=0.0,
        rrf_score=0.05, semantic_rank=1, keyword_rank=3,
    )
    assert c.rrf_score == 0.05
    assert c.semantic_rank == 1
    assert c.keyword_rank == 3
