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
    """Tests should drop the cached pool between scenarios so each
    test gets fresh persistent clients (with a fresh fake SDK).
    Resetting the module-level singleton is sufficient: the next
    ``call_llm_sync`` will lazily build a new pool, and any in-flight
    daemon threads from prior tests are harmless (they hold the
    *previous* test's fake SDK and never see a new submission)."""
    import saga._llm as _llm
    _llm._persistent_pool = None


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


def test_persistent_pool_reuses_idle_runner():
    """A single-threaded sequence of acquire/release should reuse the
    same runner — that's the whole point of caching the warm SDK
    client across calls."""
    import saga._llm as _llm

    _reset_persistent_runner()
    pool = _llm._PersistentClaudePool(max_size=2)

    r1 = pool.acquire()
    pool.release(r1)
    r2 = pool.acquire()
    try:
        assert r1 is r2, "released runner must be reused on next acquire"
    finally:
        pool.release(r2)


def test_persistent_pool_caps_concurrent_instances():
    """Under concurrent load, the pool must never exceed ``max_size``
    live ``_PersistentClaudeCode`` instances — that's the leak fix.
    Many worker threads churning through acquire/release see at most
    ``max_size`` distinct instances."""
    import saga._llm as _llm
    import threading

    _reset_persistent_runner()
    pool = _llm._PersistentClaudePool(max_size=3)
    seen: set[int] = set()
    seen_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        # Stagger acquires so churn is realistic but the pool is
        # genuinely under contention (8 workers, cap=3).
        barrier.wait()
        for _ in range(3):
            r = pool.acquire()
            try:
                with seen_lock:
                    seen.add(id(r))
            finally:
                pool.release(r)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "worker hung — likely a deadlock in pool"

    assert len(seen) <= 3, (
        f"pool grew past max_size: {len(seen)} distinct runners "
        f"created, expected <= 3"
    )
    assert pool.size <= 3
    # All workers borrowed from the (small) pool; we should have
    # actually constructed at least 1 and at most 3 instances.
    assert 1 <= len(seen) <= 3


def test_persistent_pool_blocks_when_full_until_release():
    """When ``max_size`` instances are checked out, a new ``acquire``
    must block until something is released — not silently grow the
    pool past the cap."""
    import saga._llm as _llm
    import threading

    _reset_persistent_runner()
    pool = _llm._PersistentClaudePool(max_size=1)

    held = pool.acquire()
    acquired_event = threading.Event()
    second: dict[str, object] = {}

    def waiter():
        second["runner"] = pool.acquire()
        acquired_event.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()

    # The waiter should be parked — not yet fired.
    assert not acquired_event.wait(timeout=0.2), (
        "waiter acquired before holder released — pool exceeded max_size"
    )

    pool.release(held)

    assert acquired_event.wait(timeout=5), "waiter never woke after release"
    t.join(timeout=5)
    pool.release(second["runner"])
    assert second["runner"] is held, (
        "post-release acquire should reuse the same instance"
    )


def test_persistent_pool_construction_failure_frees_slot():
    """If ``_PersistentClaudeCode.__init__`` raises, the reserved slot
    must be returned to the pool — otherwise repeated failures would
    permanently shrink capacity."""
    import saga._llm as _llm

    _reset_persistent_runner()
    pool = _llm._PersistentClaudePool(max_size=1)

    boom_count = {"n": 0}
    real_init = _llm._PersistentClaudeCode.__init__

    def flaky_init(self, *args, **kwargs):
        boom_count["n"] += 1
        if boom_count["n"] == 1:
            raise RuntimeError("boom")
        return real_init(self, *args, **kwargs)

    _llm._PersistentClaudeCode.__init__ = flaky_init
    try:
        try:
            pool.acquire()
        except RuntimeError:
            pass
        else:
            raise AssertionError("flaky_init should have raised")

        # Slot was reserved before the failed construction; without
        # the back-out, ``size`` would be stuck at 1 with nothing to
        # check out, and this acquire would block forever.
        runner = pool.acquire()
        assert runner is not None
        pool.release(runner)
    finally:
        _llm._PersistentClaudeCode.__init__ = real_init


def test_persistent_pool_default_size_from_env(monkeypatch):
    """``SAGA_PERSISTENT_CLAUDE_POOL_SIZE`` controls the default
    pool's cap; bogus values fall back to the default rather than
    raising."""
    import saga._llm as _llm

    _reset_persistent_runner()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "7")
    pool = _llm._get_persistent_pool()
    assert pool.max_size == 7

    _reset_persistent_runner()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "not-a-number")
    pool = _llm._get_persistent_pool()
    assert pool.max_size == _llm._DEFAULT_POOL_SIZE

    _reset_persistent_runner()
    monkeypatch.setenv("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "0")
    pool = _llm._get_persistent_pool()
    assert pool.max_size == _llm._DEFAULT_POOL_SIZE


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
