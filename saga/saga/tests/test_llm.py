"""Tests for saga._llm — the unified LLM transport.

Covers:
- openai_compat path: requests.post invoked with right shape, returns content
- openai_compat path: ``reasoning`` field used when ``content`` is None
- openai_compat path: exception → empty string
- anthropic path: messages.create invoked, content blocks flattened
- anthropic path: missing api_key → empty string
- anthropic path: ImportError → falls back to openai_compat
- system prompt threaded to both providers
- default provider is openai_compat
"""

from __future__ import annotations

import sys
import types

import pytest


# ─── openai_compat ───────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_openai_compat_happy_path(monkeypatch):
    from saga._llm import call_llm_sync

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        captured["timeout"] = timeout
        return _FakeResp({"choices": [{"message": {"content": "hello world"}}]})

    monkeypatch.setattr("requests.post", fake_post)

    out = call_llm_sync(
        {"provider": "openai_compat", "url": "https://x/v1/chat", "api_key": "k", "model": "m"},
        prompt="hi", max_tokens=42, temperature=0.5, system="you are helpful",
    )
    assert out == "hello world"
    assert captured["url"] == "https://x/v1/chat"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["body"]["model"] == "m"
    assert captured["body"]["temperature"] == 0.5
    assert captured["body"]["max_tokens"] == 42
    assert captured["body"]["max_completion_tokens"] == 42
    assert captured["body"]["messages"][0] == {"role": "system", "content": "you are helpful"}
    assert captured["body"]["messages"][1] == {"role": "user", "content": "hi"}


def test_openai_compat_uses_reasoning_when_content_none(monkeypatch):
    from saga._llm import call_llm_sync

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp({"choices": [{"message": {"content": None, "reasoning": "thought"}}]})

    monkeypatch.setattr("requests.post", fake_post)
    out = call_llm_sync(
        {"provider": "openai_compat", "url": "u", "api_key": "k"},
        prompt="x",
    )
    assert out == "thought"


def test_openai_compat_exception_returns_empty(monkeypatch):
    from saga._llm import call_llm_sync

    def boom(url, headers=None, json=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.post", boom)
    out = call_llm_sync(
        {"provider": "openai_compat", "url": "u", "api_key": "k"},
        prompt="x",
    )
    assert out == ""


def test_default_provider_is_openai_compat(monkeypatch):
    """No provider field → openai_compat path."""
    from saga._llm import call_llm_sync

    called = {"hit": False}

    def fake_post(url, headers=None, json=None, timeout=None):
        called["hit"] = True
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("requests.post", fake_post)
    out = call_llm_sync({"url": "u", "api_key": "k"}, prompt="x")
    assert out == "ok"
    assert called["hit"]


# ─── anthropic ───────────────────────────────────────────────────


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeMsg:
    def __init__(self, blocks):
        self.content = blocks


class _FakeMessages:
    def __init__(self, blocks, captured):
        self._blocks = blocks
        self._captured = captured

    def create(self, **kwargs):
        self._captured.update(kwargs)
        return _FakeMsg(self._blocks)


class _FakeAnthropic:
    def __init__(self, blocks, captured):
        self.messages = _FakeMessages(blocks, captured)


def _install_fake_anthropic(monkeypatch, blocks, captured):
    """Install a fake `anthropic` module so the lazy import in _call_anthropic resolves."""
    fake_mod = types.ModuleType("anthropic")
    def _ctor(api_key=None, timeout=None):
        captured["init"] = {"api_key": api_key, "timeout": timeout}
        return _FakeAnthropic(blocks, captured)
    fake_mod.Anthropic = _ctor
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)


def test_anthropic_happy_path(monkeypatch):
    from saga._llm import call_llm_sync

    captured = {}
    _install_fake_anthropic(
        monkeypatch,
        [_FakeBlock("hello "), _FakeBlock("world")],
        captured,
    )

    out = call_llm_sync(
        {"provider": "anthropic", "api_key": "ak", "model": "claude-x", "timeout": 17},
        prompt="hi", max_tokens=99, temperature=0.2, system="be brief",
    )
    assert out == "hello world"
    assert captured["init"] == {"api_key": "ak", "timeout": 17}
    assert captured["model"] == "claude-x"
    assert captured["max_tokens"] == 99
    assert captured["temperature"] == 0.2
    assert captured["system"] == "be brief"
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_default_model(monkeypatch):
    """Empty/missing model field → claude-haiku-4-5 default."""
    from saga._llm import call_llm_sync

    captured = {}
    _install_fake_anthropic(monkeypatch, [_FakeBlock("ok")], captured)

    call_llm_sync(
        {"provider": "anthropic", "api_key": "ak"},
        prompt="hi",
    )
    assert captured["model"] == "claude-haiku-4-5"


def test_anthropic_skips_blocks_without_text(monkeypatch):
    """Tool_use / thinking blocks lack a .text attr — should be skipped."""
    from saga._llm import call_llm_sync

    class _NonText:
        # No .text attribute.
        pass

    captured = {}
    _install_fake_anthropic(
        monkeypatch,
        [_FakeBlock("a"), _NonText(), _FakeBlock("b")],
        captured,
    )
    out = call_llm_sync(
        {"provider": "anthropic", "api_key": "ak"},
        prompt="hi",
    )
    assert out == "ab"


