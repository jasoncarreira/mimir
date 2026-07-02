"""Tests for ``mimir.agent._resolve_model`` and ``_supports_responses_api``.

Covers the model spec → BaseChatModel translation, plus the OpenAI
Responses-API gating heuristic. We don't actually hit any network or
spawn a subprocess — tests patch ``init_chat_model`` and lazy-load
ChatClaudeCode only when present (skipped otherwise).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel

from mimir import _langchain_claude_code_patches as lcc_patches
from mimir.agent import _resolve_model, _supports_responses_api, resolve_model_from_config


def _raise_package_not_found(name: str) -> None:
    raise lcc_patches.importlib_metadata.PackageNotFoundError(name)


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
    def test_claude_code_path_returns_chat_claude_code(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # claude-code path doesn't accept max_retries; just confirm we get
        # back a ChatClaudeCode instance regardless of the kwarg.
        try:
            from langchain_claude_code import ChatClaudeCode
        except ImportError:
            pytest.skip("langchain-claude-code extra not installed")
        monkeypatch.setattr(
            lcc_patches, "ensure_tool_enforcement_hooks_installed", lambda *_a, **_kw: None
        )
        m = _resolve_model("claude-code:claude-sonnet-4-6", max_retries=12)
        assert isinstance(m, ChatClaudeCode)
        assert m.model == "claude-sonnet-4-6"

    def test_rejects_stale_pypi_adapter(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_lcc = types.ModuleType("langchain_claude_code")
        fake_lcc.ChatClaudeCode = lambda **_kwargs: "SHOULD_NOT_CONSTRUCT"
        monkeypatch.setitem(sys.modules, "langchain_claude_code", fake_lcc)
        monkeypatch.setattr(
            lcc_patches.importlib_metadata,
            "version",
            lambda name: (
                "0.1.0"
                if name == lcc_patches.UPSTREAM_LANGCHAIN_CLAUDE_CODE_DIST
                else (_raise_package_not_found(name))
            ),
        )
        with pytest.raises(ImportError, match="stale PyPI adapter"):
            _resolve_model("claude-code:claude-sonnet-4-6")

    def test_accepts_controlled_pypi_adapter_metadata(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}
        fake_lcc = types.ModuleType("langchain_claude_code")
        monkeypatch.setattr(
            lcc_patches, "ensure_tool_enforcement_hooks_installed", lambda *_a, **_kw: None
        )

        def _fake_chat(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "MODEL"

        fake_lcc.ChatClaudeCode = _fake_chat
        monkeypatch.setitem(sys.modules, "langchain_claude_code", fake_lcc)
        monkeypatch.setattr(
            lcc_patches.importlib_metadata,
            "version",
            lambda name: (
                "0.1.2"
                if name == lcc_patches.CONTROLLED_LANGCHAIN_CLAUDE_CODE_DIST
                else (_raise_package_not_found(name))
            ),
        )

        assert _resolve_model("claude-code:claude-sonnet-4-6") == "MODEL"
        assert captured["model"] == "claude-sonnet-4-6"

    def test_native_compatible_adapter_still_installs_mimir_enforcement_hooks(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_lcc = types.ModuleType("langchain_claude_code")
        fake_ccm = types.ModuleType("langchain_claude_code.claude_chat_model")
        fake_sdk = types.ModuleType("claude_agent_sdk")

        class _FakeHookMatcher:
            def __init__(self, *, hooks: list[Any]) -> None:
                self.hooks = hooks

        fake_sdk.HookMatcher = _FakeHookMatcher
        fake_lcc.MIMIR_COMPATIBILITY = {
            "features": sorted(lcc_patches._REQUIRED_ADAPTER_FEATURES)
        }

        class _FakeChatClaudeCode:
            def _get_tool_schema(self, tool: Any) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            def _wrap_langchain_tool(
                self, tool: Any, schema: dict[str, Any]
            ) -> Any:
                return None

            def _build_options(self, **_overrides: Any) -> Any:
                return types.SimpleNamespace(hooks=None)

            async def _aquery(self, *_args: Any, **_kwargs: Any) -> Any:
                return "", [], {}

            async def _astream(self, *_args: Any, **_kwargs: Any) -> Any:
                if False:
                    yield None

        fake_ccm.ClaudeCodeChatModel = _FakeChatClaudeCode
        fake_lcc.claude_chat_model = fake_ccm
        monkeypatch.setitem(sys.modules, "langchain_claude_code", fake_lcc)
        monkeypatch.setitem(
            sys.modules, "langchain_claude_code.claude_chat_model", fake_ccm
        )
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

        original_schema = _FakeChatClaudeCode._get_tool_schema
        original_wrap = _FakeChatClaudeCode._wrap_langchain_tool

        lcc_patches.ensure_tool_enforcement_hooks_installed(fake_lcc)

        assert _FakeChatClaudeCode._get_tool_schema is original_schema
        assert _FakeChatClaudeCode._wrap_langchain_tool is original_wrap
        assert _FakeChatClaudeCode._build_options is not None
        assert _FakeChatClaudeCode._aquery is not None
        assert _FakeChatClaudeCode._astream is not None
        assert _FakeChatClaudeCode._mimir_tool_event_hooks_installed is True


# ─── Config integration ────────────────────────────────────────────


def test_resolve_model_from_config_threads_all_config_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_init(spec: str, **kwargs: Any) -> str:
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return "MODEL"

    class _Cfg:
        model_spec = "openai:gpt-5.4"
        model_max_retries = 4
        model_max_tokens = 2048
        model_reasoning_effort = "high"

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
    monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)

    out = resolve_model_from_config(_Cfg())

    assert out == "MODEL"
    assert captured == {
        "spec": "openai:gpt-5.4",
        "kwargs": {
            "max_retries": 4,
            "max_tokens": 2048,
            "use_responses_api": True,
            "reasoning_effort": "high",
        },
    }


def test_resolve_model_from_config_honors_env_spec_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_init(spec: str, **kwargs: Any) -> str:
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return "MODEL"

    class _Cfg:
        model_spec = "anthropic:claude-haiku-4-5"
        model_max_retries = 2
        model_max_tokens = 0
        model_reasoning_effort = ""

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "openai:gpt-5.4-nano")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("MIMIR_USE_RESPONSES_API", raising=False)

    resolve_model_from_config(_Cfg())

    assert captured["spec"] == "openai:gpt-5.4-nano"
    assert captured["kwargs"] == {"max_retries": 2, "use_responses_api": True}


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


# ─── reasoning_effort threading (settable across providers) ──────────


def test_codex_plus_reasoning_effort_defaults_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("langchain_codex_plus")
    captured: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "M"

    monkeypatch.setattr("langchain_codex_plus.ChatCodexPlus", _fake)
    _resolve_model("codex-plus:gpt-5.4")
    assert captured["reasoning_effort"] == "none"


def test_codex_plus_reasoning_effort_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("langchain_codex_plus")
    captured: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "M"

    monkeypatch.setattr("langchain_codex_plus.ChatCodexPlus", _fake)
    _resolve_model("codex-plus:gpt-5.4", reasoning_effort="medium")
    assert captured["reasoning_effort"] == "medium"


def test_openai_reasoning_effort_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_init(spec: str, **kwargs: Any) -> str:
        captured["kwargs"] = kwargs
        return "M"

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _resolve_model("openai:gpt-5.4", reasoning_effort="high")
    assert captured["kwargs"]["reasoning_effort"] == "high"


def test_anthropic_ignores_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_init(spec: str, **kwargs: Any) -> str:
        captured["kwargs"] = kwargs
        return "M"

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
    # Real Claude takes `effort` (langchain-anthropic), NOT `reasoning_effort`.
    _resolve_model("anthropic:claude-haiku-4-5", reasoning_effort="high")
    assert captured["kwargs"]["effort"] == "high"
    assert "reasoning_effort" not in captured["kwargs"]


def test_config_default_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.delenv("MIMIR_MODEL_REASONING_EFFORT", raising=False)
    cfg = Config.from_env()
    assert cfg.model_reasoning_effort == ""


def test_config_reasoning_effort_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.setenv("MIMIR_MODEL_REASONING_EFFORT", "medium")
    cfg = Config.from_env()
    assert cfg.model_reasoning_effort == "medium"


def test_anthropic_minimax_excluded_from_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_init(spec: str, **kwargs: Any) -> str:
        captured["kwargs"] = kwargs
        return "M"

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
    # Minimax rides the anthropic: spec, but the anthropic-compat endpoint
    # isn't known to support effort — it must NOT receive it.
    _resolve_model("anthropic:MiniMax-M3", reasoning_effort="high")
    assert "effort" not in captured["kwargs"]
    assert "reasoning_effort" not in captured["kwargs"]


def test_effort_skipped_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_init(spec: str, **kwargs: Any) -> str:
        captured["kwargs"] = kwargs
        return "M"

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)
    # "none" is Codex-only; real Claude has no "none" level, so skip it.
    _resolve_model("anthropic:claude-opus-4-8", reasoning_effort="none")
    assert "effort" not in captured["kwargs"]


def test_claude_code_gets_effort_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langchain_claude_code")
    captured: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "M"

    monkeypatch.setattr("langchain_claude_code.ChatClaudeCode", _fake)
    monkeypatch.setattr(
        lcc_patches, "ensure_tool_enforcement_hooks_installed", lambda *_a, **_kw: None
    )
    _resolve_model("claude-code:claude-sonnet-4-6", reasoning_effort="high")
    assert captured["effort"] == "high"


def test_claude_code_omits_effort_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langchain_claude_code")
    captured: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "M"

    monkeypatch.setattr("langchain_claude_code.ChatClaudeCode", _fake)
    monkeypatch.setattr(
        lcc_patches, "ensure_tool_enforcement_hooks_installed", lambda *_a, **_kw: None
    )
    _resolve_model("claude-code:claude-sonnet-4-6")
    assert "effort" not in captured


# ─── per-provider effort validation (fail fast on an invalid level) ──


def test_codex_invalid_effort_raises() -> None:
    pytest.importorskip("langchain_codex_plus")
    # "max" is valid for Claude but not Codex (none/low/medium/high/xhigh).
    with pytest.raises(ValueError, match="codex-plus"):
        _resolve_model("codex-plus:gpt-5.4", reasoning_effort="max")


def test_openai_invalid_effort_raises() -> None:
    # "xhigh" is valid for Codex/Claude but not OpenAI (minimal/low/medium/high).
    with pytest.raises(ValueError, match="openai"):
        _resolve_model("openai:gpt-5.4", reasoning_effort="xhigh")


def test_anthropic_invalid_effort_raises() -> None:
    # "minimal" is OpenAI-only; not valid for Claude.
    with pytest.raises(ValueError, match="anthropic"):
        _resolve_model("anthropic:claude-opus-4-8", reasoning_effort="minimal")


def test_claude_code_invalid_effort_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("langchain_claude_code")
    monkeypatch.setattr(
        lcc_patches, "ensure_tool_enforcement_hooks_installed", lambda *_a, **_kw: None
    )
    with pytest.raises(ValueError, match="claude-code"):
        _resolve_model("claude-code:claude-sonnet-4-6", reasoning_effort="minimal")
