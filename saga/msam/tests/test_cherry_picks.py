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

    def test_user_mappings_extend_defaults(self, cfg_override):
        """User-supplied entity_mappings should ADD to built-in defaults,
        not replace them. Built-in `user → User` should still apply when
        the user adds a custom mapping."""
        from msam.core import _apply_query_rewriting
        cfg_override.setdefault("retrieval_v2", {})["enable_query_rewriting"] = True
        cfg_override["retrieval_v2"]["entity_mappings"] = {
            "the bot": "Mimir",
        }
        out = _apply_query_rewriting("what did the user tell the bot?")
        # Both the built-in (user→User) and the custom (the bot→Mimir) apply.
        assert "User" in out
        assert "Mimir" in out

    def test_user_mappings_override_default_on_conflict(self, cfg_override):
        from msam.core import _apply_query_rewriting
        cfg_override.setdefault("retrieval_v2", {})["enable_query_rewriting"] = True
        cfg_override["retrieval_v2"]["entity_mappings"] = {
            # Override the built-in `user → User` with a specific name.
            "user": "Joe",
        }
        out = _apply_query_rewriting("what does the user think?")
        # User's override wins; the default's "User" replacement is gone.
        assert "Joe" in out
        # "User" might still appear as part of "Joe"... it doesn't, but
        # check explicitly that the lowercase token is gone (replaced).
        assert " user " not in f" {out} "

    def test_user_mapping_can_disable_default(self, cfg_override):
        """Documented escape hatch: override a default with a no-op to
        effectively disable it. Only that exact key is suppressed —
        sibling defaults (`the user`, `user's`) still apply unless
        explicitly overridden too."""
        from msam.core import _apply_query_rewriting
        cfg_override.setdefault("retrieval_v2", {})["enable_query_rewriting"] = True
        cfg_override["retrieval_v2"]["entity_mappings"] = {
            "user": "user",  # no-op override of the bare "user" mapping
        }
        # Use phrasing that only triggers the bare `user` mapping
        # (not `the user`).
        out = _apply_query_rewriting("what does user think?")
        assert "User" not in out


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


# ─── Contextual query rewriting (production-only) ────────────────────────

class TestContextualRewrite:
    """Production-only feature: agents may pass prior conversation
    messages so MSAM can resolve references like 'yes, look for that'
    into self-contained queries via an LLM. Default-off; no-op when
    context is None/empty regardless of flag (so the bench harness pays
    nothing).
    """

    def test_no_context_is_noop(self, cfg_override):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        assert _resolve_contextual_query("yes, look for that", None) == \
            "yes, look for that"
        assert _resolve_contextual_query("yes, look for that", []) == \
            "yes, look for that"

    def test_flag_off_is_noop(self, cfg_override):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        ctx = [
            {"role": "user", "content": "Tell me about my Sony headphones"},
            {"role": "assistant", "content": "You bought WH-1000XM5 in Boston."},
        ]
        assert _resolve_contextual_query("yes, look for that", ctx) == \
            "yes, look for that"

    def test_no_api_key_is_noop(self, cfg_override, monkeypatch):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "", "timeout": 5},
        )
        ctx = [{"role": "user", "content": "Tell me about my headphones"}]
        assert _resolve_contextual_query("yes, that one", ctx) == "yes, that one"

    def test_llm_rewrite_replaces_query(self, cfg_override, monkeypatch):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )

        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": "What do I know about Sony WH-1000XM5 headphones?"
                }}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())

        ctx = [
            {"role": "user", "content": "Tell me about my Sony WH-1000XM5"},
            {"role": "assistant", "content": "You bought them in Boston for $399."},
        ]
        out = _resolve_contextual_query("yes, look for that", ctx)
        assert "Sony WH-1000XM5" in out

    def test_llm_failure_returns_original(self, cfg_override, monkeypatch):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        def _boom(*a, **kw): raise RuntimeError("network down")
        monkeypatch.setattr("requests.post", _boom)
        ctx = [{"role": "user", "content": "anything"}]
        assert _resolve_contextual_query("yes, that", ctx) == "yes, that"

    def test_strips_wrapping_quotes(self, cfg_override, monkeypatch):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": '"What is my headphone model?"'
                }}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        ctx = [{"role": "user", "content": "my headphones"}]
        out = _resolve_contextual_query("yes, that", ctx)
        assert out == "What is my headphone model?"

    def test_empty_response_returns_original(self, cfg_override, monkeypatch):
        from msam.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "   "}}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        ctx = [{"role": "user", "content": "context"}]
        assert _resolve_contextual_query("yes, that", ctx) == "yes, that"


