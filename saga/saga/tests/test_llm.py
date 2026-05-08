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


@pytest.mark.asyncio
async def test_openai_compat_happy_path(monkeypatch):
    from saga._llm import call_llm

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        captured["timeout"] = timeout
        return _FakeResp({"choices": [{"message": {"content": "hello world"}}]})

    monkeypatch.setattr("requests.post", fake_post)

    out = await call_llm(
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


@pytest.mark.asyncio
async def test_openai_compat_uses_reasoning_when_content_none(monkeypatch):
    from saga._llm import call_llm

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp({"choices": [{"message": {"content": None, "reasoning": "thought"}}]})

    monkeypatch.setattr("requests.post", fake_post)
    out = await call_llm(
        {"provider": "openai_compat", "url": "u", "api_key": "k"},
        prompt="x",
    )
    assert out == "thought"


@pytest.mark.asyncio
async def test_openai_compat_exception_returns_empty(monkeypatch):
    from saga._llm import call_llm

    def boom(url, headers=None, json=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.post", boom)
    out = await call_llm(
        {"provider": "openai_compat", "url": "u", "api_key": "k"},
        prompt="x",
    )
    assert out == ""


@pytest.mark.asyncio
async def test_default_provider_is_openai_compat(monkeypatch):
    """No provider field → openai_compat path."""
    from saga._llm import call_llm

    called = {"hit": False}

    def fake_post(url, headers=None, json=None, timeout=None):
        called["hit"] = True
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("requests.post", fake_post)
    out = await call_llm({"url": "u", "api_key": "k"}, prompt="x")
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


@pytest.mark.asyncio
async def test_anthropic_happy_path(monkeypatch):
    from saga._llm import call_llm

    captured = {}
    _install_fake_anthropic(
        monkeypatch,
        [_FakeBlock("hello "), _FakeBlock("world")],
        captured,
    )

    out = await call_llm(
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


@pytest.mark.asyncio
async def test_anthropic_default_model(monkeypatch):
    """Empty/missing model field → claude-haiku-4-5 default."""
    from saga._llm import call_llm

    captured = {}
    _install_fake_anthropic(monkeypatch, [_FakeBlock("ok")], captured)

    await call_llm(
        {"provider": "anthropic", "api_key": "ak"},
        prompt="hi",
    )
    assert captured["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_anthropic_skips_blocks_without_text(monkeypatch):
    """Tool_use / thinking blocks lack a .text attr — should be skipped."""
    from saga._llm import call_llm

    class _NonText:
        # No .text attribute.
        pass

    captured = {}
    _install_fake_anthropic(
        monkeypatch,
        [_FakeBlock("a"), _NonText(), _FakeBlock("b")],
        captured,
    )
    out = await call_llm(
        {"provider": "anthropic", "api_key": "ak"},
        prompt="hi",
    )
    assert out == "ab"


@pytest.mark.asyncio
async def test_anthropic_missing_api_key_returns_empty(monkeypatch):
    from saga._llm import call_llm

    # Ensure anthropic isn't even imported by clearing the slot.
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    out = await call_llm(
        {"provider": "anthropic", "api_key": ""},
        prompt="x",
    )
    assert out == ""


@pytest.mark.asyncio
async def test_anthropic_import_error_falls_back_to_openai_compat(monkeypatch):
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

    out = await _llm.call_llm(
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


@pytest.mark.asyncio
async def test_claude_code_omits_unset_options(monkeypatch):
    _reset_async_pools()
    from saga._llm import call_llm

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("ok")]],
        captured=captured,
    )

    await call_llm({"provider": "claude_code"}, prompt="x")
    assert "model" not in captured["options"]
    assert "system_prompt" not in captured["options"]


@pytest.mark.asyncio
async def test_claude_code_skips_blocks_without_text(monkeypatch):
    _reset_async_pools()
    from saga._llm import call_llm

    class _NonText:
        pass  # no .text

    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("a"), _NonText(), _FakeContent("b")]],
    )

    out = await call_llm({"provider": "claude_code"}, prompt="x")
    assert out == "ab"


@pytest.mark.asyncio
async def test_claude_code_query_exception_returns_empty(monkeypatch):
    _reset_async_pools()
    from saga._llm import call_llm

    _install_fake_claude_agent_sdk(
        monkeypatch,
        raise_on_query=RuntimeError("CLI not authenticated"),
    )

    out = await call_llm({"provider": "claude_code"}, prompt="x")
    assert out == ""


