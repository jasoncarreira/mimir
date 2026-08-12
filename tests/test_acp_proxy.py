from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path

import pytest

from mimir.acp.profiles import Profile, ProfileStore
from mimir.acp.proxy import MAX_FRAME_BYTES, FrameWriter, ProxyError, run_local_proxy, run_proxy


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None: self.data.extend(data)
    async def drain(self) -> None: return None
    def write_eof(self) -> None: return None
    def close(self) -> None: self.closed = True
    def is_closing(self) -> bool: return self.closed
    async def wait_closed(self) -> None: return None


def frame(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


@pytest.mark.asyncio
async def test_authenticate_overwrites_reserved_metadata() -> None:
    writer = Writer()
    transformer = FrameWriter(writer, "raw-key")
    transformer.write(frame({"method": "authenticate", "params": {"methodId": "mimir-web-key", "_meta": {"ok": 1, "mimir": "x", "mimir.fake": "x"}}}))
    await transformer.drain()
    assert json.loads(writer.data)["params"]["_meta"] == {"ok": 1, "mimir.webKey": "raw-key"}


@pytest.mark.parametrize("message", [
    {"method": "authenticate"},
    {"method": "authenticate", "params": None},
    {"method": "authenticate", "params": {"_meta": []}},
    [],
])
def test_authenticate_requires_bounded_object_shape(message: object) -> None:
    with pytest.raises(ProxyError, match="invalid frame"):
        FrameWriter(Writer(), "SECRET").write(frame(message))


def test_transformer_enforces_input_and_output_bound() -> None:
    transformer = FrameWriter(Writer(), "SECRET")
    with pytest.raises(ProxyError, match="invalid frame"):
        transformer.write(b"x" * (MAX_FRAME_BYTES + 1))
    oversized_after_auth = {"method": "authenticate", "params": {"_meta": {"padding": "x" * (MAX_FRAME_BYTES - 40)}}}
    with pytest.raises(ProxyError, match="invalid frame"):
        FrameWriter(Writer(), "SECRET").write(frame(oversized_after_auth))


def test_malformed_frames_fail_without_echoing_bytes() -> None:
    writer = Writer()
    with pytest.raises(ProxyError, match="invalid frame"):
        FrameWriter(writer, "SECRET").write(b"SENTINEL-not-json\n")
    assert bytes(writer.data) == b""


@pytest.mark.asyncio
async def test_local_proxy_connects_and_stdout_contains_only_protocol(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = Path("/tmp") / f"mimir-proxy-{os.getpid()}"
    socket = home / ".mimir" / "acp" / "daemon.sock"
    socket.parent.mkdir(parents=True, mode=0o700)
    os.chmod(socket.parent, 0o700)
    received = bytearray()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.extend(await reader.readline())
        writer.write(frame({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket))
    input_reader = asyncio.StreamReader()
    input_reader.feed_data(frame({"jsonrpc": "2.0", "id": 1, "method": "authenticate", "params": {"methodId": "mimir-web-key"}}))
    input_reader.feed_eof()
    transport = type("Transport", (), {"close": lambda self: None})()
    output = io.BytesIO()
    monkeypatch.setattr("mimir.acp.proxy.open_stdio", lambda target: asyncio.sleep(0, result=(input_reader, WriterToFile(target), transport)))
    try:
        await run_local_proxy(Profile("default", home), "raw-key", output)
    finally:
        server.close()
        await server.wait_closed()
        socket.unlink(missing_ok=True)
        socket.parent.rmdir()
        home.joinpath(".mimir").rmdir()
        home.rmdir()
    assert json.loads(received)["params"]["_meta"] == {"mimir.webKey": "raw-key"}
    assert output.getvalue() == frame({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    assert b"raw-key" not in output.getvalue()


class WriterToFile(Writer):
    def __init__(self, output: io.BytesIO) -> None:
        super().__init__()
        self.output = output

    def write(self, data: bytes) -> None: self.output.write(data)


@pytest.mark.asyncio
async def test_invalid_key_is_retrieved_from_owned_credential_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.set(Profile("default", Path("/home")))
    credentials = type("Credentials", (), {"get": lambda self, name: "invalid-key"})()
    observed: list[str] = []
    async def local(profile: Profile, credential: str, output: object) -> None:
        observed.append(credential)
    monkeypatch.setattr("mimir.acp.proxy.run_local_proxy", local)
    await run_proxy("default", io.BytesIO(), profiles=profiles, credentials=credentials)
    assert observed == ["invalid-key"]
