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
    # Only max_completion_tokens is sent — gpt-5.x rejects max_tokens.
    assert captured["body"]["max_completion_tokens"] == 42
    assert "max_tokens" not in captured["body"]
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


class _FakeAssistantMessage:
    """Stand-in for claude_agent_sdk.AssistantMessage (isinstance check
    in the impl checks against the fake's class — see _install_fake_sdk)."""
    def __init__(self, content):
        self.content = content


def _install_fake_claude_agent_sdk(
    monkeypatch, *, scripted_responses=None, captured=None,
    raise_on_query=None, raise_on_connect=None,
):
    """Install a fake ``claude_agent_sdk`` module exposing
    ``ClaudeSDKClient``, ``ClaudeAgentOptions``, and ``AssistantMessage``.

    ``scripted_responses`` is a list of [block_list_for_call_1,
    block_list_for_call_2, ...]; each call to ``client.query`` consumes
    one entry. ``captured`` (optional dict) records call args so tests
    can assert on them.
    """
    import sys as _sys
    fake_mod = types.ModuleType("claude_agent_sdk")

    class _FakeOptions:
        def __init__(self, **kwargs):
            if captured is not None:
                captured.setdefault("option_inits", []).append(kwargs)
                captured["options"] = kwargs  # last-write-wins for convenience

    fake_mod.ClaudeAgentOptions = _FakeOptions
    fake_mod.AssistantMessage = _FakeAssistantMessage

    state: dict[str, Any] = {
        "calls_so_far": 0,
        "queue": list(scripted_responses or [[]]),
    }

    class _FakeClient:
        def __init__(self, options):
            if captured is not None:
                captured.setdefault("client_options", []).append(options)
            self._next_blocks = None
            self._connected = False

        async def connect(self):
            if raise_on_connect:
                raise raise_on_connect
            if captured is not None:
                captured["connect_count"] = captured.get("connect_count", 0) + 1
            self._connected = True

        async def disconnect(self):
            if captured is not None:
                captured["disconnect_count"] = captured.get("disconnect_count", 0) + 1
            self._connected = False

        async def query(self, prompt):
            if captured is not None:
                captured.setdefault("prompts", []).append(prompt)
            if raise_on_query:
                raise raise_on_query
            # Pop next response from queue (or use last if exhausted).
            if state["queue"]:
                self._next_blocks = state["queue"].pop(0)
            else:
                self._next_blocks = []

        async def receive_response(self):
            yield _FakeAssistantMessage(self._next_blocks or [])

    fake_mod.ClaudeSDKClient = _FakeClient
    monkeypatch.setitem(_sys.modules, "claude_agent_sdk", fake_mod)


def _reset_persistent_runner():
    """Tests should drop the per-thread cached runner between scenarios
    so each test gets a fresh persistent client (with a fresh fake SDK).
    The runner is stored on a ``threading.local`` keyed by the calling
    thread; pytest runs tests on the main thread, so clearing the main
    thread's entry is sufficient."""
    import saga._llm as _llm
    if hasattr(_llm._persistent_runner_local, "runner"):
        delattr(_llm._persistent_runner_local, "runner")


def test_claude_code_happy_path(monkeypatch):
    _reset_persistent_runner()
    from saga._llm import call_llm_sync

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("hello "), _FakeContent("world")]],
        captured=captured,
    )

    out = call_llm_sync(
        {"provider": "claude_code", "model": "claude-haiku-4-5"},
        prompt="hi", system="be brief",
    )
    assert out == "hello world"
    assert captured["prompts"] == ["hi"]
    # First call connects; first connect uses the model + system prompt.
    assert captured["options"]["model"] == "claude-haiku-4-5"
    assert captured["options"]["system_prompt"] == "be brief"
    assert captured["connect_count"] == 1


def test_claude_code_omits_unset_options(monkeypatch):
    _reset_persistent_runner()
    from saga._llm import call_llm_sync

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("ok")]],
        captured=captured,
    )

    call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert "model" not in captured["options"]
    assert "system_prompt" not in captured["options"]