@pytest.mark.asyncio
async def test_anthropic_exception_returns_empty(monkeypatch):
    from saga._llm import call_llm

    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("rate limited")

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = lambda **kw: types.SimpleNamespace(messages=_BoomMessages())
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    out = await call_llm(
        {"provider": "anthropic", "api_key": "ak"},
        prompt="x",
    )
    assert out == ""


# ─── Async-native call_llm (chainlink #45 / Phase 1 of #20) ─────────


def _reset_async_pools():
    """Drop the per-loop async pool registry between tests so each
    scenario sees a fresh ``_AsyncClaudePool``. Safe to call without
    awaiting — we don't disconnect the runners (the fake SDK has
    nothing to clean up; in production each test loop is torn down
    independently)."""
    import saga._llm as _llm
    _llm._reset_async_pools()


@pytest.mark.asyncio
async def test_async_call_llm_claude_code_happy_path(monkeypatch):
    _reset_async_pools()
    from saga._llm import call_llm

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("hello "), _FakeContent("world")]],
        captured=captured,
    )

    out = await call_llm(
        {"provider": "claude_code", "model": "claude-haiku-4-5"},
        prompt="hi", system="be brief",
    )
    assert out == "hello world"
    assert captured["prompts"] == ["hi"]
    assert captured["options"]["model"] == "claude-haiku-4-5"
    assert captured["options"]["system_prompt"] == "be brief"
    assert captured["connect_count"] == 1


@pytest.mark.asyncio
async def test_async_call_llm_reuses_runner_across_calls(monkeypatch):
    """Sequential awaits with the same model must reuse one runner
    (one connect, no disconnect until recycle threshold)."""
    _reset_async_pools()
    from saga._llm import call_llm

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
    assert await call_llm(cfg, prompt="p1") == "one"
    assert await call_llm(cfg, prompt="p2") == "two"
    assert await call_llm(cfg, prompt="p3") == "three"
    # 3 < default recycle threshold (10) → one connect, no disconnect.
    assert captured["connect_count"] == 1
    assert captured.get("disconnect_count", 0) == 0


@pytest.mark.asyncio
async def test_async_pool_recycles_after_threshold(monkeypatch):
    """Past ``SAGA_PERSISTENT_CLAUDE_RECYCLE`` calls, the runner
    disconnects and reconnects — bounds context bloat."""
    _reset_async_pools()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_RECYCLE", "2")
    from saga._llm import call_llm

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent(f"r{i}")] for i in range(5)],
        captured=captured,
    )

    cfg = {"provider": "claude_code", "model": "claude-haiku-4-5"}
    for i in range(5):
        await call_llm(cfg, prompt=f"p{i}")
    # Same shape as the sync recycle test: K=2 → 3 connects + 2 disconnects.
    assert captured["connect_count"] == 3
    assert captured["disconnect_count"] == 2


@pytest.mark.asyncio
async def test_async_pool_recycles_on_model_change(monkeypatch):
    _reset_async_pools()
    from saga._llm import call_llm

    captured: dict[str, Any] = {}
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent("a")], [_FakeContent("b")]],
        captured=captured,
    )

    await call_llm({"provider": "claude_code", "model": "claude-haiku-4-5"}, prompt="p1")
    await call_llm({"provider": "claude_code", "model": "claude-opus-4-7"},  prompt="p2")
    assert captured["connect_count"] == 2
    assert captured["disconnect_count"] == 1


@pytest.mark.asyncio
async def test_async_call_llm_import_error_falls_back_to_openai_compat(monkeypatch):
    """If claude-agent-sdk isn't importable, fall through to
    openai_compat — keeps standalone saga environments runnable."""
    _reset_async_pools()
    from saga import _llm

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    called = {"hit": False}

    def fake_post(url, headers=None, json=None, timeout=None):
        called["hit"] = True
        return _FakeResp({"choices": [{"message": {"content": "fallback"}}]})

    monkeypatch.setattr("requests.post", fake_post)

    out = await _llm.call_llm(
        {"provider": "claude_code", "url": "u", "api_key": "k"},
        prompt="x",
    )
    assert out == "fallback"
    assert called["hit"]


