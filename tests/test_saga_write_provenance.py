"""Server-owned SAGA write provenance and concurrent-call isolation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain.tools import ToolRuntime

from mimir.access_control import create_auth_context
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, AuthContext
from mimir.tools import saga_ops
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.store import memory_store


class _WriteStore:
    def __init__(self) -> None:
        self.atom_calls: list[dict[str, Any]] = []
        self.session_calls: list[dict[str, Any]] = []

    async def store(self, content: str, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        self.atom_calls.append({"content": content, **kwargs})
        return {"atom_id": f"atom-{len(self.atom_calls)}", "stored": True}

    async def end_session(self, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        self.session_calls.append(kwargs)
        return {"session_summary_written": True}


@pytest.fixture
def write_store() -> _WriteStore:
    store = _WriteStore()
    previous = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = store
    yield store
    _MEMORY_STATE["client"] = previous


def _runtime(context: AuthContext, call_id: str) -> ToolRuntime[AuthContext]:
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id=call_id, store=None,
    )


def _resolver(tmp_path: Path) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    path = state / "identities.yaml"
    path.write_text(
        """people:
  - canonical: alice
    aliases: [discord-alice]
    access: {roles: [user, admin]}
  - canonical: bob
    aliases: [slack-bob]
    access: {roles: [user, admin]}
  - canonical: regular
    aliases: [discord-regular]
    access: {roles: [user]}
"""
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def _user_context(tmp_path: Path, author: str, channel: str) -> AuthContext:
    return create_auth_context(
        AgentEvent(
            trigger="user_message", channel_id=channel, author=author,
            source="discord" if channel.startswith("discord") else "slack",
        ),
        _resolver(tmp_path),
        enforce=True,
    )


def _service_context(trigger: str, channel: str) -> AuthContext:
    principals = {
        "scheduled_tick": "scheduler",
        "poller": "poller",
        "saga_session_end": "synthesis",
        "upgrade": "system",
    }
    return create_auth_context(
        AgentEvent(
            trigger=trigger,
            channel_id=channel,
            service_principal=principals[trigger],
        ),
        enforce=True,
    )


@pytest.mark.asyncio
async def test_memory_store_regular_user_is_rejected(
    tmp_path: Path, write_store: _WriteStore,
) -> None:
    context = _user_context(tmp_path, "discord-regular", "discord-private")
    out = await memory_store.ainvoke({
        "content": "not allowed", "stream": "semantic",
        "runtime": _runtime(context, "regular-store"),
    })
    assert "write access denied" in out
    assert write_store.atom_calls == []


@pytest.mark.asyncio
async def test_concurrent_memory_writes_keep_runtime_owned_provenance(
    tmp_path: Path, write_store: _WriteStore,
) -> None:
    alice = _user_context(tmp_path, "discord-alice", "discord-a")
    bob = _user_context(tmp_path, "slack-bob", "slack-b")

    await asyncio.gather(
        memory_store.ainvoke({
            "content": "alice fact", "stream": "semantic",
            "session_id": "model-label-a",
            "runtime": _runtime(alice, "alice-store"),
        }),
        memory_store.ainvoke({
            "content": "bob fact", "stream": "semantic",
            "session_id": "model-label-b",
            "runtime": _runtime(bob, "bob-store"),
        }),
    )

    by_content = {call["content"]: call for call in write_store.atom_calls}
    assert by_content["alice fact"]["owner_principal"] == "alice"
    assert by_content["alice fact"]["origin_channel"] == "discord-a"
    assert by_content["alice fact"]["session_id"] == "model-label-a"
    assert by_content["bob fact"]["owner_principal"] == "bob"
    assert by_content["bob fact"]["origin_channel"] == "slack-b"
    assert by_content["bob fact"]["session_id"] == "model-label-b"


@pytest.mark.parametrize(
    "trigger",
    ["scheduled_tick", "poller", "upgrade"],
)
@pytest.mark.asyncio
async def test_service_without_memory_store_capability_is_denied(
    trigger: str, write_store: _WriteStore,
) -> None:
    context = _service_context(trigger, f"{trigger}:owned")
    out = await memory_store.ainvoke({
        "content": trigger, "stream": "episodic",
        "runtime": _runtime(context, f"{trigger}-store"),
    })

    assert "write access denied" in out
    assert write_store.atom_calls == []


@pytest.mark.asyncio
async def test_synthesis_memory_store_preserves_service_provenance(
    write_store: _WriteStore,
) -> None:
    trigger = "saga_session_end"
    context = _service_context(trigger, f"{trigger}:owned")
    out = await memory_store.ainvoke({
        "content": trigger, "stream": "episodic",
        "runtime": _runtime(context, f"{trigger}-store"),
    })

    assert "stored" in out
    call = write_store.atom_calls[-1]
    assert call["owner_principal"] == "service:synthesis"
    assert call["origin_channel"] == f"{trigger}:owned"
    assert call["visibility"] == "service"


@pytest.mark.asyncio
async def test_saga_end_session_uses_runtime_not_model_session_for_authority(
    write_store: _WriteStore,
) -> None:
    context = _service_context("saga_session_end", "discord-synthesis")
    out = await saga_ops.saga_end_session.ainvoke({
        "session_id": "model-provided-row-id", "summary": "done",
        "runtime": _runtime(context, "synthesis-end"),
    })
    assert "ok" in out
    call = write_store.session_calls[-1]
    assert call["session_id"] == "model-provided-row-id"
    assert call["owner_principal"] == "service:synthesis"
    assert call["origin_channel"] == "discord-synthesis"
    assert call["visibility"] == "service"


@pytest.mark.asyncio
async def test_http_trigger_spoof_does_not_gain_service_authority(
    write_store: _WriteStore,
) -> None:
    context = create_auth_context(
        AgentEvent(
            trigger="scheduled_tick", channel_id="http-spoof",
            extra={"event_ingress": "http_event"},
        ),
        enforce=True,
    )
    out = await memory_store.ainvoke({
        "content": "spoof", "stream": "semantic",
        "runtime": _runtime(context, "spoof-store"),
    })
    assert "write access denied" in out
    assert write_store.atom_calls == []
