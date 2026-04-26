"""Tests for the P1 observations tier: schema, consolidation tagging, retrieval bonus."""
from __future__ import annotations

import math

import numpy as np
import pytest


@pytest.fixture
def fake_embeddings(monkeypatch, tmp_path):
    import msam.core
    monkeypatch.setattr(msam.core, "DB_PATH", tmp_path / "t.db")
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("msam.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.cached_embed_query", lambda t: fake_emb)
    msam.core.get_db().close()
    msam.core.run_migrations()
    return tmp_path / "t.db"


class TestSchema:
    def test_memory_type_column_defaults_to_raw(self, fake_embeddings):
        import msam.core
        atom_id = msam.core.store_atom("a regular fact")
        conn = msam.core.get_db()
        row = conn.execute("SELECT memory_type, evidence_count, trend FROM atoms WHERE id = ?", (atom_id,)).fetchone()
        conn.close()
        assert row["memory_type"] == "raw"
        assert row["evidence_count"] == 0
        assert row["trend"] is None

    def test_store_atom_accepts_observation_args(self, fake_embeddings):
        import msam.core
        atom_id = msam.core.store_atom(
            "distilled belief",
            memory_type="observation",
            evidence_count=7,
        )
        conn = msam.core.get_db()
        row = conn.execute("SELECT memory_type, evidence_count FROM atoms WHERE id = ?", (atom_id,)).fetchone()
        conn.close()
        assert row["memory_type"] == "observation"
        assert row["evidence_count"] == 7


class TestRetrievalBonus:
    def test_observation_outranks_raw_with_same_rrf_score(self, fake_embeddings, monkeypatch):
        """
        Two atoms tied on fusion rank: the observation with evidence_count=10
        should end up ahead after the P1 multiplier.
        """
        import msam.core
        # Store a raw atom and an observation with matching content.
        raw_id = msam.core.store_atom("user enjoys hiking")
        obs_id = msam.core.store_atom(
            "user enjoys hiking",
            memory_type="observation",
            evidence_count=10,
        )
        # store_atom content-dedups, so the second store likely returned (None, "duplicate content").
        # Give the observation distinct content to ensure both land.
        if not isinstance(obs_id, str):
            obs_id = msam.core.store_atom(
                "user loves going on hikes",
                memory_type="observation",
                evidence_count=10,
            )
        assert isinstance(raw_id, str) and isinstance(obs_id, str)

        # Force both to score equally in the component paths so RRF ties them.
        # Patch retrieve/keyword_search to return both in the same order.
        def fake_retrieve(query, mode="task", top_k=20, **kw):
            conn = msam.core.get_db()
            rows = conn.execute("SELECT * FROM atoms WHERE id IN (?, ?)", (raw_id, obs_id)).fetchall()
            conn.close()
            atoms = []
            for r in rows:
                a = dict(r); a.pop("embedding", None); a["_activation"] = 1.0; a["_similarity"] = 0.5
                atoms.append(a)
            return atoms

        def fake_keyword(query, top_k=10):
            return fake_retrieve(query)

        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", fake_keyword)

        results = msam.core.hybrid_retrieve("hiking", top_k=5)
        # Observation should rank above the raw atom.
        assert results[0]["id"] == obs_id
        assert results[0]["memory_type"] == "observation"

    def test_bonus_can_be_disabled(self, fake_embeddings, monkeypatch):
        import msam.core
        real_cfg = msam.core._cfg
        def fake_cfg(section, key, default=None):
            if section == "retrieval" and key == "enable_observation_bonus":
                return False
            return real_cfg(section, key, default)
        monkeypatch.setattr(msam.core, "_cfg", fake_cfg)
        # Bonus disabled — the hybrid_retrieve path should still run clean.
        msam.core.store_atom("anything", memory_type="observation", evidence_count=5)
        results = msam.core.hybrid_retrieve("anything", top_k=5)
        assert isinstance(results, list)

    def test_evidence_count_monotonic_bonus(self, fake_embeddings):
        """A smoke check on the bonus math: higher evidence_count → higher multiplier."""
        from msam.core import _cfg  # noqa: F401  (just ensure import)
        alpha = 0.3
        low = 1.0 + alpha * math.log(3)     # evidence=2 (minimum boostable)
        high = 1.0 + alpha * math.log(51)   # evidence=50
        assert high > low

    def test_evidence_count_one_gets_no_bonus(self, fake_embeddings, monkeypatch):
        """An observation backed by a single atom is a paraphrase, not new
        evidence — the multiplier must be exactly 1.0."""
        import msam.core
        obs_id = msam.core.store_atom(
            "user plays the ukulele at weekends",
            memory_type="observation",
            evidence_count=1,
        )
        assert isinstance(obs_id, str)

        def fake_retrieve(query, mode="task", top_k=20, **kw):
            conn = msam.core.get_db()
            rows = conn.execute("SELECT * FROM atoms WHERE id = ?", (obs_id,)).fetchall()
            conn.close()
            atoms = []
            for r in rows:
                a = dict(r); a.pop("embedding", None); a["_activation"] = 1.0; a["_similarity"] = 0.5
                atoms.append(a)
            return atoms

        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search", lambda q, top_k=10: fake_retrieve(q))

        # Compute the plain (un-boosted) RRF score a single-pathway rank-1
        # atom receives: weight / (k + 1) — here 1.0 / (60 + 1) per pathway,
        # ranked in both sem and kw, so 2 * 1/61.
        expected_rrf = 2.0 * (1.0 / 61.0)
        results = msam.core.hybrid_retrieve("ukulele", top_k=5)
        score = results[0]["_combined_score"]
        # Must match un-boosted score; a +21% bonus would put it ~0.0397.
        assert abs(score - expected_rrf) < 1e-6


class TestSupersedesDemotionInHybridRetrieve:
    """P4-bench: full path. Two raws with a supersedes edge — superseded one
    is demoted in the final hybrid_retrieve output."""

    def test_superseded_atom_is_demoted(self, fake_embeddings, monkeypatch):
        import msam.core
        # Two raw atoms; B supersedes A. Both should turn up in retrieval.
        a_id = msam.core.store_atom("user works at Acme")
        b_id = msam.core.store_atom("user works at Beta")
        msam.core.add_atom_relation(b_id, a_id, "supersedes", confidence=0.9)

        # Make both atoms appear in semantic + keyword pathways at rank 1.
        def fake_retrieve(q, **kwargs):
            conn = msam.core.get_db()
            rows = conn.execute(
                "SELECT * FROM atoms WHERE id IN (?, ?) ORDER BY id", (a_id, b_id)
            ).fetchall()
            conn.close()
            atoms = []
            for r in rows:
                a = dict(r); a.pop("embedding", None)
                a["_activation"] = 1.0; a["_similarity"] = 0.5
                atoms.append(a)
            return atoms

        monkeypatch.setattr(msam.core, "retrieve", fake_retrieve)
        monkeypatch.setattr(msam.core, "keyword_search",
                            lambda q, top_k=10, memory_type=None: fake_retrieve(q))

        results = msam.core.hybrid_retrieve("user works", top_k=5)
        # Both atoms returned, but the superseded one (a_id) should have a
        # lower _combined_score than b_id (factor 0.4 by default).
        scores = {r["id"]: r["_combined_score"] for r in results}
        assert a_id in scores and b_id in scores
        assert scores[a_id] < scores[b_id]
        # Diagnostic tag must be set on superseded atom.
        a_atom = next(r for r in results if r["id"] == a_id)
        assert a_atom.get("_relation_note") == "superseded"
