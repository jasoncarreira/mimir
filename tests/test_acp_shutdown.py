from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

from mimir.acp.agent import ConnectionState, MimirAcpAgent
from mimir.acp.host import _FrameDelivery, close_protocol_writer


@pytest.mark.asyncio
async def test_frame_delivery_is_bounded_and_terminal() -> None:
    stream = io.BytesIO()
    errors: list[BaseException] = []
    delivery = _FrameDelivery(stream, 16, errors.append)
    assert delivery.write(b"frame") == 5
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"frame"
    assert not errors


@pytest.mark.asyncio
async def test_frame_delivery_rejects_capacity() -> None:
    delivery = _FrameDelivery(io.BytesIO(), 2, lambda error: None)
    with pytest.raises(BufferError):
        delivery.write(b"long")
    with pytest.raises(BufferError):
        await delivery.wait_terminal()
    delivery.join()


class Partial(io.BytesIO):
    def write(self, data: bytes) -> int:
        return super().write(bytes(data[:2]))


@pytest.mark.asyncio
async def test_frame_delivery_handles_partial_writes() -> None:
    stream = Partial()
    delivery = _FrameDelivery(stream, 32, lambda error: None)
    delivery.write(b"complete")
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"complete"


@pytest.mark.asyncio
async def test_frame_delivery_sustained_ingress_is_ordered_and_bounded() -> None:
    stream = io.BytesIO()
    delivery = _FrameDelivery(stream, 16, lambda error: None)
    frames = [f"{index:03d}\n".encode() for index in range(200)]
    for payload in frames:
        while delivery.reserved_bytes > 11:
            await asyncio.sleep(0)
        delivery.write(payload)
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"".join(frames)
    assert delivery.peak_reserved_bytes <= 16


@pytest.mark.asyncio
async def test_frame_delivery_protocol_failures_reach_owner() -> None:
    class Broken(io.BytesIO):
        def write(self, data: bytes) -> int:
            raise OSError("sink failed")
    errors: list[BaseException] = []
    delivery = _FrameDelivery(Broken(), 64, errors.append)
    delivery.write(b"frame")
    delivery.finish()
    with pytest.raises(OSError, match="sink failed"):
        await delivery.wait_terminal()
    delivery.join()
    assert len(errors) == 1 and isinstance(errors[0], OSError)


@pytest.mark.asyncio
async def test_protocol_writer_uses_bounded_transport_close(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    async def close(writer: object) -> None:
        calls.append(writer)
    monkeypatch.setattr("mimir.acp.host.close_writer", close)
    writer = object()
    await close_protocol_writer(writer)
    assert calls == [writer]


@pytest.mark.asyncio
async def test_transport_death_tears_down_only_bound_generation() -> None:
    class Peer:
        def __init__(self) -> None:
            self.disconnects: list[str] = []
        async def disconnect_mcp(self, connection_id: str) -> None:
            self.disconnects.append(connection_id)

    old_peer = Peer()
    new_peer = Peer()
    old_connection = ConnectionState(1, old_peer)
    new_connection = ConnectionState(2, new_peer)
    old_provider = SimpleNamespace(peer=old_peer, connection_id="old", closed=False)
    new_provider = SimpleNamespace(peer=new_peer, connection_id="new", closed=False)
    old_state = SimpleNamespace(generation=1, active_prompt=None, provider=old_provider, record=SimpleNamespace(session_id="old-only"))
    successor = SimpleNamespace(generation=2, active_prompt=None, provider=new_provider, record=SimpleNamespace(session_id="shared"))
    old_connection.connection_sessions["old"] = old_state
    new_connection.connection_sessions["new"] = successor
    agent = object.__new__(MimirAcpAgent)
    agent._connections = {1: old_connection, 2: new_connection}
    agent._connection = new_connection
    agent._client = new_peer
    agent._bridge = SimpleNamespace(_connected=True)
    agent._sessions = {"old-only": old_state, "shared": successor}
    agent._environments = {"old-only": (1, object()), "shared": (2, object())}
    agent._boundary_lock = asyncio.Lock()
    await agent.on_transport_closed(1)
    assert "old-only" not in agent._sessions
    assert agent._sessions["shared"] is successor
    assert agent._connection is new_connection
    assert new_provider.closed is False


@pytest.mark.asyncio
async def test_inbound_generation_identity_prevents_connection_id_collision() -> None:
    old_state = SimpleNamespace(generation=1, record=SimpleNamespace(session_id="old"))
    new_state = SimpleNamespace(generation=2, record=SimpleNamespace(session_id="new"))
    old_connection = ConnectionState(1, object())
    new_connection = ConnectionState(2, object())
    old_connection.connection_sessions["collision"] = old_state
    new_connection.connection_sessions["collision"] = new_state
    agent = object.__new__(MimirAcpAgent)
    agent._connections = {1: old_connection, 2: new_connection}
    observed: list[tuple[int, str]] = []
    async def revalidate(state: object) -> None:
        observed.append((state.generation, state.record.session_id))
    agent._revalidate_provider = revalidate
    await agent.on_mcp_notification(1, "collision", "notifications/tools/list_changed", None)
    await agent.on_mcp_notification(2, "collision", "notifications/tools/list_changed", None)
    await asyncio.gather(*old_connection.tasks, *new_connection.tasks)
    assert observed == [(1, "old"), (2, "new")]


@pytest.mark.asyncio
async def test_candidate_connection_does_not_retire_active_generation() -> None:
    agent = object.__new__(MimirAcpAgent)
    old = ConnectionState(1, object())
    agent._connection = old
    agent._connections = {1: old}
    agent._generation = 1
    agent._client = old.peer
    agent._auth_context = object()
    agent._display_name = "old"
    agent._bridge = SimpleNamespace(_connected=True)
    agent._active_prompts = {}
    agent._environments = {}
    agent._retirement_tasks = set()
    retired = asyncio.Event()
    async def retire(generation: int) -> None:
        retired.set()
    agent._retire_replaced_generation = retire
    candidate = object()
    generation = agent.on_connect(candidate)
    await asyncio.sleep(0)
    assert generation == 2
    assert agent._connection is old
    assert not retired.is_set()
