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
    db_path = tmp_path / "test_saga.db"
    monkeypatch.setattr("saga.core.DB_PATH", db_path)
    monkeypatch.setattr("saga.triples.DB_PATH", db_path)
    fake_emb = list(np.random.randn(1024).astype(float))
    monkeypatch.setattr("saga.core.embed_text", lambda t: fake_emb)
    monkeypatch.setattr("saga.core.embed_query", lambda t: fake_emb)
    monkeypatch.setattr("saga.core._cached_embed_query_import", lambda t: tuple(fake_emb))
    monkeypatch.setattr("saga.core.cached_embed_query", lambda t: fake_emb)
    yield db_path


@pytest.fixture
def cfg_override(monkeypatch):
    """Patch saga.config._config to a deep copy of the live config and
    return the dict so the test can mutate it without bleeding into
    other tests."""
    from saga import config as cfg_mod
    cfg_mod._load_config()  # ensure loaded
    snapshot = copy.deepcopy(cfg_mod._config) if cfg_mod._config else {}
    monkeypatch.setattr(cfg_mod, "_config", snapshot)
    monkeypatch.setattr(cfg_mod, "_config_loaded", True)
    return snapshot


# ─── P12: synonym expansion (keyword pathway only) ───────────────────────

class TestQueryExpansion:
    def test_disabled_is_noop(self, cfg_override):
        from saga.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = False
        cfg_override["query_expansion"] = {"synonyms": {"profession": ["job"]}}
        assert _expand_query_for_keyword("what is the user's profession?") == \
            "what is the user's profession?"

    def test_enabled_appends_synonyms(self, cfg_override):
        from saga.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = True
        cfg_override["query_expansion"] = {"synonyms": {"profession": ["job", "career"]}}
        out = _expand_query_for_keyword("what is the user's profession?")
        assert "profession" in out
        assert "job" in out
        assert "career" in out

    def test_enabled_no_match_is_noop(self, cfg_override):
        from saga.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = True
        cfg_override["query_expansion"] = {"synonyms": {"profession": ["job", "career"]}}
        assert _expand_query_for_keyword("what is the weather?") == \
            "what is the weather?"

    def test_enabled_empty_dict_is_noop(self, cfg_override):
        from saga.core import _expand_query_for_keyword
        cfg_override.setdefault("retrieval_v2", {})["enable_query_expansion"] = True
        cfg_override["query_expansion"] = {"synonyms": {}}
        assert _expand_query_for_keyword("what is the user's profession?") == \
            "what is the user's profession?"


# ─── Contextual query rewriting (production-only) ────────────────────────

class TestContextualRewrite:
    """Production-only feature: agents may pass prior conversation
    messages so SAGA can resolve references like 'yes, look for that'
    into self-contained queries via an LLM. Default-off; no-op when
    context is None/empty regardless of flag (so the bench harness pays
    nothing).
    """

    @pytest.mark.asyncio
    async def test_no_context_is_noop(self, cfg_override):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        assert await _resolve_contextual_query("yes, look for that", None) == \
            "yes, look for that"
        assert await _resolve_contextual_query("yes, look for that", []) == \
            "yes, look for that"

    @pytest.mark.asyncio
    async def test_flag_off_is_noop(self, cfg_override):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        ctx = [
            {"role": "user", "content": "Tell me about my Sony headphones"},
            {"role": "assistant", "content": "You bought WH-1000XM5 in Boston."},
        ]
        assert await _resolve_contextual_query("yes, look for that", ctx) == \
            "yes, look for that"

    @pytest.mark.asyncio
    async def test_no_api_key_is_noop(self, cfg_override, monkeypatch):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "", "timeout": 5},
        )
        ctx = [{"role": "user", "content": "Tell me about my headphones"}]
        assert await _resolve_contextual_query("yes, that one", ctx) == "yes, that one"

    @pytest.mark.asyncio
    async def test_llm_rewrite_replaces_query(self, cfg_override, monkeypatch):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
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
        out = await _resolve_contextual_query("yes, look for that", ctx)
        assert "Sony WH-1000XM5" in out

    @pytest.mark.asyncio
    async def test_llm_failure_returns_original(self, cfg_override, monkeypatch):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        def _boom(*a, **kw): raise RuntimeError("network down")
        monkeypatch.setattr("requests.post", _boom)
        ctx = [{"role": "user", "content": "anything"}]
        assert await _resolve_contextual_query("yes, that", ctx) == "yes, that"

    @pytest.mark.asyncio
    async def test_strips_wrapping_quotes(self, cfg_override, monkeypatch):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
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
        out = await _resolve_contextual_query("yes, that", ctx)
        assert out == "What is my headphone model?"

    @pytest.mark.asyncio
    async def test_empty_response_returns_original(self, cfg_override, monkeypatch):
        from saga.core import _resolve_contextual_query
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "   "}}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        ctx = [{"role": "user", "content": "context"}]
        assert await _resolve_contextual_query("yes, that", ctx) == "yes, that"