def test_claude_code_skips_blocks_without_text(monkeypatch):
    _reset_persistent_runner()
    from saga._llm import call_llm_sync

    class _NonText:
        pass  # no .text

    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("a"), _NonText(), _FakeContent("b")]],
    )

    out = call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert out == "ab"


def test_claude_code_query_exception_returns_empty(monkeypatch):
    _reset_persistent_runner()
    from saga._llm import call_llm_sync

    _install_fake_claude_agent_sdk(
        monkeypatch,
        raise_on_query=RuntimeError("CLI not authenticated"),
    )

    out = call_llm_sync({"provider": "claude_code"}, prompt="x")
    assert out == ""


def test_claude_code_reuses_client_across_calls(monkeypatch):
    """Two consecutive calls with the same model should share one
    connect()/disconnect() pair — that's the whole point of the
    persistent client (avoids subprocess spawn per call)."""
    _reset_persistent_runner()
    from saga._llm import call_llm_sync

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[
            [_FakeContent("one")],
            [_FakeContent("two")],
            [_FakeContent("three")],
        ],
        captured=captured,
    )

    cfg = {"provider": "claude_code", "model": "claude-haiku-4-5"}
    assert call_llm_sync(cfg, prompt="p1") == "one"
    assert call_llm_sync(cfg, prompt="p2") == "two"
    assert call_llm_sync(cfg, prompt="p3") == "three"
    # One connect at boot, no recycle yet (3 < default 10).
    assert captured["connect_count"] == 1
    assert captured.get("disconnect_count", 0) == 0


def test_claude_code_recycles_after_threshold(monkeypatch):
    """Past SAGA_PERSISTENT_CLAUDE_RECYCLE calls, the client is torn
    down and a fresh one connected — bounds context bloat."""
    _reset_persistent_runner()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_RECYCLE", "2")
    from saga._llm import call_llm_sync

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[
            [_FakeContent(f"r{i}")] for i in range(5)
        ],
        captured=captured,
    )

    cfg = {"provider": "claude_code", "model": "claude-haiku-4-5"}
    for i in range(5):
        call_llm_sync(cfg, prompt=f"p{i}")
    # K=2: calls 0,1 on client A. Call 2 triggers recycle → client B
    # handles 2,3. Call 4 triggers another recycle → client C handles 4.
    # That's 3 connects, 2 disconnects.
    assert captured["connect_count"] == 3
    assert captured["disconnect_count"] == 2


def test_claude_code_recycles_on_model_change(monkeypatch):
    """Switching ``model`` between calls forces a recycle so the new
    options take effect."""
    _reset_persistent_runner()
    from saga._llm import call_llm_sync

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("a")], [_FakeContent("b")]],
        captured=captured,
    )

    call_llm_sync({"provider": "claude_code", "model": "claude-haiku-4-5"}, prompt="p1")
    call_llm_sync({"provider": "claude_code", "model": "claude-opus-4-7"},  prompt="p2")
    assert captured["connect_count"] == 2
    assert captured["disconnect_count"] == 1


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


def test_persistent_runner_is_per_thread():
    """Each calling thread gets its own persistent runner instance.
    Two threads asking for the runner must receive distinct objects;
    a single thread asking twice gets the same instance."""
    import saga._llm as _llm
    import threading

    _reset_persistent_runner()

    main_runner = _llm._persistent_runner()
    main_runner_again = _llm._persistent_runner()
    assert main_runner is main_runner_again, (
        "same thread must reuse its cached runner"
    )

    other_runner_holder: dict[str, object] = {}
    ready = threading.Event()

    def in_other_thread():
        try:
            other_runner_holder["runner"] = _llm._persistent_runner()
            # Confirm the other thread also caches its own across calls.
            other_runner_holder["runner_again"] = _llm._persistent_runner()
        finally:
            ready.set()

    t = threading.Thread(target=in_other_thread, daemon=True)
    t.start()
    assert ready.wait(timeout=10), "other thread didn't complete"
    t.join(timeout=10)

    other_runner = other_runner_holder["runner"]
    other_runner_again = other_runner_holder["runner_again"]
    assert other_runner is other_runner_again, (
        "second thread must also reuse its cached runner"
    )
    assert other_runner is not main_runner, (
        "different threads must get different runners"
    )
