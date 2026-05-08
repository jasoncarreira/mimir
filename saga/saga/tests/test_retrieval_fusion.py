"""Tests for Reciprocal Rank Fusion and its integration with hybrid_retrieve."""
from __future__ import annotations

import numpy as np
import pytest

from saga.retrieval_fusion import reciprocal_rank_fusion


@pytest.fixture
def fake_embeddings(monkeypatch, tmp_path):
    """Stub out the embedding provider + DB path for integration tests."""
    db_path = tmp_path / "t.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
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

    def _force_fusion(self, monkeypatch, mode):
        import saga.core
        real_cfg = saga.core._cfg
        def fake_cfg(section, key, default=None):
            if section == "retrieval" and key == "fusion":
                return mode
            return real_cfg(section, key, default)
        monkeypatch.setattr(saga.core, "_cfg", fake_cfg)

    @pytest.mark.asyncio
    async def test_default_runs(self, fake_embeddings):
        import saga.core
        saga.core.get_db().close()
        saga.core.run_migrations()
        saga.core.store_atom("The user prefers Sony cameras over Canon")
        saga.core.store_atom("User's favorite hobby is landscape photography")
        results = await saga.core.hybrid_retrieve("camera preferences", top_k=5)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_weighted_sum_runs(self, fake_embeddings, monkeypatch):
        import saga.core
        self._force_fusion(monkeypatch, "weighted_sum")
        saga.core.get_db().close()
        saga.core.run_migrations()
        saga.core.store_atom("The user prefers Sony cameras over Canon")
        saga.core.store_atom("User's favorite hobby is landscape photography")
        results = await saga.core.hybrid_retrieve("camera preferences", top_k=5)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_rrf_fusion_runs(self, fake_embeddings, monkeypatch):
        import saga.core
        self._force_fusion(monkeypatch, "rrf")
        saga.core.get_db().close()
        saga.core.run_migrations()
        saga.core.store_atom("The user prefers Sony cameras over Canon")
        saga.core.store_atom("User's favorite hobby is landscape photography")
        results = await saga.core.hybrid_retrieve("camera preferences", top_k=5)
        assert isinstance(results, list)
        for r in results:
            assert "_combined_score" in r


class TestTemporalPathway:
    def test_no_temporal_scope_returns_empty(self, fake_embeddings):
        import saga.core
        saga.core.get_db().close()
        saga.core.run_migrations()
        saga.core.store_atom("unrelated content")
        assert saga.core.temporal_retrieve("no time expression here") == []

    def test_returns_atoms_in_window(self, fake_embeddings):
        import saga.core
        from datetime import datetime, timezone
        saga.core.get_db().close()
        saga.core.run_migrations()
        # Store one atom, then backdate it so it sits inside "yesterday"
        # relative to a known reference date.
        atom_id = saga.core.store_atom("user went for a run")
        ref = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
        backdated = datetime(2026, 4, 19, 9, 0, 0, tzinfo=timezone.utc).isoformat()
        conn = saga.core.get_db()
        conn.execute(
            "UPDATE atoms SET created_at = ? WHERE id = ?",
            (backdated, atom_id),
        )
        conn.commit()
        conn.close()
        results = saga.core.temporal_retrieve(
            "what did I do yesterday?", top_k=5, reference_date=ref
        )
        assert len(results) == 1
        assert results[0]["id"] == atom_id
        assert "_temporal_score" in results[0]

    def test_atoms_outside_window_excluded(self, fake_embeddings):
        import saga.core
        from datetime import datetime, timezone
        saga.core.get_db().close()
        saga.core.run_migrations()
        atom_id = saga.core.store_atom("old content")
        ref = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
        # Backdate to 10 days ago — outside the "yesterday" window.
        backdated = datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc).isoformat()
        conn = saga.core.get_db()
        conn.execute(
            "UPDATE atoms SET created_at = ? WHERE id = ?",
            (backdated, atom_id),
        )
        conn.commit()
        conn.close()
        results = saga.core.temporal_retrieve(
            "what did I do yesterday?", top_k=5, reference_date=ref
        )
        assert results == []


class TestGraphPathway:
    def test_empty_triple_store_returns_empty(self, fake_embeddings):
        import saga.core
        saga.core.get_db().close()
        saga.core.run_migrations()
        saga.core.store_atom("some content")
        # No triples have been extracted — pathway must degrade gracefully.
        assert saga.core.graph_retrieve("anything", top_k=5) == []
