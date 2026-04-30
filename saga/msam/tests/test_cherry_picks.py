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
        hybrid_retrieve("what is my profession?", top_k=3)
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
        hybrid_retrieve("what is my profession?", top_k=3)
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
        hybrid_retrieve("what is my profession?", top_k=3)
        assert called["n"] == 0

    def test_gate_skips_for_non_question_queries(self, cfg_override, monkeypatch):
        """HyDE generates a hypothetical *answer* — that only makes sense
        for a query asking something. Statements and commands shouldn't
        trip the gate even when first-pass confidence is weak."""
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0  # force-trip if reached
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0}
        monkeypatch.setattr("msam.core._hyde_query", lambda q: called.__setitem__("n", called["n"] + 1) or None)

        self._seed_atom()
        hybrid_retrieve("save this for later", top_k=3)
        hybrid_retrieve("Yes, please do that", top_k=3)
        hybrid_retrieve("That sounds good", top_k=3)
        assert called["n"] == 0


class TestLooksLikeQuestion:
    """Heuristic question detector that gates HyDE."""

    def test_question_mark(self):
        from msam.core import _looks_like_question
        assert _looks_like_question("Tell me about my dog?") is True
        assert _looks_like_question("really?") is True
        assert _looks_like_question("ok? ") is True

    def test_wh_words(self):
        from msam.core import _looks_like_question
        for q in [
            "what is my profession",
            "Who did I meet last week",
            "When did I buy those headphones",
            "Where do I live",
            "Why did I switch jobs",
            "How does that work",
            "Which option did I pick",
        ]:
            assert _looks_like_question(q) is True, f"failed: {q}"

    def test_aux_verb_starts(self):
        from msam.core import _looks_like_question
        for q in [
            "Is that right",
            "Are we still on for Thursday",
            "Do I own a car",
            "Did I mention the trip",
            "Can you remind me",
            "Have we discussed this",
        ]:
            assert _looks_like_question(q) is True, f"failed: {q}"

    def test_statements_and_commands(self):
        from msam.core import _looks_like_question
        for s in [
            "Yes, please save that",
            "Save this for later",
            "That sounds great",
            "I prefer dark mode",
            "Remember the meeting on Friday",
            "Note: I switched jobs",
        ]:
            assert _looks_like_question(s) is False, f"false-positive: {s}"

    def test_empty_or_whitespace(self):
        from msam.core import _looks_like_question
        assert _looks_like_question("") is False
        assert _looks_like_question("   ") is False
        assert _looks_like_question(None) is False  # type: ignore[arg-type]


class TestCombinedRewriteHyde:
    """Combined contextual rewrite + HyDE in one LLM call.

    When both would fire (rewrite needs context-resolution AND HyDE
    would fire on a question), bundle them. When only one applies, fall
    back to the standalone helpers. When neither applies, no LLM call
    pre-retrieval (HyDE may still fire post-retrieval if its gate is on).
    """

    def _llm_returns(self, monkeypatch, body: str):
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )

        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": body}}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())

    def test_parses_two_section_output(self, cfg_override, monkeypatch):
        from msam.core import _resolve_query_and_hypothetical
        self._llm_returns(monkeypatch,
            "REWRITTEN: What do I know about my Sony WH-1000XM5?\n"
            "HYPOTHETICAL: I bought Sony WH-1000XM5 in Boston for $399."
        )
        ctx = [{"role": "user", "content": "Tell me about my headphones"}]
        rewritten, hyp = _resolve_query_and_hypothetical("yes, look for that", ctx)
        assert rewritten == "What do I know about my Sony WH-1000XM5?"
        assert hyp == "I bought Sony WH-1000XM5 in Boston for $399."

    def test_no_context_returns_passthrough(self, cfg_override, monkeypatch):
        from msam.core import _resolve_query_and_hypothetical
        # No LLM should be hit when there's no context to resolve against.
        called = {"n": 0}
        monkeypatch.setattr("requests.post", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or None)
        rewritten, hyp = _resolve_query_and_hypothetical("anything", None)
        assert rewritten == "anything"
        assert hyp is None
        assert called["n"] == 0

    def test_llm_failure_returns_originals(self, cfg_override, monkeypatch):
        from msam.core import _resolve_query_and_hypothetical
        monkeypatch.setattr(
            "msam.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        def _boom(*a, **kw): raise RuntimeError("network down")
        monkeypatch.setattr("requests.post", _boom)
        ctx = [{"role": "user", "content": "context here"}]
        rewritten, hyp = _resolve_query_and_hypothetical("yes that", ctx)
        assert rewritten == "yes that"
        assert hyp is None

    def test_partial_response_keeps_what_parsed(self, cfg_override, monkeypatch):
        """If only REWRITTEN parses cleanly (no HYPOTHETICAL section),
        return rewritten + None for the hypothetical."""
        from msam.core import _resolve_query_and_hypothetical
        self._llm_returns(monkeypatch,
            "REWRITTEN: What about Italy?"
        )
        ctx = [{"role": "user", "content": "Tell me about Italy"}]
        rewritten, hyp = _resolve_query_and_hypothetical("tell me more", ctx)
        assert rewritten == "What about Italy?"
        assert hyp is None


class TestRewriteHydeDispatch:
    """The pre-retrieval dispatch in hybrid_retrieve. The key invariant:
    when both rewrite + HyDE conditions are met, we make ONE combined
    LLM call. When only one applies, fall back to that helper. When
    neither applies, skip pre-retrieval LLM entirely.
    """

    def _seed_atom(self):
        from msam.core import store_atom
        return store_atom("dispatch test content for retrieval probe")

    def _track(self, monkeypatch):
        """Monkeypatch the three LLM helpers and return a counter dict."""
        n = {"combined": 0, "rewrite": 0, "hyde": 0}

        def _combined(q, c):
            n["combined"] += 1
            return ("rewritten", "hypothetical")

        def _rewrite(q, c):
            n["rewrite"] += 1
            return q

        def _hyde(q):
            n["hyde"] += 1
            return None

        monkeypatch.setattr("msam.core._resolve_query_and_hypothetical", _combined)
        monkeypatch.setattr("msam.core._resolve_contextual_query", _rewrite)
        monkeypatch.setattr("msam.core._hyde_query", _hyde)
        return n

    def test_combined_path_when_both_apply(self, cfg_override, monkeypatch):
        """Both flags on + context + question → one combined call. No
        standalone rewrite call. No standalone HyDE call (we already
        have the hypothetical from the combined call).
        """
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "Tell me about my headphones"}]
        hybrid_retrieve("what about that?", top_k=3, context=ctx)
        assert n["combined"] == 1
        assert n["rewrite"] == 0
        assert n["hyde"] == 0

    def test_rewrite_only_when_not_a_question(self, cfg_override, monkeypatch):
        """Both flags on + context, but query is a statement → standalone
        rewrite (no HyDE because non-question)."""
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "About a meeting"}]
        hybrid_retrieve("yes, please save that", top_k=3, context=ctx)
        assert n["combined"] == 0
        assert n["rewrite"] == 1
        assert n["hyde"] == 0  # statement → HyDE skipped post-retrieval too

    def test_rewrite_only_when_hyde_disabled(self, cfg_override, monkeypatch):
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "About headphones"}]
        hybrid_retrieve("what about that?", top_k=3, context=ctx)
        assert n["combined"] == 0
        assert n["rewrite"] == 1
        assert n["hyde"] == 0

    def test_hyde_only_when_no_context(self, cfg_override, monkeypatch):
        """HyDE flag on + question, no context → standalone HyDE
        post-retrieval, no combined call, no rewrite call."""
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0  # force-trip
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        hybrid_retrieve("what is my profession?", top_k=3, context=None)
        assert n["combined"] == 0
        assert n["rewrite"] == 0
        assert n["hyde"] == 1

    def test_hyde_only_when_rewrite_disabled(self, cfg_override, monkeypatch):
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "About headphones"}]
        hybrid_retrieve("what is my profession?", top_k=3, context=ctx)
        assert n["combined"] == 0
        assert n["rewrite"] == 0
        assert n["hyde"] == 1

    def test_neither_when_both_off(self, cfg_override, monkeypatch):
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        hybrid_retrieve("what is my profession?", top_k=3)
        assert n == {"combined": 0, "rewrite": 0, "hyde": 0}


