"""Stage 1 of CLAUDE_SDK_CLIENT_MIGRATION.md.

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


def _reset_module_state() -> None:
    agent_mod._sdk_client = None
    agent_mod._sdk_options_fingerprint = None
    agent_mod._sdk_lock = None
    _FakeClaudeSDKClient.instances.clear()


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


async def _drain(prompt: str, options: ClaudeAgentOptions) -> list:
    out = []
    async for msg in agent_mod.query(prompt=prompt, options=options):
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
async def test_session_id_is_default_for_stage_1():
    """Stage 1 keeps session_id='default' so history accumulates the
    same way as the legacy query() path. Stage 2 will switch to
    per-turn turn_id."""
    await _drain("a", _opts())
    await _drain("b", _opts())
    client = _FakeClaudeSDKClient.instances[0]
    assert [s for _, s in client.queries] == ["default", "default"]
