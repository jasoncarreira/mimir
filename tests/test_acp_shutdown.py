from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.acp.agent import ConnectionState, MimirAcpAgent
from mimir.acp.host import _FrameDelivery, close_protocol_writer
from mimir.acp.proxy import ProxyRouter, _OutputWriter, run_router
from mimir.acp.transport import close_writer, pump_stream


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
async def test_eof_stage_preserves_descriptor_ownership() -> None:
    class Writer:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.closed = False
        def write_eof(self) -> None: self.events.append("eof")
        async def drain(self) -> None: self.events.append("drain")
        def close(self) -> None: self.closed = True

    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = Writer()
    await pump_stream(reader, writer)
    assert writer.events == ["eof", "drain"]
    assert not writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["write", "flush"])
async def test_peer_disconnect_write_and_flush_are_reported(stage: str) -> None:
    class Sink(io.BytesIO):
        def write(self, data: bytes) -> int:
            if stage == "write": raise BrokenPipeError
            return super().write(data)
        def flush(self) -> None:
            if stage == "flush": raise ConnectionResetError
            super().flush()

    writer = _OutputWriter(Sink())
    with pytest.raises((BrokenPipeError, ConnectionResetError)):
        writer.write(b"frame\n")
    assert not writer.closed


@pytest.mark.asyncio
async def test_writer_close_uses_exact_finite_drain_close_and_abort_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []
    stages: list[str] = []

    async def wait_for(awaitable: object, timeout: float) -> object:
        timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError

    class Transport:
        def abort(self) -> None: stages.append("abort")

    class Writer:
        transport = Transport()
        async def drain(self) -> None: await asyncio.Future()
        def close(self) -> None: stages.append("close")
        async def wait_closed(self) -> None: await asyncio.Future()

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await close_writer(Writer())
    assert timeouts == [2.0, 1.0, 1.0]
    assert stages == ["close", "abort"]


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


@pytest.mark.asyncio
async def test_proxy_generation_teardown_retires_hosted_ids_grants_calls_and_workers(
    tmp_path: Path,
) -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

    router = ProxyRouter(Writer(), Writer(), "secret")
    router._active_sessions.add("session")
    router._grants.add("session", "hands_python")
    router._server_sessions["server"] = "session"
    router._connection_sessions["connection"] = "session"
    router._provider.bind_session("session", tmp_path)
    connection_id = router._provider.connect("session")
    await router._provider.request(
        connection_id,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    await router._provider.notification(connection_id, "notifications/initialized")
    await router._provider._python_kernels.execute("session", tmp_path, "value = 1")
    worker = next(iter(router._provider._python_kernels._processes))
    shell_call = asyncio.create_task(
        router._provider.request(
            connection_id,
            "tools/call",
            {"name": "shell", "arguments": {"command": "sleep 30"}},
            request_id="shell",
        )
    )
    async with asyncio.timeout(5):
        while not router._provider._processes:
            await asyncio.sleep(0.01)
    shell = next(iter(router._provider._processes))
    router._fail_generation(ConnectionError("generation retired"))
    failure = await router.wait_failed()
    assert str(failure) == "generation retired"
    assert router._active_sessions == set()
    assert len(router._grants) == 0
    assert router._server_sessions == {}
    assert router._connection_sessions == {}
    assert router._used_server_ids == set()
    assert router._used_connection_ids == set()
    assert router._local_requests == {}
    assert router._daemon_requests == {}
    assert router._provider._connections == {}
    assert router._provider._processes == {}
    assert router._provider._python_kernels._processes == {}
    assert shell.returncode is not None
    assert worker.returncode is not None
    assert shell_call.done()
    await asyncio.gather(shell_call, return_exceptions=True)
    await router.close()


@pytest.mark.asyncio
async def test_daemon_eof_retires_generation_before_client_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def is_closing(self) -> bool:
            return self.closed

        async def wait_closed(self) -> None:
            return None

    client_writer = Writer()
    daemon_writer = Writer()
    router = ProxyRouter(client_writer, daemon_writer, "secret")
    router._active_sessions.add("session")
    router._grants.add("session", "hands_python")
    router._server_sessions["server"] = "session"
    router._server_provider_sessions["server"] = "session"
    router._provider.bind_session("session", tmp_path)
    connection_id = router._provider.connect("session")
    router._connection_sessions[connection_id] = "session"
    router._connection_provider_sessions[connection_id] = "session"
    await router._provider.request(
        connection_id,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    await router._provider.notification(connection_id, "notifications/initialized")
    await router._provider._python_kernels.execute("session", tmp_path, "value = 1")
    worker = next(iter(router._provider._python_kernels._processes))
    await router.route_daemon({
        "jsonrpc": "2.0",
        "id": "shell",
        "method": "mcp/message",
        "params": {
            "connectionId": connection_id,
            "method": "tools/call",
            "params": {"name": "shell", "arguments": {"command": "sleep 30"}},
        },
    })
    async with asyncio.timeout(5):
        while not router._provider._processes:
            await asyncio.sleep(0.01)
    shell = next(iter(router._provider._processes))
    client_reader = asyncio.StreamReader()
    daemon_reader = asyncio.StreamReader()
    monkeypatch.setattr(
        "mimir.acp.proxy.ProxyRouter",
        lambda client, daemon, credential, timeout_seconds=60: router,
    )
    monkeypatch.setattr("mimir.acp.proxy.PEER_EOF_GRACE_TIMEOUT", 30.0)
    running = asyncio.create_task(
        run_router(
            client_reader,
            client_writer,
            daemon_reader,
            daemon_writer,
            "secret",
        )
    )
    daemon_reader.feed_eof()
    async with asyncio.timeout(5):
        while router._provider._processes or router._provider._python_kernels._processes:
            await asyncio.sleep(0.01)
    assert not running.done()
    assert router._active_sessions == set()
    assert len(router._grants) == 0
    assert router._server_sessions == {}
    assert router._connection_sessions == {}
    assert router._local_requests == {}
    assert router._daemon_requests == {}
    assert router._provider._connections == {}
    assert shell.returncode is not None
    assert worker.returncode is not None
    client_reader.feed_eof()
    await asyncio.wait_for(running, 5)