def test_anthropic_missing_api_key_returns_empty(monkeypatch):
    from saga._llm import call_llm_sync

    # Ensure anthropic isn't even imported by clearing the slot.
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    out = call_llm_sync(
        {"provider": "anthropic", "api_key": ""},
        prompt="x",
    )
    assert out == ""


def test_anthropic_import_error_falls_back_to_openai_compat(monkeypatch):
    """If `anthropic` SDK isn't installed, _call_anthropic falls through
    to _call_openai_compat — keeps bench infra runnable on minimal envs."""
    from saga import _llm

    # Make `from anthropic import Anthropic` raise ImportError.
    monkeypatch.setitem(sys.modules, "anthropic", None)

    called = {"openai_compat": False}

    def fake_post(url, headers=None, json=None, timeout=None):
        called["openai_compat"] = True
        return _FakeResp({"choices": [{"message": {"content": "fallback"}}]})

    monkeypatch.setattr("requests.post", fake_post)

    out = _llm.call_llm_sync(
        {"provider": "anthropic", "api_key": "ak", "url": "u"},
        prompt="x",
    )
    assert out == "fallback"
    assert called["openai_compat"]


# ─── claude_code (Max OAuth via claude-agent-sdk) ───────────────


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMsg:
    def __init__(self, blocks):
        self.content = blocks


def _install_fake_claude_agent_sdk(monkeypatch, *, blocks_per_msg=None,
                                    captured=None, raise_on_query=None):
    """Install a fake `claude_agent_sdk` module with a query() that
    yields AssistantMessage-like objects."""
    fake_mod = types.ModuleType("claude_agent_sdk")

    class _FakeOptions:
        def __init__(self, **kwargs):
            if captured is not None:
                captured["options"] = kwargs

    fake_mod.ClaudeAgentOptions = _FakeOptions

    async def fake_query(*, prompt, options):
        if captured is not None:
            captured["prompt"] = prompt
        if raise_on_query:
            raise raise_on_query
        for blocks in (blocks_per_msg or [[]]):
            yield _FakeAssistantMsg(blocks)

    fake_mod.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)


def test_claude_code_happy_path(monkeypatch):
    from saga._llm import call_llm_sync

    captured = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        blocks_per_msg=[[_FakeContent("hello "), _FakeContent("world")]],
        captured=captured,
    )

    out = call_llm_sync(
        {"provider": "claude_code", "model": "claude-haiku-4-5"},
        prompt="hi", system="be brief",
    )
    assert out == "hello world"
    assert captured["prompt"] == "hi"
    assert captured["options"]["model"] == "claude-haiku-4-5"
    assert captured["options"]["system_prompt"] == "be brief"


def test_claude_code_omits_unset_options(monkeypatch):
    """When model/system aren't provided, ClaudeAgentOptions doesn't get
    those keys — lets the CLI's defaults / user CLAUDE_MODEL win."""
    from saga._llm import call_llm_sync

    captured = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        blocks_per_msg=[[_FakeContent("ok")]],
        captured=captured,
    )

    call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert "model" not in captured["options"]
    assert "system_prompt" not in captured["options"]


def test_claude_code_concatenates_multi_message_stream(monkeypatch):
    """query() can yield several AssistantMessages (e.g., interleaved
    with system messages we ignore). Text from all of them is joined."""
    from saga._llm import call_llm_sync

    _install_fake_claude_agent_sdk(
        monkeypatch,
        blocks_per_msg=[
            [_FakeContent("first ")],
            [_FakeContent("second")],
        ],
    )

    out = call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert out == "first second"


def test_claude_code_skips_blocks_without_text(monkeypatch):
    """Tool_use / thinking blocks lack a .text attr — skipped."""
    from saga._llm import call_llm_sync

    class _NonText:
        pass  # no .text

    _install_fake_claude_agent_sdk(
        monkeypatch,
        blocks_per_msg=[[_FakeContent("a"), _NonText(), _FakeContent("b")]],
    )

    out = call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert out == "ab"


def test_claude_code_query_exception_returns_empty(monkeypatch):
    from saga._llm import call_llm_sync

    _install_fake_claude_agent_sdk(
        monkeypatch,
        raise_on_query=RuntimeError("CLI not authenticated"),
    )

    out = call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert out == ""


def test_claude_code_import_error_falls_back_to_openai_compat(monkeypatch):
    """If claude-agent-sdk isn't installed, fall through to openai_compat —
    keeps standalone saga environments runnable."""
    from saga import _llm

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    called = {"hit": False}

    def fake_post(url, headers=None, json=None, timeout=None):
        called["hit"] = True
        return _FakeResp({"choices": [{"message": {"content": "fallback"}}]})

    monkeypatch.setattr("requests.post", fake_post)

    out = _llm.call_llm_sync(
        {"provider": "claude_code", "url": "u", "api_key": "k"},
        prompt="x",
    )
    assert out == "fallback"
    assert called["hit"]


def test_anthropic_exception_returns_empty(monkeypatch):
    from saga._llm import call_llm_sync

    captured = {}

    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("rate limited")

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = lambda **kw: types.SimpleNamespace(messages=_BoomMessages())
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    out = call_llm_sync(
        {"provider": "anthropic", "api_key": "ak"},
        prompt="x",
    )
    assert out == ""