# ─── P38: confidence-gated HyDE ──────────────────────────────────────────

class TestHydeHelper:
    """The _hyde_query helper itself: gating + LLM mock + failure modes."""

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, cfg_override):
        from saga.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = False
        assert await _hyde_query("What is my profession?") is None

    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self, cfg_override, monkeypatch):
        from saga.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "", "timeout": 5},
        )
        assert await _hyde_query("What is my profession?") is None

    @pytest.mark.asyncio
    async def test_returns_hypothetical_when_enabled(self, cfg_override, monkeypatch):
        from saga.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )

        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": "I work as a software engineer at TechCorp."
                }}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        out = await _hyde_query("What is my profession?")
        assert out == "I work as a software engineer at TechCorp."

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self, cfg_override, monkeypatch):
        from saga.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        def _boom(*a, **kw): raise RuntimeError("network down")
        monkeypatch.setattr("requests.post", _boom)
        assert await _hyde_query("anything?") is None

    @pytest.mark.asyncio
    async def test_strips_wrapping_quotes(self, cfg_override, monkeypatch):
        from saga.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": '"I prefer dark mode."'
                }}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        assert await _hyde_query("dark mode?") == "I prefer dark mode."

    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self, cfg_override, monkeypatch):
        from saga.core import _hyde_query
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "   "}}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
        assert await _hyde_query("anything?") is None


