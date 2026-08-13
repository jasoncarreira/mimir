from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from mimir.acp.daemon import AcpDaemon
from mimir.acp.profiles import Profile, ProfileStore
from mimir.acp.proxy import MAX_FRAME_BYTES, FrameWriter, ProxyError, run_local_proxy
from mimir.channel_registry import ChannelRegistry
from mimir.identities import IdentityResolver, hash_web_key
from mimir.tools.client_provider import MIMIR_HANDS_V1, PermissionDecision, PermissionEligibility, get_turn_capability_context
from mimir.turn_event_bus import TurnEventBus


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
@pytest.mark.parametrize("failure", ["stdio", "protocol"])
async def test_local_proxy_start_and_protocol_failures_close_connection(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    home = Path(tempfile.mkdtemp(prefix="mp-"))
    socket = home / ".mimir" / "acp" / "daemon.sock"
    socket.parent.mkdir(parents=True, mode=0o700)
    os.chmod(socket.parent, 0o700)
    closed = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        closed.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket))
    if failure == "stdio":
        async def open_failed(output: object) -> object:
            raise OSError("stdio failed")
        monkeypatch.setattr("mimir.acp.proxy.open_stdio", open_failed)
        expected: type[BaseException] = OSError
    else:
        reader = asyncio.StreamReader()
        reader.feed_data(b"malformed\n")
        reader.feed_eof()
        transport = type("Transport", (), {"close": lambda self: None})()
        monkeypatch.setattr("mimir.acp.proxy.open_stdio", lambda output: asyncio.sleep(0, result=(reader, Writer(), transport)))
        expected = ProxyError
    try:
        with pytest.raises(expected):
            await run_local_proxy(Profile("default", home), "secret", io.BytesIO())
        await asyncio.wait_for(closed.wait(), 5)
    finally:
        server.close()
        await server.wait_closed()


class IntegratedCore:
    def __init__(self, bus: TurnEventBus, channels: ChannelRegistry) -> None:
        self.bus = bus
        self.channels = channels
        self.calls = 0
        self.block = False
        self.entered = asyncio.Event()

    async def run_turn(self, event: Any, **kwargs: Any) -> None:
        self.calls += 1
        self.entered.set()
        if self.block:
            await asyncio.Future()
        context = get_turn_capability_context()
        assert context is not None
        turn_id = kwargs["turn_id"]
        arguments = {"path": "note", "oldText": "old", "newText": "new"}
        self.bus.publish({
            "turn_id": turn_id,
            "channel_id": event.channel_id,
            "seq": 1,
            "ts": "now",
            "type": "tool_call",
            "phase": "start",
            "id": "edit-1",
            "tool_name": "hands_edit",
            "args": arguments,
        })
        decision = await context.permission_broker.request_permission(
            PermissionEligibility("edit-1", "hands_edit", "other", arguments)
        )
        assert decision is PermissionDecision.ALLOW_ONCE
        result = await self.channels.send(event.channel_id, "done")
        assert result.sent


def _integrated_bundle(home: Path, secret: str) -> tuple[Any, IntegratedCore]:
    state = home / "state"
    state.mkdir(parents=True)
    (state / "identities.yaml").write_text(yaml.safe_dump({
        "people": [{
            "canonical": "operator",
            "display_name": "Operator",
            "aliases": [hash_web_key(secret)],
            "access": {"roles": ["admin"], "is_service": False},
        }]
    }))
    resolver = IdentityResolver(home)
    resolver.reload()
    bus = TurnEventBus()
    channels = ChannelRegistry()
    core = IntegratedCore(bus, channels)
    return SimpleNamespace(
        core=SimpleNamespace(identity_resolver=resolver),
        config=SimpleNamespace(home=home, acp_journal_ttl_days=7),
        adapters=SimpleNamespace(channels=channels),
        turn_event_bus=bus,
        agent=core,
    ), core


