from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import pytest

from mimir.acp.relay import _Output, RelayError, _socket, run_relay


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
async def test_relay_connect_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("mimir.acp.relay._socket", lambda home: tmp_path / "socket")
    async def blocked(path: str) -> None:
        await asyncio.sleep(10)
    monkeypatch.setattr("mimir.acp.relay.asyncio.open_unix_connection", blocked)
    monkeypatch.setattr("mimir.acp.relay.CONNECT_TIMEOUT", 0.01)
    with pytest.raises(TimeoutError):
        await run_relay(tmp_path, io.BytesIO())
