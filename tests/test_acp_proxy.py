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
from mimir.acp.proxy import MAX_FRAME_BYTES, FrameWriter, ProxyError, ProxyRouter, _route_stream, run_local_proxy
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


def messages(writer: Writer) -> list[dict[str, Any]]:
    return [json.loads(line) for line in bytes(writer.data).splitlines()]


async def hosted_router(tmp_path: Path) -> tuple[ProxyRouter, Writer, Writer, str, str]:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    await router.route_client({
        "jsonrpc": "2.0",
        "id": "new",
        "method": "session/new",
        "params": {"cwd": str(tmp_path)},
    })
    server_id = messages(daemon)[-1]["params"]["mcpServers"][0]["serverId"]
    await router.route_daemon({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "mcp/connect",
        "params": {"serverId": server_id},
    })
    connection_id = messages(daemon)[-1]["result"]["connectionId"]
    await router.route_daemon({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "mcp/message",
        "params": {
            "connectionId": connection_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "client", "version": "1"},
            },
        },
    })
    await asyncio.sleep(0)
    await router.route_daemon({
        "jsonrpc": "2.0",
        "method": "mcp/message",
        "params": {
            "connectionId": connection_id,
            "method": "notifications/initialized",
            "params": {},
        },
    })
    await router.route_daemon({
        "jsonrpc": "2.0", "id": "new", "result": {"sessionId": "session"}
    })
    return router, client, daemon, server_id, connection_id


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
async def test_complete_frame_over_one_mebibyte_fails_closed() -> None:
    writer = Writer()
    transformer = FrameWriter(writer, "secret")
    with pytest.raises(ProxyError, match="invalid frame"):
        transformer.write(frame({"jsonrpc": "2.0", "method": "x", "params": "x" * MAX_FRAME_BYTES}))
    assert bytes(writer.data) == b""
    reader = asyncio.StreamReader()
    reader.feed_data(frame({"jsonrpc": "2.0", "method": "x", "params": "x" * MAX_FRAME_BYTES}))
    reader.feed_eof()
    dispatched: list[dict[str, Any]] = []

    async def route(message: dict[str, Any], raw: bytes) -> None:
        dispatched.append(message)

    with pytest.raises(ProxyError, match="invalid frame"):
        await _route_stream(reader, route)
    assert dispatched == []


