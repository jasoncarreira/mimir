"""Tests for ``mimir.agent._resolve_model`` and ``_supports_responses_api``.

Covers the model spec → BaseChatModel translation, plus the OpenAI
Responses-API gating heuristic. We don't actually hit any network or
spawn a subprocess — tests patch ``init_chat_model`` and lazy-load
ChatClaudeCode only when present (skipped otherwise).
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel

from mimir.agent import _resolve_model, _supports_responses_api


# ─── Responses API heuristic ────────────────────────────────────────


class TestSupportsResponsesAPI:
    def test_defaults_true_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert _supports_responses_api() is True

    def test_true_for_api_openai_com(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        assert _supports_responses_api() is True

    def test_false_for_third_party_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drop-in proxies like Groq / Together / DeepSeek typically only
        # implement /chat/completions; Responses returns 404.
        monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
        assert _supports_responses_api() is False

    def test_env_override_forces_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_USE_RESPONSES_API", "1")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
        assert _supports_responses_api() is True

    def test_env_override_forces_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_USE_RESPONSES_API", "0")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert _supports_responses_api() is False

    def test_substring_attack_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Crafted host that contains ``api.openai.com`` as a substring of
        # a different parent domain must NOT trigger the flag. The
        # previous ``in`` check accepted this; the urlparse-based
        # hostname comparison rejects it.
        monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com.evil.example/v1")
        assert _supports_responses_api() is False


# ─── _resolve_model paths ──────────────────────────────────────────


class TestResolveModelPassthrough:
    def test_basechatmodel_instance_passthrough(self) -> None:
        class _Fake(BaseChatModel):
            def _generate(self, *args: Any, **kwargs: Any) -> Any:
                raise NotImplementedError

            @property
            def _llm_type(self) -> str:
                return "fake"

        m = _Fake()
        assert _resolve_model(m) is m

    def test_invalid_spec_type_raises(self) -> None:
        with pytest.raises(TypeError):
            _resolve_model(123)  # type: ignore[arg-type]


class TestResolveModelInitChat:
    """For non-claude-code specs we patch init_chat_model so we can
    inspect what kwargs were threaded through."""

    def test_passes_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["spec"] = spec
            captured["kwargs"] = kwargs
            return "MODEL"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        out = _resolve_model("anthropic:claude-haiku-4-5", max_retries=3)
        assert out == "MODEL"
        assert captured["spec"] == "anthropic:claude-haiku-4-5"
        assert captured["kwargs"]["max_retries"] == 3
        # responses_api flag is OpenAI-only
        assert "use_responses_api" not in captured["kwargs"]

    def test_clamps_negative_retries_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        _resolve_model("anthropic:foo", max_retries=-5)
        assert captured["kwargs"]["max_retries"] == 0

    def test_openai_at_api_openai_sets_responses_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)
        _resolve_model("openai:gpt-5.4-nano")
        assert captured["kwargs"].get("use_responses_api") is True

    def test_openai_at_proxy_skips_responses_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
        monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)
        _resolve_model("openai:foo")
        assert "use_responses_api" not in captured["kwargs"]

    def test_non_openai_provider_no_responses_api_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        # Even on api.openai.com, anthropic provider shouldn't get the flag.
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        _resolve_model("anthropic:claude-haiku-4-5")
        assert "use_responses_api" not in captured["kwargs"]

    def test_passes_max_tokens_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        # Thinking-via-Anthropic-compat models (Minimax / Kimi) need a raised
        # output cap so reasoning blocks don't eat the whole budget (the M3
        # fix — without it the turn hits max_tokens mid-thought, empty reply).
        _resolve_model("anthropic:MiniMax-M3", max_tokens=32768)
        assert captured["kwargs"]["max_tokens"] == 32768

    def test_omits_max_tokens_when_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        # 0 (the default) means "leave the provider default" — never pass it.
        _resolve_model("anthropic:claude-haiku-4-5", max_tokens=0)
        assert "max_tokens" not in captured["kwargs"]

    def test_omits_max_tokens_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_init(spec: str, **kwargs: Any) -> str:
            captured["kwargs"] = kwargs
            return "M"

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
        _resolve_model("anthropic:claude-haiku-4-5")
        assert "max_tokens" not in captured["kwargs"]


class TestResolveModelClaudeCode:
    def test_claude_code_path_returns_chat_claude_code(self) -> None:
        # claude-code path doesn't accept max_retries; just confirm we get
        # back a ChatClaudeCode instance regardless of the kwarg.
        try:
            from langchain_claude_code import ChatClaudeCode
        except ImportError:
            pytest.skip("langchain-claude-code extra not installed")
        m = _resolve_model("claude-code:claude-sonnet-4-6", max_retries=12)
        assert isinstance(m, ChatClaudeCode)
        assert m.model == "claude-sonnet-4-6"


# ─── Config integration ────────────────────────────────────────────


def test_config_default_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.delenv("MIMIR_MODEL_MAX_RETRIES", raising=False)
    cfg = Config.from_env()
    assert cfg.model_max_retries == 6


def test_config_max_retries_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.setenv("MIMIR_MODEL_MAX_RETRIES", "12")
    cfg = Config.from_env()
    assert cfg.model_max_retries == 12


def test_config_default_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.delenv("MIMIR_MODEL_MAX_TOKENS", raising=False)
    cfg = Config.from_env()
    assert cfg.model_max_tokens == 0


def test_config_max_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.setenv("MIMIR_MODEL_MAX_TOKENS", "32768")
    cfg = Config.from_env()
    assert cfg.model_max_tokens == 32768
