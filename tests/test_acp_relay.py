from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import pytest

from mimir.acp.relay import _Output, _output_writer, RelayError, _socket, run_relay


def test_relay_requires_absolute_home() -> None:
    with pytest.raises(RelayError):
        _socket(Path("relative"))


class Partial(io.BytesIO):
    def write(self, data: bytes) -> int:
        return super().write(bytes(data[:2]))


def test_relay_output_retries_partial_writes() -> None:
    stream = Partial()
    _Output(stream).write(b"complete")
    assert stream.getvalue() == b"complete"


@pytest.mark.asyncio
async def test_relay_pipe_output_uses_nonblocking_event_loop_transport() -> None:
    read_fd, write_fd = os.pipe()
    output = os.fdopen(write_fd, "wb", buffering=0)
    writer = await _output_writer(output)
    try:
        assert isinstance(writer, asyncio.StreamWriter)
        writer.write(b"nonblocking")
        await writer.drain()
        assert os.read(read_fd, 11) == b"nonblocking"
    finally:
        writer.close()
        await asyncio.sleep(0)
        os.close(read_fd)


def test_relay_is_blind_to_protocol_profiles_credentials_and_runtime() -> None:
    source = Path(__import__("mimir.acp.relay", fromlist=["x"]).__file__).read_text()
    for forbidden in ("json", "authenticate", "mimir.webKey", "profiles", "credentials", "runtime", "server"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_relay_round_trip_and_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = Path("/tmp") / f"mimir-relay-{os.getpid()}"
    socket = home / ".mimir" / "acp" / "daemon.sock"
    socket.parent.mkdir(parents=True, mode=0o700)
    os.chmod(socket.parent, 0o700)
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write((await reader.read())[::-1])
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(handler, path=str(socket))
    reader = asyncio.StreamReader()
    reader.feed_data(b"relay")
    reader.feed_eof()
    output = io.BytesIO()
    transport = type("Transport", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    async def stdio(target: object) -> tuple[object, object, object]:
        return reader, _Output(output), transport
    monkeypatch.setattr("mimir.acp.relay._stdio", stdio)
    try:
        await run_relay(home, output)
    finally:
        server.close()
        await server.wait_closed()
        socket.unlink(missing_ok=True)
        socket.parent.rmdir()
        home.joinpath(".mimir").rmdir()
        home.rmdir()
    assert output.getvalue() == b"yaler"
    assert transport.closed


@pytest.mark.asyncio
async def test_dead_relay_client_promptly_closes_daemon_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    client_reader = asyncio.StreamReader()
    daemon_reader = asyncio.StreamReader()
    daemon_closed = asyncio.Event()
    stdout_read_fd, stdout_write_fd = os.pipe()
    stdout = os.fdopen(stdout_write_fd, "wb", buffering=0)
    client_writer = await _output_writer(stdout)

    class DaemonWriter(_Output):
        def write_eof(self) -> None:
            pass

        def close(self) -> None:
            super().close()
            daemon_closed.set()

    daemon_writer = DaemonWriter(io.BytesIO())
    stdin_transport = type("Transport", (), {"close": lambda self: None})()

    async def open_daemon(path: str) -> tuple[asyncio.StreamReader, DaemonWriter]:
        return daemon_reader, daemon_writer

    async def stdio(target: object) -> tuple[object, object, object]:
        return client_reader, client_writer, stdin_transport

    monkeypatch.setattr("mimir.acp.relay._socket", lambda home: tmp_path / "socket")
    monkeypatch.setattr("mimir.acp.relay.asyncio.open_unix_connection", open_daemon)
    monkeypatch.setattr("mimir.acp.relay._stdio", stdio)

    relay = asyncio.create_task(run_relay(tmp_path, io.BytesIO()))
    await asyncio.sleep(0.02)
    assert not daemon_closed.is_set(), "a quiet live relay client was evicted"

    os.close(stdout_read_fd)
    await asyncio.sleep(0.02)
    assert client_writer.is_closing()
    client_reader.feed_eof()
    await asyncio.wait_for(daemon_closed.wait(), 0.1)
    await asyncio.wait_for(relay, 0.1)


@pytest.mark.asyncio
async def test_relay_connect_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("mimir.acp.relay._socket", lambda home: tmp_path / "socket")
    async def blocked(path: str) -> None:
        await asyncio.sleep(10)
    monkeypatch.setattr("mimir.acp.relay.asyncio.open_unix_connection", blocked)
    monkeypatch.setattr("mimir.acp.relay.CONNECT_TIMEOUT", 0.01)
    with pytest.raises(TimeoutError):
        await run_relay(tmp_path, io.BytesIO())
