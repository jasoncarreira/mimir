"""Tests for the retrieval_v2 cherry-picks (P11/P12/P13) plumbed into hybrid_retrieve.

Each enhancement is gated by its own [retrieval_v2] flag and must be a
no-op when disabled. Tests cover both the gating behavior and the
intended effect when enabled.
"""

import copy
import pytest
import numpy as np


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test_msam.db"
    monkeypatch.setattr("msam.core.DB_PATH", db_path)
    monkeypatch.setattr("msam.triples.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("msam.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("msam.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("msam.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    monkeypatch.setattr("msam.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


@pytest.fixture
def cfg_override(monkeypatch):
    """Patch msam.config._config to a deep copy of the live config and
    return the dict so the test can mutate it without bleeding into
    other tests."""
    from msam import config as cfg_mod
    cfg_mod._load_config()  # ensure loaded
    snapshot = copy.deepcopy(cfg_mod._config) if cfg_mod._config else {}
    monkeypatch.setattr(cfg_mod, "_config", snapshot)
    monkeypatch.setattr(cfg_mod, "_config_loaded", True)
    return snapshot


# ─── P11: query rewriting ────────────────────────────────────────────────

class TestQueryRewriting:
    def test_disabled_is_noop(self, cfg_override):
        from msam.core import _apply_query_rewriting
        cfg_override.setdefault("retrieval_v2", {})["enable_query_rewriting"] = False
        assert _apply_query_rewriting("What does the user think?") == \
            "What does the user think?"

    def test_enabled_normalizes_user(self, cfg_override):
        from msam.core import _apply_query_rewriting
        cfg_override.setdefault("retrieval_v2", {})["enable_query_rewriting"] = True
        out = _apply_query_rewriting("what does the user think?")
        # rewrite_query maps lowercase "user" -> "User" via _QUERY_REWRITES
        assert "User" in out


# ─── P12: synonym expansion (keyword pathway only) ───────────────────────

class TestQueryExpansion:
    def test_disabled_is_noop(self, cfg_override):
        from msam.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = False
        cfg_override["query_expansion"] = {"synonyms": {"profession": ["job"]}}
        assert _expand_query_for_keyword("what is the user's profession?") == \
            "what is the user's profession?"

    def test_enabled_appends_synonyms(self, cfg_override):
        from msam.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = True
        cfg_override["query_expansion"] = {"synonyms": {"profession": ["job", "career"]}}
        out = _expand_query_for_keyword("what is the user's profession?")
        assert "profession" in out
        assert "job" in out
        assert "career" in out

    def test_enabled_no_match_is_noop(self, cfg_override):
        from msam.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = True
        cfg_override["query_expansion"] = {"synonyms": {"profession": ["job", "career"]}}
        assert _expand_query_for_keyword("what is the weather?") == \
            "what is the weather?"

    def test_enabled_empty_dict_is_noop(self, cfg_override):
        from msam.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = True
        cfg_override["query_expansion"] = {"synonyms": {}}
        assert _expand_query_for_keyword("what is the user's profession?") == \
            "what is the user's profession?"


# ─── P13: atom quality scoring ──────────────────────────────────────────

class TestQualityScoring:
    def _make_atoms(self):
        return {
            "low": {
                "id": "low",
                # Short, repetitive, no entities -> quality < 0.3
                "content": "the the the the",
                "_combined_score": 1.0,
            },
            "mid": {
                "id": "mid",
                "content": "The user mentioned they are a software engineer at TechCorp.",
                "_combined_score": 1.0,
            },
            "high": {
                "id": "high",
                "content": (
                    "The user reported that on 2026-01-15 they purchased a Sony WH-1000XM5 "
                    "headphone for $399 from BestBuy in Boston. Notable: replaces previous "
                    "Bose QC45 model. Configured Bluetooth pairing with iPhone 15 Pro and "
                    "MacBook Pro M3 simultaneously."
                ),
                "_combined_score": 1.0,
            },
        }

    def test_disabled_is_noop(self, cfg_override):
        from msam.core import _apply_quality_scoring
        cfg_override.setdefault("retrieval_v2", {})["enable_quality_filter"] = False
        atoms = self._make_atoms()
        before = {aid: a["_combined_score"] for aid, a in atoms.items()}
        changed = _apply_quality_scoring(atoms)
        after = {aid: a["_combined_score"] for aid, a in atoms.items()}
        assert changed is False
        assert before == after

    def test_enabled_demotes_low_quality(self, cfg_override):
        from msam.core import _apply_quality_scoring
        cfg_override.setdefault("retrieval_v2", {})["enable_quality_filter"] = True
        atoms = self._make_atoms()
        changed = _apply_quality_scoring(atoms)
        assert changed is True
        assert atoms["low"]["_combined_score"] == pytest.approx(0.5)

    def test_enabled_boosts_high_quality(self, cfg_override):
        from msam.core import _apply_quality_scoring
        cfg_override.setdefault("retrieval_v2", {})["enable_quality_filter"] = True
        atoms = self._make_atoms()
        _apply_quality_scoring(atoms)
        assert atoms["high"]["_combined_score"] > 1.0
