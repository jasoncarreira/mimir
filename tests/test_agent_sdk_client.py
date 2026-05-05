"""Stages 1-2 of CLAUDE_SDK_CLIENT_MIGRATION.md.

The agent loop now routes through ``mimir.agent.query`` — a thin async-
generator wrapper around a shared ``ClaudeSDKClient`` rather than a
direct call to ``claude_agent_sdk.query()``. These tests pin the
wrapper's behavior:

- A fresh client is constructed + connected on first call.
- Subsequent calls with matching options-fingerprint reuse the client.
- Options changes (system_prompt, model, …) trigger
  disconnect+reconnect.
- ``shutdown_sdk_client()`` releases the client cleanly and is
  idempotent.
- ``session_id`` flows through unchanged so per-turn isolation
  (``ctx.turn_id``) is preserved by the SDK's session store
  (stage 2).

Tests patch ``mimir.agent.ClaudeSDKClient`` with a fake recorder rather
than spinning up the real subprocess. Existing tests under
``test_agent_saga.py`` / ``test_phase8_hardening.py`` patch
``mimir.agent.query`` directly and don't exercise the wrapper — that's
intentional; the wrapper has its own dedicated coverage here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

import mimir.agent as agent_mod


class _FakeClaudeSDKClient:
    """Minimal stand-in for ClaudeSDKClient. Records construction
    (with options), connect/disconnect lifecycle, and the prompts /
    session_ids passed to ``query()``. ``receive_response()`` yields a
    canned reply derived from the most recent prompt so each test can
    assert what came out."""

    instances: list["_FakeClaudeSDKClient"] = []

    def __init__(self, options: ClaudeAgentOptions | None = None, transport: Any = None) -> None:
        self.options = options
        self.connect_count = 0
        self.disconnect_count = 0
        # list of (prompt, session_id) so tests can assert ordering
        self.queries: list[tuple[str, str]] = []
        self._next_reply: str = "ok"
        type(self).instances.append(self)

    async def connect(self) -> None:
        self.connect_count += 1

    async def disconnect(self) -> None:
        self.disconnect_count += 1

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append((prompt, session_id))
        self._next_reply = f"reply to: {prompt}"

    async def receive_response(self):
        yield AssistantMessage(
            content=[TextBlock(text=self._next_reply)],
            model="claude-opus-4-7",
        )

    # Stage 5: ``get_context_usage`` lets the agent loop pull plan-window
    # utilization off the warm client at end-of-turn. The fake records
    # call counts so tests can assert reuse vs recycle, and returns a
    # canned ``apiUsage`` payload by default — override
    # ``_context_usage_response`` per-test for failure / empty-response
    # coverage.
    _context_usage_response: dict | None = None
    get_context_usage_count: int = 0

    async def get_context_usage(self) -> dict:
        type(self).get_context_usage_count = (
            getattr(type(self), "get_context_usage_count", 0) + 1
        )
        if self._context_usage_response is not None:
            return self._context_usage_response
        return {
            "apiUsage": {
                "five_hour": {
                    "utilization": 0.42,
                    "resets_at": 9999999999,
                    "status": "allowed",
                },
            }
        }


def _reset_module_state() -> None:
    # chainlink #11: the singleton client + global asyncio.Lock were
    # replaced with a ``ClientPool``. The legacy module-level names
    # are kept as no-op back-compat (assigning ``None`` is harmless);
    # the load-bearing reset is ``_reset_pool_for_tests()``.
    agent_mod._sdk_client = None
    agent_mod._sdk_options_fingerprint = None
    agent_mod._sdk_lock = None
    agent_mod._reset_pool_for_tests()
    _FakeClaudeSDKClient.instances.clear()
    _FakeClaudeSDKClient.get_context_usage_count = 0


@pytest.fixture(autouse=True)
def _isolate_sdk_state(monkeypatch):
    """Each test starts with a clean module-level state and a patched
    ClaudeSDKClient."""
    _reset_module_state()
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", _FakeClaudeSDKClient)
    yield
    _reset_module_state()


def _opts(**overrides) -> ClaudeAgentOptions:
    base = dict(
        system_prompt="hello system",
        model="claude-opus-4-7",
        cwd="/tmp",
        permission_mode="bypassPermissions",
    )
    base.update(overrides)
    return ClaudeAgentOptions(**base)


async def _drain(
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    session_id: str | None = None,
) -> list:
    out = []
    kwargs: dict[str, Any] = {"prompt": prompt, "options": options}
    if session_id is not None:
        kwargs["session_id"] = session_id
    async for msg in agent_mod.query(**kwargs):
        out.append(msg)
    return out


@pytest.mark.asyncio
async def test_first_call_connects_and_yields_reply():
    msgs = await _drain("hi there", _opts())

    assert len(_FakeClaudeSDKClient.instances) == 1
    client = _FakeClaudeSDKClient.instances[0]
    assert client.connect_count == 1
    assert client.disconnect_count == 0
    assert client.queries == [("hi there", "default")]
    assert len(msgs) == 1
    assert isinstance(msgs[0], AssistantMessage)


@pytest.mark.asyncio
async def test_second_call_with_same_options_reuses_client():
    await _drain("first", _opts())
    await _drain("second", _opts())

    # Still exactly one client instance, two queries against it.
    assert len(_FakeClaudeSDKClient.instances) == 1
    client = _FakeClaudeSDKClient.instances[0]
    assert client.connect_count == 1
    assert client.disconnect_count == 0
    assert client.queries == [("first", "default"), ("second", "default")]


@pytest.mark.asyncio
async def test_options_change_recycles_client():
    await _drain("first", _opts(system_prompt="prompt-a"))
    await _drain("second", _opts(system_prompt="prompt-b"))

    # Two clients; first was disconnected before the second was constructed.
    assert len(_FakeClaudeSDKClient.instances) == 2
    a, b = _FakeClaudeSDKClient.instances
    assert a.disconnect_count == 1
    assert a.queries == [("first", "default")]
    assert b.connect_count == 1
    assert b.disconnect_count == 0
    assert b.queries == [("second", "default")]


@pytest.mark.asyncio
async def test_model_change_recycles_client():
    await _drain("first", _opts(model="claude-opus-4-7"))
    await _drain("second", _opts(model="claude-haiku-4-5"))

    assert len(_FakeClaudeSDKClient.instances) == 2
    assert _FakeClaudeSDKClient.instances[0].disconnect_count == 1


@pytest.mark.asyncio
async def test_shutdown_disconnects_and_is_idempotent():
    await _drain("hi", _opts())
    client = _FakeClaudeSDKClient.instances[0]
    assert client.disconnect_count == 0

    await agent_mod.shutdown_sdk_client()
    assert client.disconnect_count == 1
    assert agent_mod._sdk_client is None

    # Idempotent — second call is a no-op.
    await agent_mod.shutdown_sdk_client()
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_shutdown_with_no_client_is_safe():
    # Never called query(); shutdown should not raise.
    await agent_mod.shutdown_sdk_client()


@pytest.mark.asyncio
async def test_shutdown_after_disconnect_failure_still_clears_state():
    """If disconnect() raises during shutdown, the module-level client
    reference is still cleared so subsequent calls construct a fresh
    one rather than re-using a broken handle."""

    class _ExplodingClient(_FakeClaudeSDKClient):
        async def disconnect(self) -> None:
            await super().disconnect()
            raise RuntimeError("boom")

    # Swap in the exploding fake for this test only.
    instances_before = len(_FakeClaudeSDKClient.instances)
    agent_mod.ClaudeSDKClient = _ExplodingClient  # type: ignore[assignment]
    try:
        await _drain("hi", _opts())
        await agent_mod.shutdown_sdk_client()
    finally:
        agent_mod.ClaudeSDKClient = _FakeClaudeSDKClient  # type: ignore[assignment]

    assert agent_mod._sdk_client is None
    assert agent_mod._sdk_options_fingerprint is None
    # _ExplodingClient inherits from _FakeClaudeSDKClient, so a new
    # instance was registered.
    assert len(_FakeClaudeSDKClient.instances) == instances_before + 1


@pytest.mark.asyncio
async def test_session_id_defaults_when_caller_omits_it():
    """Callers that don't pass ``session_id`` get the historical
    ``"default"`` accumulating-session behavior. The agent loop opts
    in to per-turn isolation by passing ``session_id=ctx.turn_id``;
    other callers (and existing tests) keep working unchanged."""
    await _drain("a", _opts())
    await _drain("b", _opts())
    client = _FakeClaudeSDKClient.instances[0]
    assert [s for _, s in client.queries] == ["default", "default"]


# ─── Stage 2: per-turn session_id ───────────────────────────────────


@pytest.mark.asyncio
async def test_session_id_is_forwarded_to_client_query():
    """The wrapper passes ``session_id`` straight through to
    ``client.query()``. The SDK's session store keys on this id, so
    forwarding is the load-bearing invariant for per-turn isolation."""
    await _drain("hello", _opts(), session_id="turn-abc123")
    client = _FakeClaudeSDKClient.instances[0]
    assert client.queries == [("hello", "turn-abc123")]


@pytest.mark.asyncio
async def test_distinct_session_ids_isolate_history_in_shared_client():
    """Two turns with different ``session_id`` values share the same
    ``ClaudeSDKClient`` (the persistent wrapper) but the per-call
    session_id flows through unchanged. The SDK's session store
    scopes history by session_id, so turn N+1's input does not see
    turn N's content even though the underlying client / subprocess
    is the same.

    We verify the wrapper-level invariant: the recorded
    ``client.query()`` calls carry distinct session_ids in order.
    The fake's per-session_id history mirrors what the real SDK's
    ``InMemorySessionStore`` does."""

    # A richer fake that actually keys history per session_id, the
    # way the SDK's InMemorySessionStore does. Lets us assert that
    # session-A's prompts never appear in session-B's history.
    class _SessionAwareFake(_FakeClaudeSDKClient):
        history: dict[str, list[str]] = {}

        async def query(self, prompt: str, session_id: str = "default") -> None:
            await super().query(prompt, session_id)
            type(self).history.setdefault(session_id, []).append(prompt)

    _SessionAwareFake.history = {}
    agent_mod.ClaudeSDKClient = _SessionAwareFake  # type: ignore[assignment]
    try:
        await _drain("apple", _opts(), session_id="turn-1")
        await _drain("banana", _opts(), session_id="turn-2")
    finally:
        agent_mod.ClaudeSDKClient = _FakeClaudeSDKClient  # type: ignore[assignment]

    # Same persistent client served both turns.
    assert len(_FakeClaudeSDKClient.instances) == 1
    client = _FakeClaudeSDKClient.instances[0]
    assert client.connect_count == 1
    assert client.queries == [("apple", "turn-1"), ("banana", "turn-2")]

    # Per-session histories are disjoint — turn-1's prompt never
    # appears in turn-2's history and vice versa.
    assert _SessionAwareFake.history == {
        "turn-1": ["apple"],
        "turn-2": ["banana"],
    }


@pytest.mark.asyncio
async def test_same_session_id_accumulates_history_within_one_turn():
    """If a single session_id is reused (e.g., a multi-message turn
    or an explicit ``"default"`` accumulating session), the SDK's
    session store appends to that session's history. Verifies the
    wrapper doesn't reset session state between calls with the same
    id."""

    class _SessionAwareFake(_FakeClaudeSDKClient):
        history: dict[str, list[str]] = {}

        async def query(self, prompt: str, session_id: str = "default") -> None:
            await super().query(prompt, session_id)
            type(self).history.setdefault(session_id, []).append(prompt)

    _SessionAwareFake.history = {}
    agent_mod.ClaudeSDKClient = _SessionAwareFake  # type: ignore[assignment]
    try:
        await _drain("first", _opts(), session_id="turn-X")
        await _drain("second", _opts(), session_id="turn-X")
    finally:
        agent_mod.ClaudeSDKClient = _FakeClaudeSDKClient  # type: ignore[assignment]

    assert _SessionAwareFake.history == {"turn-X": ["first", "second"]}


# ─── Stage 5: get_context_usage off the warm client ─────────────────


@pytest.mark.asyncio
async def test_get_context_usage_reuses_warm_client():
    """A turn just queried; calling ``get_context_usage`` after with
    the same options reuses the warm client (same fingerprint) instead
    of reconnecting."""
    opts = _opts()
    await _drain("hello", opts)
    assert len(_FakeClaudeSDKClient.instances) == 1
    client = _FakeClaudeSDKClient.instances[0]
    assert client.connect_count == 1

    response = await agent_mod.get_context_usage(opts)
    # Same client served both — no recycle, no extra connect.
    assert len(_FakeClaudeSDKClient.instances) == 1
    assert client.connect_count == 1
    assert client.disconnect_count == 0
    assert _FakeClaudeSDKClient.get_context_usage_count == 1
    assert response is not None
    assert "apiUsage" in response


@pytest.mark.asyncio
async def test_get_context_usage_connects_when_no_warm_client():
    """No prior query() has run — the wrapper still connects a fresh
    client to service the usage probe rather than returning None.
    Plan-window data is observability; first-turn capture is fine."""
    response = await agent_mod.get_context_usage(_opts())
    assert response is not None
    assert len(_FakeClaudeSDKClient.instances) == 1
    client = _FakeClaudeSDKClient.instances[0]
    assert client.connect_count == 1


@pytest.mark.asyncio
async def test_get_context_usage_recycles_on_options_change():
    """Different options-fingerprint after the warm client was set up
    forces a recycle, same as ``query()``. Avoids stale system_prompt
    bleed if the agent loop's options drifted between query and
    capture."""
    await _drain("hi", _opts(system_prompt="prompt-a"))
    await agent_mod.get_context_usage(_opts(system_prompt="prompt-b"))
    assert len(_FakeClaudeSDKClient.instances) == 2
    assert _FakeClaudeSDKClient.instances[0].disconnect_count == 1


@pytest.mark.asyncio
async def test_get_context_usage_swallows_get_context_usage_failure():
    """``get_context_usage`` is best-effort — when the underlying call
    raises, the wrapper logs and returns None rather than propagating.
    The agent loop treats None the same as an empty response."""

    class _RaisingFake(_FakeClaudeSDKClient):
        async def get_context_usage(self) -> dict:
            raise RuntimeError("daemon disconnected")

    agent_mod.ClaudeSDKClient = _RaisingFake  # type: ignore[assignment]
    try:
        response = await agent_mod.get_context_usage(_opts())
    finally:
        agent_mod.ClaudeSDKClient = _FakeClaudeSDKClient  # type: ignore[assignment]
    assert response is None


@pytest.mark.asyncio
async def test_get_context_usage_swallows_connect_failure():
    """When connect() raises during the on-demand client construction,
    the wrapper returns None instead of propagating. The next turn's
    capture attempt is independent."""

    class _ConnectFails(_FakeClaudeSDKClient):
        async def connect(self) -> None:
            raise RuntimeError("connect failed")

    agent_mod.ClaudeSDKClient = _ConnectFails  # type: ignore[assignment]
    try:
        response = await agent_mod.get_context_usage(_opts())
    finally:
        agent_mod.ClaudeSDKClient = _FakeClaudeSDKClient  # type: ignore[assignment]
    assert response is None
    # No client should be retained — the connect failure must clear
    # the singleton so the next call constructs a fresh one.
    assert agent_mod._sdk_client is None