class TestRetrievalPathLogging:
    """Each LLM-using path emits a tagged log line on msam.retrieval so
    Mimir / ops can count path frequencies in production logs.
    """

    def _seed_atom(self):
        from msam.core import store_atom
        return store_atom("logging probe content")

    def _stub_helpers(self, monkeypatch):
        """Stub the rewrite + HyDE helpers so we exercise the dispatch
        without real LLM calls."""
        monkeypatch.setattr(
            "msam.core._resolve_query_and_hypothetical",
            lambda q, c: ("rewritten q?", "hypothetical answer"),
        )
        monkeypatch.setattr(
            "msam.core._resolve_contextual_query",
            lambda q, c: "rewritten q",
        )
        monkeypatch.setattr("msam.core._hyde_query", lambda q: "hypothetical answer")

    def test_combined_path_logged(self, cfg_override, monkeypatch, caplog):
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._stub_helpers(monkeypatch)
        self._seed_atom()

        with caplog.at_level("INFO", logger="msam.retrieval"):
            ctx = [{"role": "user", "content": "headphones"}]
            hybrid_retrieve("what about that?", top_k=3, context=ctx)

        msgs = [r.getMessage() for r in caplog.records if r.name == "msam.retrieval"]
        assert any(m.startswith("path=combined") for m in msgs), msgs
        # Combined path also logs the post-retrieval HyDE-pathway source.
        # Stays at DEBUG so it doesn't appear at INFO.

    def test_rewrite_only_path_logged(self, cfg_override, monkeypatch, caplog):
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._stub_helpers(monkeypatch)
        self._seed_atom()

        with caplog.at_level("INFO", logger="msam.retrieval"):
            ctx = [{"role": "user", "content": "context"}]
            hybrid_retrieve("statement here", top_k=3, context=ctx)

        msgs = [r.getMessage() for r in caplog.records if r.name == "msam.retrieval"]
        assert any(m == "path=rewrite_only" for m in msgs), msgs

    def test_hyde_gated_fired_logged(self, cfg_override, monkeypatch, caplog):
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0  # force trip
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._stub_helpers(monkeypatch)
        self._seed_atom()

        with caplog.at_level("INFO", logger="msam.retrieval"):
            hybrid_retrieve("what is my profession?", top_k=3)

        msgs = [r.getMessage() for r in caplog.records if r.name == "msam.retrieval"]
        # Includes 'fired=yes' since the stubbed _hyde_query returns text.
        assert any(m.startswith("path=hyde_gated fired=yes") for m in msgs), msgs

    def test_no_log_when_no_llm_path(self, cfg_override, monkeypatch, caplog):
        """The common bench/CLI case: no flags on, no context. Nothing
        should be logged at INFO — keeps high-volume retrieval quiet."""
        from msam.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._seed_atom()

        with caplog.at_level("INFO", logger="msam.retrieval"):
            hybrid_retrieve("what is my profession?", top_k=3)

        msgs = [r.getMessage() for r in caplog.records if r.name == "msam.retrieval"]
        assert msgs == [], f"unexpected INFO logs: {msgs}"