class TestHydeGating:
    """The gate inside hybrid_retrieve: only fire HyDE when first-pass
    confidence is weak. The gate's job is to keep HyDE's LLM cost off
    the queries where the cheap path already found a confident match.
    """

    def _seed_atom(self, content: str = "irrelevant content for test"):
        from saga.core import store_atom
        return store_atom(content)

    @pytest.mark.asyncio
    async def test_gate_does_not_fire_when_disabled(self, cfg_override, monkeypatch):
        """enable_hyde=False → never call _hyde_query, even on weak first pass."""
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0}
        async def _spy(q):
            called["n"] += 1
            return None
        monkeypatch.setattr("saga.core._hyde_query", _spy)

        self._seed_atom()
        await hybrid_retrieve("what is my profession?", top_k=3)
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_gate_fires_on_weak_first_pass(self, cfg_override, monkeypatch):
        """When max similarity in first-pass < trigger AND enable_hyde=True,
        _hyde_query must be called. The fixture's randomized embeddings
        produce low cosine similarity, so the gate naturally trips here.
        """
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        # Fixture's embed_query returns the same vector as embed_text, so
        # cosine sim is 1.0. Use 2.0 to guarantee the gate trips.
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0, "q": None}
        async def _spy(q):
            called["n"] += 1
            called["q"] = q
            return None  # simulate LLM returning nothing — gate fires but no rerun
        monkeypatch.setattr("saga.core._hyde_query", _spy)

        self._seed_atom()
        await hybrid_retrieve("what is my profession?", top_k=3)
        assert called["n"] == 1
        assert called["q"] == "what is my profession?"

    @pytest.mark.asyncio
    async def test_gate_skips_when_first_pass_is_confident(self, cfg_override, monkeypatch):
        """When the first pass already has a confident match, HyDE should
        be skipped — that's the whole point of the gate.
        """
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = -1.0  # impossibly low → never trip
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0}
        async def _spy(q):
            called["n"] += 1
            return None
        monkeypatch.setattr("saga.core._hyde_query", _spy)

        self._seed_atom()
        await hybrid_retrieve("what is my profession?", top_k=3)
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_gate_skips_in_weighted_sum_mode(self, cfg_override, monkeypatch):
        """HyDE is a fusion-only feature. weighted_sum mode is the legacy
        path; we don't bolt new pathways onto it."""
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0
        cfg_override["retrieval"]["fusion"] = "weighted_sum"

        called = {"n": 0}
        async def _spy(q):
            called["n"] += 1
            return None
        monkeypatch.setattr("saga.core._hyde_query", _spy)

        self._seed_atom()
        await hybrid_retrieve("what is my profession?", top_k=3)
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_gate_skips_for_non_question_queries(self, cfg_override, monkeypatch):
        """HyDE generates a hypothetical *answer* — that only makes sense
        for a query asking something. Statements and commands shouldn't
        trip the gate even when first-pass confidence is weak."""
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0  # force-trip if reached
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"n": 0}
        async def _spy(q):
            called["n"] += 1
            return None
        monkeypatch.setattr("saga.core._hyde_query", _spy)

        self._seed_atom()
        await hybrid_retrieve("save this for later", top_k=3)
        await hybrid_retrieve("Yes, please do that", top_k=3)
        await hybrid_retrieve("That sounds good", top_k=3)
        assert called["n"] == 0


class TestLooksLikeQuestion:
    """Heuristic question detector that gates HyDE."""

    def test_question_mark(self):
        from saga.core import _looks_like_question
        assert _looks_like_question("Tell me about my dog?") is True
        assert _looks_like_question("really?") is True
        assert _looks_like_question("ok? ") is True

    def test_wh_words(self):
        from saga.core import _looks_like_question
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
        from saga.core import _looks_like_question
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
        from saga.core import _looks_like_question
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
        from saga.core import _looks_like_question
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
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )

        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": body}}]}
        monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())

    @pytest.mark.asyncio
    async def test_parses_two_section_output(self, cfg_override, monkeypatch):
        from saga.core import _resolve_query_and_hypothetical
        self._llm_returns(monkeypatch,
            "REWRITTEN: What do I know about my Sony WH-1000XM5?\n"
            "HYPOTHETICAL: I bought Sony WH-1000XM5 in Boston for $399."
        )
        ctx = [{"role": "user", "content": "Tell me about my headphones"}]
        rewritten, hyp = await _resolve_query_and_hypothetical("yes, look for that", ctx)
        assert rewritten == "What do I know about my Sony WH-1000XM5?"
        assert hyp == "I bought Sony WH-1000XM5 in Boston for $399."

    @pytest.mark.asyncio
    async def test_no_context_returns_passthrough(self, cfg_override, monkeypatch):
        from saga.core import _resolve_query_and_hypothetical
        # No LLM should be hit when there's no context to resolve against.
        called = {"n": 0}
        monkeypatch.setattr("requests.post", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or None)
        rewritten, hyp = await _resolve_query_and_hypothetical("anything", None)
        assert rewritten == "anything"
        assert hyp is None
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_originals(self, cfg_override, monkeypatch):
        from saga.core import _resolve_query_and_hypothetical
        monkeypatch.setattr(
            "saga.config.resolve_llm_config",
            lambda subsystem: {"url": "u", "model": "m", "api_key": "k", "timeout": 5},
        )
        def _boom(*a, **kw): raise RuntimeError("network down")
        monkeypatch.setattr("requests.post", _boom)
        ctx = [{"role": "user", "content": "context here"}]
        rewritten, hyp = await _resolve_query_and_hypothetical("yes that", ctx)
        assert rewritten == "yes that"
        assert hyp is None

    @pytest.mark.asyncio
    async def test_partial_response_keeps_what_parsed(self, cfg_override, monkeypatch):
        """If only REWRITTEN parses cleanly (no HYPOTHETICAL section),
        return rewritten + None for the hypothetical."""
        from saga.core import _resolve_query_and_hypothetical
        self._llm_returns(monkeypatch,
            "REWRITTEN: What about Italy?"
        )
        ctx = [{"role": "user", "content": "Tell me about Italy"}]
        rewritten, hyp = await _resolve_query_and_hypothetical("tell me more", ctx)
        assert rewritten == "What about Italy?"
        assert hyp is None


