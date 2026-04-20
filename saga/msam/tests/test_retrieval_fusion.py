"""Tests for Reciprocal Rank Fusion and its integration with hybrid_retrieve."""
from __future__ import annotations

import numpy as np
import pytest

from msam.retrieval_fusion import reciprocal_rank_fusion


@pytest.fixture
def fake_embeddings(monkeypatch, tmp_path):
    """Stub out the embedding provider + DB path for integration tests."""
    db_path = tmp_path / "t.db"
    monkeypatch.setattr("msam.core.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("msam.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.cached_embed_query", lambda t: fake_emb)
    return db_path


class TestRRF:
    def test_empty_returns_empty(self):
        assert reciprocal_rank_fusion({}) == []

    def test_single_pathway_preserves_order(self):
        result = reciprocal_rank_fusion({"semantic": ["a", "b", "c"]})
        assert [aid for aid, _ in result] == ["a", "b", "c"]

    def test_overlap_boosts_docs_found_by_both(self):
        # 'a' appears in both at rank 1; 'b' only in semantic at rank 2;
        # 'c' only in keyword at rank 2. 'a' should win.
        result = reciprocal_rank_fusion({
            "semantic": ["a", "b"],
            "keyword": ["a", "c"],
        })
        winner, _ = result[0]
        assert winner == "a"

    def test_k_dampens_rank_contribution(self):
        # With k=0, rank 1 contributes 1.0; rank 2 contributes 0.5.
        # With k=60, ranks 1 and 2 contribute nearly the same.
        low_k = reciprocal_rank_fusion({"p": ["a", "b"]}, k=0)
        high_k = reciprocal_rank_fusion({"p": ["a", "b"]}, k=60)
        low_k_gap = dict(low_k)["a"] - dict(low_k)["b"]
        high_k_gap = dict(high_k)["a"] - dict(high_k)["b"]
        assert low_k_gap > high_k_gap

    def test_weights_bias_pathway(self):
        # With equal weights, 'a' and 'b' tie (each rank-1 in one pathway).
        # Boosting 'semantic' breaks the tie toward 'a'.
        result = reciprocal_rank_fusion(
            {"semantic": ["a"], "keyword": ["b"]},
            weights={"semantic": 2.0, "keyword": 1.0},
        )
        assert result[0][0] == "a"

    def test_negative_k_raises(self):
        with pytest.raises(ValueError):
            reciprocal_rank_fusion({"p": ["a"]}, k=-1)


class TestHybridRetrieveFusionConfig:
    """Integration smoke: hybrid_retrieve respects the fusion config flag."""

    def test_weighted_sum_default_runs(self, fake_embeddings):
        import msam.core
        msam.core.get_db().close()
        msam.core.run_migrations()
        msam.core.store_atom("The user prefers Sony cameras over Canon")
        msam.core.store_atom("User's favorite hobby is landscape photography")
        results = msam.core.hybrid_retrieve("camera preferences", top_k=5)
        assert isinstance(results, list)

    def test_rrf_fusion_runs(self, fake_embeddings, monkeypatch):
        import msam.core
        msam.core.get_db().close()
        msam.core.run_migrations()
        msam.core.store_atom("The user prefers Sony cameras over Canon")
        msam.core.store_atom("User's favorite hobby is landscape photography")
        real_cfg = msam.core._cfg
        def fake_cfg(section, key, default=None):
            if section == "retrieval" and key == "fusion":
                return "rrf"
            return real_cfg(section, key, default)
        monkeypatch.setattr(msam.core, "_cfg", fake_cfg)
        results = msam.core.hybrid_retrieve("camera preferences", top_k=5)
        assert isinstance(results, list)
        for r in results:
            assert "_combined_score" in r