def _thaw(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


async def _proxy_process(tmp_path: Path, home: Path, secret: str) -> tuple[asyncio.subprocess.Process, dict[str, str], Path, list[str]]:
    config = tmp_path / "config"
    ProfileStore(config / "mimir" / "acp" / "profiles.json").set(Profile("default", home))
    key_file = tmp_path / ".native-keyring"
    key_file.write_text(secret)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import keyring\n"
        "class Keyring:\n"
        " __module__='keyring.backends.SecretService'\n"
        " priority=1\n"
        " def get_password(self, service, user): return Path('.native-keyring').read_text()\n"
        "keyring.get_keyring=lambda:Keyring()\n"
    )
    root = Path(__file__).resolve().parents[1]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(fixture), str(root))),
        "XDG_CONFIG_HOME": str(config),
    }
    command = [
        sys.executable,
        "-c",
        "import sys; from mimir.entrypoint import main; sys.argv=['mimir','acp']; raise SystemExit(main())",
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=tmp_path,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr
    return process, environment, key_file, command


async def _send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert process.stdin
    process.stdin.write(frame(message))
    await process.stdin.drain()


async def _receive(process: asyncio.subprocess.Process, stdout: bytearray | None = None) -> dict[str, Any]:
    assert process.stdout
    raw = await asyncio.wait_for(process.stdout.readline(), 5)
    if stdout is not None:
        stdout.extend(raw)
    return json.loads(raw)


async def _request(process: asyncio.subprocess.Process, request_id: int, method: str, params: dict[str, Any], stdout: bytearray | None = None) -> dict[str, Any]:
    await _send(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        message = await _receive(process, stdout)
        if message.get("id") == request_id:
            return message
        await _answer_client_request(process, message)


async def _answer_client_request(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    method = message.get("method")
    if "id" not in message:
        return
    if method == "mcp/connect":
        result: Any = {"connectionId": f"hands-connection-{message['id']}"}
    elif method == "mcp/disconnect":
        result = None
    elif method == "mcp/message" and message["params"]["method"] == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "hands", "version": "1"}}
    elif method == "mcp/message" and message["params"]["method"] == "tools/list":
        result = {"tools": [{
            "name": tool.provider_name,
            "description": tool.description,
            "inputSchema": _thaw(tool.input_schema),
            "outputSchema": _thaw(tool.result_schema),
        } for tool in MIMIR_HANDS_V1.tools]}
    elif method == "session/request_permission":
        result = {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
    else:
        raise AssertionError(message)
    await _send(process, {"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest.mark.asyncio
async def test_real_command_path_flow_and_secret_negative_surfaces(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret = "raw-sentinel-never-persist"
    home = Path(tempfile.mkdtemp(prefix="mp-"))
    bundle, core = _integrated_bundle(home, secret)
    daemon = AcpDaemon(bundle)
    await daemon.start()
    process, environment, key_file, command = await _proxy_process(tmp_path, home, secret)
    raw_stdout = bytearray()
    try:
        initialized = await _request(process, 1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}, raw_stdout)
        assert initialized["result"]["authMethods"][0]["id"] == "mimir-web-key"
        assert (await _request(process, 2, "authenticate", {"methodId": "mimir-web-key", "_meta": {"mimir.fake": "forged"}}, raw_stdout))["result"] == {}
        created = await _request(process, 3, "session/new", {
            "cwd": "/workspace",
            "mcpServers": [{"type": "acp", "name": "mimir-hands", "serverId": "hands"}],
        }, raw_stdout)
        session_id = created["result"]["sessionId"]
        prompted = await _request(process, 4, "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "edit"}],
        }, raw_stdout)
        assert prompted["result"]["stopReason"] == "end_turn"
        loaded = await _request(process, 5, "session/load", {
            "cwd": "/workspace",
            "sessionId": session_id,
            "mcpServers": [],
        }, raw_stdout)
        assert loaded["result"] == {}
        core.block = True
        core.entered.clear()
        await _send(process, {"jsonrpc": "2.0", "id": 6, "method": "session/prompt", "params": {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "cancel"}],
        }})
        await asyncio.wait_for(core.entered.wait(), 5)
        await _send(process, {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
        while True:
            response = await _receive(process, raw_stdout)
            if response.get("id") == 6:
                break
            await _answer_client_request(process, response)
        assert response["result"]["stopReason"] == "cancelled"
        assert daemon._agent is not None and daemon._agent._bundle is bundle
        assert core.calls == 2
    finally:
        assert process.stdin
        process.stdin.close()
        await process.stdin.wait_closed()
        stderr = await process.stderr.read() if process.stderr else b""
        await asyncio.wait_for(process.wait(), 10)
        await daemon.stop()
    assert process.returncode == 0, stderr.decode()
    profile_bytes = (tmp_path / "config" / "mimir" / "acp" / "profiles.json").read_bytes()
    persisted = b"".join(path.read_bytes() for path in home.rglob("*") if path.is_file())
    exposed = b"\n".join([
        profile_bytes,
        persisted,
        stderr,
        bytes(raw_stdout),
        " ".join(environment.values()).encode(),
        " ".join(command).encode(),
        caplog.text.encode(),
        repr(daemon._agent._audit_events if daemon._agent else ()).encode(),
    ])
    assert secret.encode() not in exposed
    assert key_file.read_text() == secret


@pytest.mark.asyncio
async def test_invalid_key_reaches_actual_daemon_rejection_through_credential_path(tmp_path: Path) -> None:
    home = Path(tempfile.mkdtemp(prefix="mp-"))
    bundle, _ = _integrated_bundle(home, "valid-key")
    daemon = AcpDaemon(bundle)
    await daemon.start()
    process, _, _, _ = await _proxy_process(tmp_path, home, "invalid-key")
    try:
        response = await _request(process, 1, "authenticate", {"methodId": "mimir-web-key"})
        assert response["error"]["code"] == -32000
        assert daemon._agent is not None
        assert daemon._agent._auth_context is None
    finally:
        assert process.stdin
        process.stdin.close()
        await process.stdin.wait_closed()
        stderr = await process.stderr.read() if process.stderr else b""
        await asyncio.wait_for(process.wait(), 10)
        await daemon.stop()
    assert b"invalid-key" not in stderr