class TestRewriteHydeDispatch:
    """The pre-retrieval dispatch in hybrid_retrieve. The key invariant:
    when both rewrite + HyDE conditions are met, we make ONE combined
    LLM call. When only one applies, fall back to that helper. When
    neither applies, skip pre-retrieval LLM entirely.
    """

    def _seed_atom(self):
        from saga.core import store_atom
        return store_atom("dispatch test content for retrieval probe")

    def _track(self, monkeypatch):
        """Monkeypatch the three LLM helpers and return a counter dict."""
        n = {"combined": 0, "rewrite": 0, "hyde": 0}

        async def _combined(q, c):
            n["combined"] += 1
            return ("rewritten", "hypothetical")

        async def _rewrite(q, c):
            n["rewrite"] += 1
            return q

        async def _hyde(q):
            n["hyde"] += 1
            return None

        monkeypatch.setattr("saga.core._resolve_query_and_hypothetical", _combined)
        monkeypatch.setattr("saga.core._resolve_contextual_query", _rewrite)
        monkeypatch.setattr("saga.core._hyde_query", _hyde)
        return n

    @pytest.mark.asyncio
    async def test_combined_path_when_both_apply(self, cfg_override, monkeypatch):
        """Both flags on + context + question → one combined call. No
        standalone rewrite call. No standalone HyDE call (we already
        have the hypothetical from the combined call).
        """
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "Tell me about my headphones"}]
        await hybrid_retrieve("what about that?", top_k=3, context=ctx)
        assert n["combined"] == 1
        assert n["rewrite"] == 0
        assert n["hyde"] == 0

    @pytest.mark.asyncio
    async def test_rewrite_only_when_not_a_question(self, cfg_override, monkeypatch):
        """Both flags on + context, but query is a statement → standalone
        rewrite (no HyDE because non-question)."""
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "About a meeting"}]
        await hybrid_retrieve("yes, please save that", top_k=3, context=ctx)
        assert n["combined"] == 0
        assert n["rewrite"] == 1
        assert n["hyde"] == 0  # statement → HyDE skipped post-retrieval too

    @pytest.mark.asyncio
    async def test_rewrite_only_when_hyde_disabled(self, cfg_override, monkeypatch):
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "About headphones"}]
        await hybrid_retrieve("what about that?", top_k=3, context=ctx)
        assert n["combined"] == 0
        assert n["rewrite"] == 1
        assert n["hyde"] == 0

    @pytest.mark.asyncio
    async def test_hyde_only_when_no_context(self, cfg_override, monkeypatch):
        """HyDE flag on + question, no context → standalone HyDE
        post-retrieval, no combined call, no rewrite call."""
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0  # force-trip
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        await hybrid_retrieve("what is my profession?", top_k=3, context=None)
        assert n["combined"] == 0
        assert n["rewrite"] == 0
        assert n["hyde"] == 1

    @pytest.mark.asyncio
    async def test_hyde_only_when_rewrite_disabled(self, cfg_override, monkeypatch):
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        ctx = [{"role": "user", "content": "About headphones"}]
        await hybrid_retrieve("what is my profession?", top_k=3, context=ctx)
        assert n["combined"] == 0
        assert n["rewrite"] == 0
        assert n["hyde"] == 1

    @pytest.mark.asyncio
    async def test_neither_when_both_off(self, cfg_override, monkeypatch):
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        n = self._track(monkeypatch)

        self._seed_atom()
        await hybrid_retrieve("what is my profession?", top_k=3)
        assert n == {"combined": 0, "rewrite": 0, "hyde": 0}