@pytest.mark.asyncio
async def test_async_call_llm_openai_compat_via_to_thread(monkeypatch):
    """The async path's openai_compat dispatch should hit the same
    sync transport via ``asyncio.to_thread`` — verify by capturing
    the request shape."""
    _reset_async_pools()
    from saga._llm import call_llm

    captured: dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _FakeResp({"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr("requests.post", fake_post)

    out = await call_llm(
        {"provider": "openai_compat", "url": "https://x/v1/chat", "api_key": "k", "model": "m"},
        prompt="hello", max_tokens=42, temperature=0.5,
    )
    assert out == "hi"
    assert captured["url"] == "https://x/v1/chat"
    assert captured["body"]["max_completion_tokens"] == 42


@pytest.mark.asyncio
async def test_async_call_llm_anthropic_via_to_thread(monkeypatch):
    """The async path's anthropic dispatch hits the sync anthropic
    SDK through ``asyncio.to_thread``."""
    _reset_async_pools()
    from saga._llm import call_llm

    captured: dict[str, Any] = {}
    _install_fake_anthropic(
        monkeypatch,
        blocks=[_FakeBlock("from-anthropic")],
        captured=captured,
    )

    out = await call_llm(
        {"provider": "anthropic", "api_key": "ak", "model": "claude-haiku-4-5"},
        prompt="x", system="y",
    )
    assert out == "from-anthropic"
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["system"] == "y"


@pytest.mark.asyncio
async def test_async_pool_caps_concurrent_runners(monkeypatch):
    """The whole point of the bounded pool: under concurrent load
    (more callers than ``max_size``), the pool must never spawn
    more than ``max_size`` distinct runners. Cross-task parallelism
    is preserved up to the cap."""
    _reset_async_pools()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "2")
    # Disable recycling so connect_count == distinct-runner-count
    # (otherwise high-volume tests trigger mid-run reconnects which
    # inflate connect_count past the runner count).
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_RECYCLE", "1000")
    import asyncio
    from saga._llm import call_llm, _get_async_claude_pool

    captured: dict[str, Any] = {}
    # Plenty of canned responses so 8 concurrent calls × 3 each don't
    # exhaust the queue.
    _install_fake_claude_agent_sdk(
        monkeypatch,
        scripted_responses=[[_FakeContent(f"r{i}")] for i in range(64)],
        captured=captured,
    )

    cfg = {"provider": "claude_code", "model": "claude-haiku-4-5"}

    async def worker():
        for _ in range(3):
            await call_llm(cfg, prompt="p")

    await asyncio.gather(*(worker() for _ in range(8)))

    # Strict invariant: pool size never exceeds the cap.
    pool = _get_async_claude_pool()
    assert pool.size <= 2

    # And the number of distinct Claude SDK client constructions
    # bounded by the cap (since recycle is effectively disabled).
    assert 1 <= captured["connect_count"] <= 2


@pytest.mark.asyncio
async def test_async_pool_blocks_when_full_until_release(monkeypatch):
    """When ``max_size`` runners are checked out, a new ``acquire``
    must await until something is released — not silently grow past
    the cap."""
    _reset_async_pools()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "1")
    import asyncio
    import saga._llm as _llm

    pool = _llm._AsyncClaudePool(max_size=1, recycle_after=10)

    held = await pool.acquire()
    waiter_started = asyncio.Event()
    waiter_done = asyncio.Event()
    second: dict[str, Any] = {}

    async def waiter():
        waiter_started.set()
        second["runner"] = await pool.acquire()
        waiter_done.set()

    task = asyncio.create_task(waiter())
    await waiter_started.wait()
    # Give the event loop a chance to advance the waiter into pool.acquire.
    await asyncio.sleep(0.01)
    assert not waiter_done.is_set(), "waiter should be blocked while held runner is checked out"

    await pool.release(held)
    await asyncio.wait_for(waiter_done.wait(), timeout=2)
    assert second["runner"] is held, "released runner should be reused on next acquire"

    await pool.release(second["runner"])
    await task


def test_async_pool_default_size_from_env(monkeypatch):
    """``SAGA_PERSISTENT_CLAUDE_POOL_SIZE`` and
    ``SAGA_PERSISTENT_CLAUDE_RECYCLE`` flow through to the lazy
    pool's caps; bogus values fall back to defaults rather than
    raising."""
    import saga._llm as _llm

    _reset_async_pools()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "7")
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_RECYCLE", "3")
    # The factory only resolves env at construction; build a pool by
    # hand using the same resolvers (avoids needing a running loop).
    pool = _llm._AsyncClaudePool(
        max_size=_llm._resolve_pool_size(),
        recycle_after=_llm._resolve_recycle_after(),
    )
    assert pool.max_size == 7
    assert pool._recycle_after == 3

    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_RECYCLE", "not-a-number")
    assert _llm._resolve_recycle_after() == _llm._RECYCLE_AFTER_CALLS

    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_RECYCLE", "0")
    assert _llm._resolve_recycle_after() == _llm._RECYCLE_AFTER_CALLS