# ─── P38: confidence-gated HyDE ──────────────────────────────────────────

class TestHydeHelper:
    """The _hyde_query helper itself: gating + LLM mock + failure modes."""

    def test_disabled_returns_none(self, cfg_override):
        from msam.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = False
        assert _hyde_query("What is my profession?") is None

    def test_no_api_key_returns_none(self, cfg_override, monkeypatch):
        from msam.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "", "timeout": 5},
        )
        assert _hyde_query("What is my profession?") is None

    def test_returns_hypothetical_when_enabled(self, cfg_override, monkeypatch):
        from msam.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )

        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": "I work as a software engineer at TechCorp."
                }}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        out = _hyde_query("What is my profession?")
        assert out == "I work as a software engineer at TechCorp."

    def test_llm_failure_returns_none(self, cfg_override, monkeypatch):
        from msam.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        def _boom(*a, **kw): raise RuntimeError("network down")
        monkeypatch.setattr("requests.post", _boom)
        assert _hyde_query("anything?") is None

    def test_strips_wrapping_quotes(self, cfg_override, monkeypatch):
        from msam.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": '"I prefer dark mode."'
                }}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        assert _hyde_query("dark mode?") == "I prefer dark mode."

    def test_empty_response_returns_none(self, cfg_override, monkeypatch):
        from msam.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "   "}}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        assert _hyde_query("anything?") is None


class TestHydeGating:
    """The gate inside hybrid_retrieve: only fire HyDE when first-pass
    confidence is weak. The gate's job is to keep HyDE's LLM cost off
    the queries where the cheap path already found a confident match.
    """

    def _seed_atom(self, content: str = "irrelevant content for test"):
        from msam.core import store_atom
        return store_atom(content)

    def test_gate_does_not_fire_when_disabled(self, cfg_override, monkeypatch):
        """enable_hyde=False → never call _hyde_query, even on weak first pass."""
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0}
        def _spy(q):
            called["n"] += 1
            return None
        monkeypatch.setattr("msam.core._hyde_query", _spy)

        self._seed_atom()
        hybrid_retrieve("anything", top_k=3)
        assert called["n"] == 0

    def test_gate_fires_on_weak_first_pass(self, cfg_override, monkeypatch):
        """When max similarity in first-pass < trigger AND enable_hyde=True,
        _hyde_query must be called. The fixture's randomized embeddings
        produce low cosine similarity, so the gate naturally trips here.
        """
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        # Fixture's embed_query returns the same vector as embed_text, so
        # cosine sim is 1.0. Use 2.0 to guarantee the gate trips.
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0, "q": None}
        def _spy(q):
            called["n"] += 1
            called["q"] = q
            return None  # simulate LLM returning nothing — gate fires but no rerun
        monkeypatch.setattr("msam.core._hyde_query", _spy)

        self._seed_atom()
        hybrid_retrieve("what is my profession?", top_k=3)
        assert called["n"] == 1
        assert called["q"] == "what is my profession?"

    def test_gate_skips_when_first_pass_is_confident(self, cfg_override, monkeypatch):
        """When the first pass already has a confident match, HyDE should
        be skipped — that's the whole point of the gate.
        """
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = -1.0  # impossibly low → never trip
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0}
        def _spy(q):
            called["n"] += 1
            return None
        monkeypatch.setattr("msam.core._hyde_query", _spy)

        self._seed_atom()
        hybrid_retrieve("anything", top_k=3)
        assert called["n"] == 0

    def test_gate_skips_in_weighted_sum_mode(self, cfg_override, monkeypatch):
        """HyDE is a fusion-only feature. weighted_sum mode is the legacy
        path; we don't bolt new pathways onto it."""
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0
        cfg_override["retrieval"]["fusion"] = "weighted_sum"

        called = {"n": 0}
        monkeypatch.setattr("msam.core._hyde_query", lambda q: called.__setitem__("n", called["n"] + 1) or None)

        self._seed_atom()
        hybrid_retrieve("anything", top_k=3)
        assert called["n"] == 0