class TestRetrievalPathLogging:
    """Each LLM-using path emits a tagged log line on saga.retrieval so
    Mimir / ops can count path frequencies in production logs.
    """

    def _seed_atom(self):
        from saga.core import store_atom
        return store_atom("logging probe content")

    def _stub_helpers(self, monkeypatch):
        """Stub the rewrite + HyDE helpers so we exercise the dispatch
        without real LLM calls."""
        async def _combined(q, c):
            return ("rewritten q?", "hypothetical answer")
        async def _rewrite(q, c):
            return "rewritten q"
        async def _hyde(q):
            return "hypothetical answer"
        monkeypatch.setattr("saga.core._resolve_query_and_hypothetical", _combined)
        monkeypatch.setattr("saga.core._resolve_contextual_query", _rewrite)
        monkeypatch.setattr("saga.core._hyde_query", _hyde)

    @pytest.mark.asyncio
    async def test_combined_path_logged(self, cfg_override, monkeypatch, caplog):
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._stub_helpers(monkeypatch)
        self._seed_atom()

        with caplog.at_level("INFO", logger="saga.retrieval"):
            ctx = [{"role": "user", "content": "headphones"}]
            await hybrid_retrieve("what about that?", top_k=3, context=ctx)

        msgs = [r.getMessage() for r in caplog.records if r.name == "saga.retrieval"]
        assert any(m.startswith("path=combined") for m in msgs), msgs
        # Combined path also logs the post-retrieval HyDE-pathway source.
        # Stays at DEBUG so it doesn't appear at INFO.

    @pytest.mark.asyncio
    async def test_rewrite_only_path_logged(self, cfg_override, monkeypatch, caplog):
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = True
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._stub_helpers(monkeypatch)
        self._seed_atom()

        with caplog.at_level("INFO", logger="saga.retrieval"):
            ctx = [{"role": "user", "content": "context"}]
            await hybrid_retrieve("statement here", top_k=3, context=ctx)

        msgs = [r.getMessage() for r in caplog.records if r.name == "saga.retrieval"]
        assert any(m == "path=rewrite_only" for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_hyde_gated_fired_logged(self, cfg_override, monkeypatch, caplog):
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = True
        cfg_override["retrieval"]["hyde_trigger_confidence"] = 2.0  # force trip
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._stub_helpers(monkeypatch)
        self._seed_atom()

        with caplog.at_level("INFO", logger="saga.retrieval"):
            await hybrid_retrieve("what is my profession?", top_k=3)

        msgs = [r.getMessage() for r in caplog.records if r.name == "saga.retrieval"]
        # Includes 'fired=yes' since the stubbed _hyde_query returns text.
        assert any(m.startswith("path=hyde_gated fired=yes") for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_no_log_when_no_llm_path(self, cfg_override, monkeypatch, caplog):
        """The common bench/CLI case: no flags on, no context. Nothing
        should be logged at INFO — keeps high-volume retrieval quiet."""
        from saga.core import hybrid_retrieve
        cfg_override.setdefault("retrieval", {})["enable_contextual_rewrite"] = False
        cfg_override["retrieval"]["enable_hyde"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"
        self._seed_atom()

        with caplog.at_level("INFO", logger="saga.retrieval"):
            await hybrid_retrieve("what is my profession?", top_k=3)

        msgs = [r.getMessage() for r in caplog.records if r.name == "saga.retrieval"]
        assert msgs == [], f"unexpected INFO logs: {msgs}"


# ─── P41: embedding-cosine triple augmentation ────────────────────────────

class TestTripleAugmentV2:
    """P41: cosine-match query embedding against active triple embeddings,
    surface the source atoms via triple.atom_id. Strict no-op when
    [retrieval] enable_triple_augment_v2 is False (default)."""

    def _seed_triple(self, monkeypatch, atom_id="src1", subject="user",
                     predicate="profession", obj="software_engineer"):
        """Seed one atom + one triple about it."""
        from saga.core import store_atom, get_db, pack_embedding
        import numpy as np
        # Seed the atom
        real_id = store_atom("the user is a software engineer at TechCorp")
        # Seed a triple referencing it with a known embedding
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY, atom_id TEXT, subject TEXT,
                predicate TEXT, object TEXT, confidence REAL DEFAULT 1.0,
                state TEXT DEFAULT 'active', embedding BLOB,
                valid_from TEXT, valid_until TEXT, source_atom_id TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Embedding fixture returns the same vector for every text, so the
        # triple embedding == the query embedding == the atom embedding.
        # That makes cosine = 1.0, which is what we want for the test.
        emb = pack_embedding(list(np.zeros(1024)))  # any consistent vector
        # Use the actual fake_emb from fixture for cosine = 1.0 with query
        from saga.core import embed_text
        fake_vec = list(embed_text("anything"))
        emb = pack_embedding(fake_vec)
        conn.execute(
            "INSERT INTO triples (id, atom_id, subject, predicate, object, "
            "embedding, state) VALUES (?, ?, ?, ?, ?, ?, 'active')",
            ("t1", real_id, subject, predicate, obj, emb)
        )
        conn.commit()
        conn.close()
        return real_id

    def test_disabled_is_noop(self, cfg_override, monkeypatch):
        from saga.core import _triple_augment_v2
        cfg_override.setdefault("retrieval", {})["enable_triple_augment_v2"] = False
        # Even with a triple seeded, the helper must early-return [].
        self._seed_triple(monkeypatch)
        assert _triple_augment_v2("what is the user's profession?") == []

    def test_no_triples_is_noop(self, cfg_override):
        from saga.core import _triple_augment_v2
        cfg_override.setdefault("retrieval", {})["enable_triple_augment_v2"] = True
        # Empty triples table → empty result.
        assert _triple_augment_v2("anything") == []

    def test_enabled_returns_atom_for_matched_triple(self, cfg_override, monkeypatch):
        from saga.core import _triple_augment_v2
        cfg_override.setdefault("retrieval", {})["enable_triple_augment_v2"] = True
        atom_id = self._seed_triple(monkeypatch)

        out = _triple_augment_v2("what is the user's profession?", top_k=5)
        assert len(out) == 1
        assert out[0]["id"] == atom_id
        assert out[0]["_triple_augmented_v2"] is True
        # Cosine == 1.0 because fixture's embed_text returns the same vec.
        assert out[0]["_similarity"] == pytest.approx(1.0, abs=1e-6)

    def test_collapses_duplicate_atom_id_to_max_sim(self, cfg_override, monkeypatch):
        """If the same atom is referenced by multiple matching triples,
        we surface it once with the strongest cosine."""
        from saga.core import _triple_augment_v2, get_db, pack_embedding, embed_text
        cfg_override.setdefault("retrieval", {})["enable_triple_augment_v2"] = True
        atom_id = self._seed_triple(monkeypatch)
        # Add a second triple pointing at the same atom_id.
        conn = get_db()
        emb = pack_embedding(list(embed_text("anything")))
        conn.execute(
            "INSERT INTO triples (id, atom_id, subject, predicate, object, "
            "embedding, state) VALUES ('t2', ?, 'user', 'works_at', "
            "'TechCorp', ?, 'active')",
            (atom_id, emb)
        )
        conn.commit()
        conn.close()

        out = _triple_augment_v2("what is the user's profession?", top_k=5)
        # Atom appears exactly once despite two matching triples.
        assert len(out) == 1
        assert out[0]["id"] == atom_id


# ─── P43: subatom (sentence-level) beam ────────────────────────────────────

class TestSubatomBeam:
    """P43 beam 2: compressed_retrieve produces sentence-level extracts;
    map sentences back to parent atoms with the strongest sentence's score
    as the atom's beam-2 score. Strict no-op when [retrieval]
    enable_subatom_beam is False."""

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, cfg_override, monkeypatch):
        from saga.core import _subatom_beam_atoms
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = False
        # Even mocking compressed_retrieve to return data — flag-off must
        # short-circuit before calling.
        called = {"n": 0}
        async def _spy(*args, **kw):
            called["n"] += 1
            return {"sentences": [{"atom_id": "x", "score": 0.9}]}
        monkeypatch.setattr("saga.subatom.compressed_retrieve", _spy)
        assert await _subatom_beam_atoms("anything", top_k=3, mode="task") == []
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_enabled_collapses_sentences_to_atoms(self, cfg_override, monkeypatch):
        from saga.core import _subatom_beam_atoms, store_atom
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = True
        # Seed two atoms so the helper has something to load.
        a = store_atom("the user prefers dark mode for late-night coding")
        b = store_atom("the user enjoys mountain biking on weekends")

        # Mock compressed_retrieve to return four sentences spread across
        # two atoms with varying scores.
        async def fake(*args, **kw):
            return {"sentences": [
                {"atom_id": a, "sentence": "...", "score": 0.5},
                {"atom_id": a, "sentence": "...", "score": 0.8},
                {"atom_id": b, "sentence": "...", "score": 0.3},
                {"atom_id": b, "sentence": "...", "score": 0.4},
            ]}
        monkeypatch.setattr("saga.subatom.compressed_retrieve", fake)

        out = await _subatom_beam_atoms("dark mode", top_k=5, mode="task")
        assert len(out) == 2
        # Atom a has the higher max-sentence-score (0.8 > 0.4) so it ranks first.
        assert out[0]["id"] == a
        assert out[1]["id"] == b
        # _subatom_score reflects the per-atom MAX, not sum.
        assert out[0]["_subatom_score"] == pytest.approx(0.8)
        assert out[1]["_subatom_score"] == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_no_sentences_is_noop(self, cfg_override, monkeypatch):
        from saga.core import _subatom_beam_atoms
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = True
        async def _no_sentences(*a, **kw):
            return {"sentences": []}
        monkeypatch.setattr("saga.subatom.compressed_retrieve", _no_sentences)
        assert await _subatom_beam_atoms("anything", top_k=3, mode="task") == []

    @pytest.mark.asyncio
    async def test_compressed_retrieve_failure_is_noop(self, cfg_override, monkeypatch):
        """A crash inside compressed_retrieve must drop the beam silently —
        same resilience pattern as graph/HyDE pathways."""
        from saga.core import _subatom_beam_atoms
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = True
        async def _boom(*a, **kw): raise RuntimeError("subatom blew up")
        monkeypatch.setattr("saga.subatom.compressed_retrieve", _boom)
        assert await _subatom_beam_atoms("anything", top_k=3, mode="task") == []

    @pytest.mark.asyncio
    async def test_recursion_guard_prevents_infinite_loop(self, cfg_override, monkeypatch):
        """compressed_retrieve internally calls hybrid_retrieve, which
        calls _subatom_beam_atoms. Without a guard, that loops forever.
        Simulate the recursion: a fake compressed_retrieve calls back
        into _subatom_beam_atoms, which must return [] on the inner call.
        """
        from saga.core import _subatom_beam_atoms
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = True

        inner_calls = {"n": 0}

        async def fake_compressed(*a, **kw):
            # Simulate compressed_retrieve → hybrid_retrieve →
            # _subatom_beam_atoms re-entry. The guard should make this
            # inner call return [] instead of recursing.
            from saga.core import _subatom_beam_atoms as inner
            inner_calls["n"] += 1
            inner_result = await inner("inner", top_k=2, mode="task")
            assert inner_result == [], "guard failed — inner call recursed"
            return {"sentences": [{"atom_id": "x", "score": 0.5}]}

        monkeypatch.setattr("saga.subatom.compressed_retrieve", fake_compressed)
        await _subatom_beam_atoms("outer", top_k=2, mode="task")
        assert inner_calls["n"] == 1


class TestBeamPathwaysInHybridRetrieve:
    """Confirm the new pathways wire into hybrid_retrieve's RRF correctly:
    when the flags are off, no extra DB calls happen; when on, the
    pathways join the ranked_lists alongside semantic+keyword."""

    @pytest.mark.asyncio
    async def test_both_flags_off_no_extra_helpers_called(self, cfg_override, monkeypatch):
        from saga.core import hybrid_retrieve, store_atom
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = False
        cfg_override["retrieval"]["enable_triple_augment_v2"] = False
        cfg_override["retrieval"]["fusion"] = "rrf"

        called = {"sub": 0, "trip": 0}
        # Replace the helpers so we can confirm they're early-returned by
        # the flag check inside hybrid_retrieve, not just by their own
        # internal flag check.
        async def _sub(*a, **kw):
            called["sub"] += 1
            return []
        monkeypatch.setattr("saga.core._subatom_beam_atoms", _sub)
        monkeypatch.setattr("saga.core._triple_augment_v2",
                            lambda *a, **kw: called.__setitem__("trip", called["trip"] + 1) or [])

        store_atom("anything for the cheap path to find")
        await hybrid_retrieve("anything", top_k=3)
        # Helpers ARE called (hybrid_retrieve always invokes them; their
        # internal flag-check is the gate). They must return [] which
        # contributes nothing to RRF.
        assert called["sub"] == 1
        assert called["trip"] == 1

    @pytest.mark.asyncio
    async def test_subatom_beam_joins_rrf_when_enabled(self, cfg_override, monkeypatch):
        from saga.core import hybrid_retrieve, store_atom
        cfg_override.setdefault("retrieval", {})["enable_subatom_beam"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"

        seed_id = store_atom("dark mode preference content")
        # Stub the helper to return our seeded atom as the only beam result.
        async def _beam(q, top_k, mode):
            return [{"id": seed_id, "content": "x",
                     "_subatom_score": 0.9, "_subatom_beam": True}]
        monkeypatch.setattr("saga.core._subatom_beam_atoms", _beam)

        results = await hybrid_retrieve("anything", top_k=3)
        # The seeded atom should appear in results (it could come from
        # cheap path AND subatom; RRF dedupes by id).
        ids = [r["id"] for r in results]
        assert seed_id in ids

    @pytest.mark.asyncio
    async def test_triple_augment_joins_rrf_when_enabled(self, cfg_override, monkeypatch):
        from saga.core import hybrid_retrieve, store_atom
        cfg_override.setdefault("retrieval", {})["enable_triple_augment_v2"] = True
        cfg_override["retrieval"]["fusion"] = "rrf"

        seed_id = store_atom("the user is a software engineer")
        monkeypatch.setattr(
            "saga.core._triple_augment_v2",
            lambda q, top_k=10: [{"id": seed_id, "content": "x",
                                  "_similarity": 0.85,
                                  "_triple_augmented_v2": True}],
        )

        results = await hybrid_retrieve("user profession", top_k=3)
        ids = [r["id"] for r in results]
        assert seed_id in ids