@pytest.mark.asyncio
async def test_new_and_load_inject_exactly_one_provider_when_absent_or_empty(tmp_path: Path) -> None:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    try:
        await router.route_client({
            "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}
        })
        await router.route_client({
            "jsonrpc": "2.0", "id": 2, "method": "session/load",
            "params": {"cwd": str(tmp_path), "sessionId": "old", "mcpServers": []},
        })
        declarations = [item["params"]["mcpServers"] for item in messages(daemon)]
        assert all(len(value) == 1 for value in declarations)
        assert all(value[0]["type"] == "acp" and value[0]["name"] == "mimir-hands" for value in declarations)
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_explicit_hands_declaration_is_preserved_verbatim(tmp_path: Path) -> None:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    declaration = [{"serverId": "foreign", "name": "mimir-hands", "type": "acp", "extra": {"x": 1}}]
    try:
        await router.route_client({
            "jsonrpc": "2.0", "id": 1, "method": "session/new",
            "params": {"cwd": str(tmp_path), "mcpServers": declaration},
        })
        assert messages(daemon)[0]["params"]["mcpServers"] == declaration
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_hosted_server_id_uses_random_required_format(tmp_path: Path) -> None:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    try:
        for request_id in range(8):
            await router.route_client({
                "jsonrpc": "2.0", "id": request_id, "method": "session/new",
                "params": {"cwd": str(tmp_path)},
            })
        server_ids = [item["params"]["mcpServers"][0]["serverId"] for item in messages(daemon)]
        assert len(set(server_ids)) == 8
        assert all(len(value.removeprefix("mimir-hosted:")) == 24 for value in server_ids)
        assert all(value.removeprefix("mimir-hosted:").replace("_", "a").replace("-", "a").isalnum() for value in server_ids)
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_plain_client_without_mcp_capability_can_call_hosted_hands(tmp_path: Path) -> None:
    (tmp_path / "note").write_text("hello")
    router, _, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 3, "method": "mcp/message",
            "params": {"connectionId": connection_id, "method": "tools/call", "params": {
                "name": "read", "arguments": {"path": "note"},
            }},
        })
        await asyncio.sleep(0.05)
        assert messages(daemon)[-1]["result"]["structuredContent"] == {"content": "hello"}
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_router_intercepts_only_ids_minted_by_current_generation(tmp_path: Path) -> None:
    router, client, _, server_id, _ = await hosted_router(tmp_path)
    try:
        foreign = {
            "jsonrpc": "2.0", "id": 8, "method": "mcp/connect",
            "params": {"serverId": server_id + "-lookalike"},
        }
        foreign_raw = json.dumps(foreign, indent=1).encode() + b"\n"
        prior = bytes(client.data)
        await router.route_daemon(foreign, foreign_raw)
        assert bytes(client.data) == prior + foreign_raw
        with pytest.raises(ProxyError, match="invalid frame"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 9, "method": "mcp/connect",
                "params": {"serverId": server_id, "extra": True},
            })
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_session_new_captures_cwd_for_hosted_operations(tmp_path: Path) -> None:
    (tmp_path / "cwd.txt").write_text("captured")
    router, _, daemon, server_id, connection_id = await hosted_router(tmp_path)
    try:
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 9, "method": "mcp/connect",
            "params": {"serverId": server_id},
        })
        assert messages(daemon)[-1]["result"]["connectionId"] != connection_id
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 4, "method": "mcp/message",
            "params": {"connectionId": connection_id, "method": "tools/call", "params": {
                "name": "read", "arguments": {"path": "cwd.txt"},
            }},
        })
        await asyncio.sleep(0.05)
        assert messages(daemon)[-1]["result"]["structuredContent"]["content"] == "captured"
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_duplicate_outstanding_typed_request_id_fails_closed() -> None:
    router = ProxyRouter(Writer(), Writer(), "secret")
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "foreign", "params": {}}
        await router.route_daemon(request)
        with pytest.raises(ProxyError, match="duplicate outstanding"):
            await router.route_daemon(request)
        await router.route_client({"jsonrpc": "2.0", "id": "1", "method": "other", "params": {}})
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_unsolicited_non_tombstoned_response_fails_closed() -> None:
    router = ProxyRouter(Writer(), Writer(), "secret")
    try:
        with pytest.raises(ProxyError, match="unsolicited response"):
            await router.route_daemon({"jsonrpc": "2.0", "id": 1, "result": {}})
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_tombstoned_late_response_is_consumed_without_id_reuse() -> None:
    router = ProxyRouter(Writer(), Writer(), "secret")
    try:
        router._tombstone((int, 7))
        await router.route_client({"jsonrpc": "2.0", "id": 7, "result": {}})
        with pytest.raises(ProxyError, match="duplicate outstanding"):
            await router.route_daemon({"jsonrpc": "2.0", "id": 7, "method": "foreign"})
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_foreign_frames_are_byte_transparent_in_both_directions() -> None:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    client_raw = b'{ "jsonrpc" : "2.0", "id" : 1, "method" : "foreign", "params" : {"x":1} }\n'
    daemon_raw = b'{"result":{"spacing" : true}, "id":1, "jsonrpc":"2.0"}\n'
    reverse_raw = b'{"jsonrpc":"2.0", "id":"same", "method":"other"}\n'
    reverse_result = b'{ "jsonrpc":"2.0", "id":"same", "result":null }\n'
    try:
        await router.route_client(json.loads(client_raw), client_raw)
        await router.route_daemon(json.loads(daemon_raw), daemon_raw)
        await router.route_daemon(json.loads(reverse_raw), reverse_raw)
        await router.route_client(json.loads(reverse_result), reverse_result)
        assert bytes(daemon.data) == client_raw + reverse_result
        assert bytes(client.data) == daemon_raw + reverse_raw
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_session_injection_is_exact_and_explicit_provider_bytes_are_unchanged(tmp_path: Path) -> None:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    explicit_raw = (
        b'{ "jsonrpc":"2.0", "id":2, "method":"session/new", "params":'
        + json.dumps({"cwd": str(tmp_path), "mcpServers": [{"type": "acp", "name": "other", "serverId": "foreign"}]}, separators=(",", ":")).encode()
        + b" }\n"
    )
    try:
        await router.route_client(json.loads(explicit_raw), explicit_raw)
        assert bytes(daemon.data) == explicit_raw
        daemon.data.clear()
        await router.route_client({
            "jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": str(tmp_path)}
        })
        declaration = messages(daemon)[0]["params"]["mcpServers"]
        assert list(declaration[0]) == ["type", "name", "serverId"]
        assert declaration[0]["type"] == "acp"
        assert declaration[0]["name"] == "mimir-hands"
        assert set(messages(daemon)[0]["params"]) == {"cwd", "mcpServers"}
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_load_always_retires_hosted_state_and_failed_provisional_state(tmp_path: Path) -> None:
    router, client, daemon, server_id, connection_id = await hosted_router(tmp_path)
    try:
        await router.route_client({
            "jsonrpc": "2.0", "id": "load", "method": "session/load", "params": {
                "cwd": str(tmp_path), "sessionId": "session",
                "mcpServers": [{"type": "acp", "name": "other", "serverId": "foreign"}],
            },
        })
        with pytest.raises(ProxyError, match="stale hosted connection ID"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 20, "method": "mcp/message",
                "params": {"connectionId": connection_id, "method": "tools/list"},
            })
        with pytest.raises(ProxyError, match="stale hosted server ID"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 21, "method": "mcp/connect",
                "params": {"serverId": server_id},
            })
        await router.route_client({
            "jsonrpc": "2.0", "id": "failed", "method": "session/load",
            "params": {"cwd": str(tmp_path), "sessionId": "session", "mcpServers": []},
        })
        failed_server = messages(daemon)[-1]["params"]["mcpServers"][0]["serverId"]
        await router.route_daemon({
            "jsonrpc": "2.0", "id": "failed", "error": {"code": -32602, "message": "failed"}
        })
        with pytest.raises(ProxyError, match="stale hosted server ID"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 22, "method": "mcp/connect",
                "params": {"serverId": failed_server},
            })
        assert messages(client)[-1]["error"]["message"] == "failed"
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_typed_directional_ids_boolean_rejection_and_hosted_cancellation(tmp_path: Path) -> None:
    router, _, daemon, server_id, connection_id = await hosted_router(tmp_path)
    try:
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 30, "method": "mcp/message", "params": {
                "connectionId": connection_id, "method": "tools/call",
                "params": {"name": "shell", "arguments": {"command": "sleep 30"}},
            },
        })
        await asyncio.sleep(0.05)
        await router.route_daemon({
            "jsonrpc": "2.0", "method": "mcp/message", "params": {
                "connectionId": connection_id, "method": "notifications/cancelled",
                "params": {"requestId": 30},
            },
        })
        await asyncio.sleep(0.05)
        with pytest.raises(ProxyError, match="duplicate outstanding"):
            await router.route_daemon({"jsonrpc": "2.0", "id": 30, "method": "foreign"})
        await router.route_daemon({"jsonrpc": "2.0", "id": "30", "method": "foreign"})
        await router.route_client({"jsonrpc": "2.0", "id": 30, "method": "opposite"})
        with pytest.raises(ProxyError, match="invalid frame"):
            await router.route_daemon({"jsonrpc": "2.0", "id": True, "method": "foreign"})
        with pytest.raises(ProxyError, match="invalid frame"):
            await router.route_daemon({
                "jsonrpc": "2.0", "method": "mcp/message", "params": {
                    "connectionId": connection_id, "method": "notifications/cancelled",
                    "params": {"requestId": False},
                },
            })
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 31, "method": "mcp/disconnect",
            "params": {"connectionId": connection_id},
        })
        with pytest.raises(ProxyError, match="stale hosted connection ID"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 32, "method": "mcp/disconnect",
                "params": {"connectionId": connection_id},
            })
        assert not any(item.get("id") == 30 and "result" in item for item in messages(daemon))
        assert server_id.startswith("mimir-hosted:")
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_hosted_failures_are_supervised_and_generation_state_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    router, _, _, _, connection_id = await hosted_router(tmp_path)
    try:
        async def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("provider failed")

        monkeypatch.setattr(router._provider, "request", fail)
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 40, "method": "mcp/message",
            "params": {"connectionId": connection_id, "method": "tools/list"},
        })
        failure = await asyncio.wait_for(router.wait_failed(), 1)
        assert isinstance(failure, RuntimeError) and str(failure) == "provider failed"
    finally:
        await router.close()

    monkeypatch.setattr("mimir.acp.proxy.MAX_GENERATION_SERVER_IDS", 1)
    bounded = ProxyRouter(Writer(), Writer(), "secret")
    try:
        await bounded.route_client({
            "jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}
        })
        with pytest.raises(ProxyError, match="too many hosted server IDs"):
            await bounded.route_client({
                "jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}
            })
    finally:
        await bounded.close()


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
        assert process.stdin and process.stdout
        process.stdin.close()
        stdout_drain = asyncio.create_task(process.stdout.read())
        try:
            await process.stdin.wait_closed()
            stderr = await process.stderr.read() if process.stderr else b""
            await asyncio.wait_for(process.wait(), 10)
            raw_stdout.extend(await stdout_drain)
        finally:
            if not stdout_drain.done():
                stdout_drain.cancel()
                await asyncio.gather(stdout_drain, return_exceptions=True)
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
    assert secret.encode() not in raw_stdout
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
