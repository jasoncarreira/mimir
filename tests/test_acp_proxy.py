from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from mimir.acp.daemon import AcpDaemon
from mimir.acp.execution_scope import ScopeApproval
from mimir.acp.hosted import HostedMcpError
from mimir.acp.profiles import Profile, ProfileStore
from mimir.acp.proxy import MAX_FRAME_BYTES, FrameWriter, PermissionGrantStore, ProxyError, ProxyRouter, _route_stream, run_local_proxy, run_router
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


@pytest.fixture(params=["confined", "unavailable"])
def lifecycle_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    from mimir.acp import confinement

    if request.param == "unavailable":
        # Force the portable risk path even on the macOS development host.
        def missing() -> object:
            raise confinement.BackendUnavailable("test lifecycle backend unavailable")
        monkeypatch.setattr(confinement, "_backend", missing)
    else:
        try:
            confinement._backend().prepare(("/bin/true",), cwd=Path.cwd().resolve())
        except confinement.BackendUnavailable:
            pytest.skip("real confinement backend unavailable")
    return request.param


async def hosted_router(
    tmp_path: Path, *, approve_unconfined_for_lifecycle: bool = True,
) -> tuple[ProxyRouter, Writer, Writer, str, str]:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    if approve_unconfined_for_lifecycle:
        # Portable subprocess lifecycle tests explicitly model an operator who
        # accepts missing-backend risk. Available backends still must confine.
        # Risk round-trip tests opt out and exercise the real proxy broker.
        async def accept_unavailable_backend_risk(session_id: str) -> bool:
            return True

        router._provider._request_unconfined_permission = accept_unavailable_backend_risk
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


async def call_hosted_python(
    router: ProxyRouter,
    daemon: Writer,
    connection_id: str,
    request_id: str | int,
    code: str,
) -> dict[str, Any]:
    await router.route_daemon({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "mcp/message",
        "params": {
            "connectionId": connection_id,
            "method": "tools/call",
            "params": {"name": "python", "arguments": {"code": code}},
        },
    })
    while True:
        for message in messages(daemon):
            if message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    raise HostedMcpError(error["code"], error["message"], error.get("data"))
                if "result" in message:
                    return message["result"]["structuredContent"]
        await asyncio.sleep(0.01)


async def connect_hosted(
    router: ProxyRouter, daemon: Writer, server_id: str, request_id: int
) -> str:
    await router.route_daemon({
        "jsonrpc": "2.0", "id": request_id, "method": "mcp/connect",
        "params": {"serverId": server_id},
    })
    connection_id = messages(daemon)[-1]["result"]["connectionId"]
    await router.route_daemon({
        "jsonrpc": "2.0", "id": request_id + 1, "method": "mcp/message",
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
        "jsonrpc": "2.0", "method": "mcp/message",
        "params": {
            "connectionId": connection_id,
            "method": "notifications/initialized",
            "params": {},
        },
    })
    return connection_id


async def owned_process_reaped(pid: int) -> bool:
    async with asyncio.timeout(5):
        while True:
            try:
                (Path("/proc") / str(pid) / "stat").read_text()
            except FileNotFoundError:
                return True
            await asyncio.sleep(0.01)


def permission_request(
    request_id: str | int,
    session_id: str,
    wrapper_name: str = "hands_edit",
    *,
    tainted: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"mimir.wrapper": wrapper_name}
    if tainted:
        metadata["mimir.tainted"] = True
    raw_input = {
        "hands_edit": {"path": "note", "old_text": "old", "new_text": "new"},
        "hands_shell": {"command": "true"},
        "hands_python": {"code": "value = 1\nvalue"},
    }[wrapper_name]
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": f"call-{request_id}",
                "title": wrapper_name,
                "kind": "other",
                "status": "pending",
                "rawInput": raw_input,
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow_session", "name": "Allow for this session", "kind": "allow_always"},
                {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
            ],
            "_meta": metadata,
        },
    }


async def active_router(session_id: str = "session") -> tuple[ProxyRouter, Writer, Writer]:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    await router.route_client({
        "jsonrpc": "2.0",
        "id": "new",
        "method": "session/new",
        "params": {
            "cwd": "/workspace",
            "mcpServers": [{"type": "acp", "name": "other", "serverId": "foreign"}],
        },
    })
    await router.route_daemon({
        "jsonrpc": "2.0", "id": "new", "result": {"sessionId": session_id}
    })
    client.data.clear()
    daemon.data.clear()
    return router, client, daemon


async def grant_session(
    router: ProxyRouter,
    request_id: str | int,
    session_id: str,
    wrapper_name: str = "hands_edit",
) -> None:
    await router.route_daemon(permission_request(request_id, session_id, wrapper_name))
    await router.route_client({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
    })


@pytest.mark.asyncio
async def test_allow_session_grant_is_recorded_only_after_wire_and_audit_request() -> None:
    router, client, daemon = await active_router()
    try:
        request = permission_request(7, "session")
        await router.route_daemon(request, frame(request))
        assert bytes(client.data) == frame(request)
        assert len(router._grants) == 0
        response = {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        }
        await router.route_client(response, frame(response))
        assert bytes(daemon.data) == frame(response)
        assert router._grants.allows("session", "hands_edit")
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_grants_are_scoped_to_session_and_wrapper() -> None:
    router, client, daemon = await active_router("one")
    try:
        await grant_session(router, 1, "one")
        await router.route_client({
            "jsonrpc": "2.0", "id": "new-two", "method": "session/new",
            "params": {"cwd": "/workspace", "mcpServers": [{"type": "acp", "name": "other", "serverId": "two"}]},
        })
        await router.route_daemon({
            "jsonrpc": "2.0", "id": "new-two", "result": {"sessionId": "two"}
        })
        client.data.clear()
        daemon.data.clear()
        await router.route_daemon(permission_request(2, "one"))
        assert messages(daemon)[-1]["result"]["outcome"]["optionId"] == "allow_once"
        assert messages(daemon)[-1]["result"]["_meta"] == {
            "mimir.permission_source": "session_grant"
        }
        await router.route_daemon(permission_request(3, "one", "hands_shell"))
        await router.route_daemon(permission_request(4, "two"))
        assert [item["id"] for item in messages(client)] == [3, 4]
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_python_permission_grant_preserves_raw_code_and_scope() -> None:
    router, client, daemon = await active_router()
    try:
        request = permission_request(5, "session", "hands_python")
        await router.route_daemon(request, frame(request))
        assert bytes(client.data) == frame(request)
        assert messages(client)[0]["params"]["toolCall"]["rawInput"] == {
            "code": "value = 1\nvalue"
        }
        await router.route_client({
            "jsonrpc": "2.0", "id": 5,
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        assert router._grants.allows("session", "hands_python")
        client.data.clear()
        daemon.data.clear()
        await router.route_daemon(permission_request(6, "session", "hands_python"))
        assert bytes(client.data) == b""
        assert messages(daemon) == [{
            "jsonrpc": "2.0", "id": 6,
            "result": {
                "outcome": {"outcome": "selected", "optionId": "allow_once"},
                "_meta": {"mimir.permission_source": "session_grant"},
            },
        }]
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_grants_clear_on_load_exit_and_daemon_generation_change() -> None:
    router, client, daemon = await active_router()
    await grant_session(router, 1, "session")
    assert len(router._grants) == 1
    await router.route_client({
        "jsonrpc": "2.0", "id": "load", "method": "session/load",
        "params": {"cwd": "/workspace", "sessionId": "session", "mcpServers": [{"type": "acp", "name": "other", "serverId": "foreign"}]},
    })
    assert len(router._grants) == 0
    await router.route_daemon({"jsonrpc": "2.0", "id": "load", "result": {}})
    await grant_session(router, 2, "session")
    assert len(router._grants) == 1
    await router.close()
    assert len(router._grants) == 0
    replacement, replacement_client, _ = await active_router()
    try:
        await replacement.route_daemon(permission_request(3, "session"))
        assert messages(replacement_client)[-1]["id"] == 3
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_tainted_matching_request_bypasses_grant_and_reaches_human() -> None:
    router, client, _ = await active_router()
    try:
        await grant_session(router, 1, "session")
        client.data.clear()
        request = permission_request(2, "session", tainted=True)
        await router.route_daemon(request, frame(request))
        assert bytes(client.data) == frame(request)
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [9, "nine"])
async def test_untainted_matching_request_is_answered_allow_once_upstream(
    request_id: str | int,
) -> None:
    router, client, daemon = await active_router()
    try:
        await grant_session(router, 1, "session")
        client.data.clear()
        daemon.data.clear()
        await router.route_daemon(permission_request(request_id, "session"))
        assert bytes(client.data) == b""
        assert messages(daemon) == [{
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "outcome": {"outcome": "selected", "optionId": "allow_once"},
                "_meta": {"mimir.permission_source": "session_grant"},
            },
        }]
        assert type(messages(daemon)[0]["id"]) is type(request_id)
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_session_grant_outcome_visibility() -> None:
    from mimir.acp.sdk import PermissionCompletion

    router, client, daemon = await active_router()
    try:
        await grant_session(router, 1, "session")
        client.data.clear()
        daemon.data.clear()
        await router.route_daemon(permission_request(2, "session"))
        assert messages(client) == []
        response, = messages(daemon)
        assert response["id"] == 2
        assert response["result"]["_meta"] == {
            "mimir.permission_source": "session_grant"
        }
        completion = PermissionCompletion.from_response(response["result"])
        assert completion.session_grant is True
        assert completion.decision == "allow_once"
        assert completion.executable is True
        assert completion.error is None
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_malformed_reserved_permission_metadata_fails_closed() -> None:
    router, client, daemon = await active_router()
    try:
        invalid = []
        false_taint = permission_request(1, "session")
        false_taint["params"]["_meta"]["mimir.tainted"] = False
        invalid.append(false_taint)
        unknown_wrapper = permission_request(2, "session")
        unknown_wrapper["params"]["_meta"]["mimir.wrapper"] = "hands_unknown"
        invalid.append(unknown_wrapper)
        wrong_options = permission_request(3, "session")
        wrong_options["params"]["options"] = list(reversed(wrong_options["params"]["options"]))
        invalid.append(wrong_options)
        wrong_payload = permission_request(4, "session")
        wrong_payload["params"]["toolCall"]["rawInput"]["extra"] = True
        invalid.append(wrong_payload)
        missing_wrapper = permission_request(5, "session")
        del missing_wrapper["params"]["_meta"]["mimir.wrapper"]
        missing_wrapper["params"]["_meta"]["mimir.tainted"] = True
        invalid.append(missing_wrapper)
        for request_id, metadata in enumerate((None, {}, {"other": True}, []), 6):
            missing_metadata = permission_request(request_id, "session")
            missing_metadata["params"]["_meta"] = metadata
            invalid.append(missing_metadata)
        absent_metadata = permission_request(10, "session")
        del absent_metadata["params"]["_meta"]
        invalid.append(absent_metadata)
        camel_payload = permission_request(11, "session")
        camel_payload["params"]["toolCall"]["rawInput"] = {
            "path": "note", "oldText": "old", "newText": "new",
        }
        invalid.append(camel_payload)
        wrong_identity = permission_request(12, "session")
        wrong_identity["params"]["toolCall"]["title"] = "hands_shell"
        invalid.append(wrong_identity)
        extra_envelope = permission_request(13, "session")
        extra_envelope["extra"] = True
        invalid.append(extra_envelope)
        extra_params = permission_request(14, "session")
        extra_params["params"]["extra"] = True
        invalid.append(extra_params)
        extra_tool_call = permission_request(15, "session")
        extra_tool_call["params"]["toolCall"]["extra"] = True
        invalid.append(extra_tool_call)
        for request in invalid:
            with pytest.raises(ProxyError, match="invalid reserved permission"):
                await router.route_daemon(request)
        unknown_reserved = permission_request(16, "session")
        unknown_reserved["params"]["_meta"]["mimir.unknown"] = True
        with pytest.raises(ProxyError, match="invalid reserved permission"):
            await router.route_daemon(unknown_reserved)
        notification = permission_request(17, "session")
        del notification["id"]
        with pytest.raises(ProxyError, match="invalid reserved permission"):
            await router.route_daemon(notification)
        assert bytes(client.data) == bytes(daemon.data) == b""
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_stale_session_or_generation_reserved_traffic_fails_closed() -> None:
    router, client, _ = await active_router()
    with pytest.raises(ProxyError, match="stale reserved"):
        await router.route_daemon(permission_request(1, "retired"))
    assert bytes(client.data) == b""
    await router.close()
    with pytest.raises(ProxyError, match="generation is closed"):
        await router.route_daemon(permission_request(2, "session"))


@pytest.mark.asyncio
async def test_non_hands_permission_frames_remain_transparent() -> None:
    router, client, daemon = await active_router()
    request_raw = b'{ "jsonrpc":"2.0", "id":8, "method":"session/request_permission", "params":{"sessionId":"foreign","toolCall":{}} }\n'
    response_raw = b'{"jsonrpc":"2.0", "id":8, "result":{"outcome":{"outcome":"selected","optionId":"allow_session"}} }\n'
    try:
        await router.route_daemon(json.loads(request_raw), request_raw)
        await router.route_client(json.loads(response_raw), response_raw)
        assert bytes(client.data) == request_raw
        assert bytes(daemon.data) == response_raw
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_malformed_correlated_permission_responses_fail_closed() -> None:
    router, client, daemon = await active_router()
    invalid_results = [
        {"outcome": {"outcome": "selected", "optionId": "allow_session"}, "extra": True},
        {"outcome": {"outcome": "selected", "optionId": "allow_session", "extra": True}},
        {"outcome": {"outcome": "selected", "optionId": "allow_always"}},
        {"outcome": {"outcome": "cancelled", "_meta": {}}},
        {"outcome": {"outcome": "selected", "optionId": "allow_session"}, "_meta": []},
        {},
    ]
    try:
        for request_id, result in enumerate(invalid_results, 20):
            await router.route_daemon(permission_request(request_id, "session"))
            before = bytes(daemon.data)
            with pytest.raises(ProxyError, match="invalid reserved permission response"):
                await router.route_client({
                    "jsonrpc": "2.0", "id": request_id, "result": result,
                })
            assert bytes(daemon.data) == before
        await router.route_daemon(permission_request(30, "session"))
        before = bytes(daemon.data)
        with pytest.raises(ProxyError, match="invalid reserved permission response"):
            await router.route_client({
                "jsonrpc": "2.0", "id": 30,
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
                "extra": True,
            })
        assert bytes(daemon.data) == before
        await router.route_daemon(permission_request(31, "session"))
        router._active_sessions.clear()
        with pytest.raises(ProxyError, match="stale reserved permission response"):
            await router.route_client({
                "jsonrpc": "2.0", "id": 31,
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
            })
        with pytest.raises(ProxyError, match="unsolicited response"):
            await router.route_client({
                "jsonrpc": "2.0", "id": "uncorrelated",
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
            })
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_hosted_disconnect_revokes_session_grants(tmp_path: Path) -> None:
    router, _, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        await grant_session(router, 80, "session")
        assert router._grants.allows("session", "hands_edit")
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 81, "method": "mcp/disconnect",
            "params": {"connectionId": connection_id},
        })
        assert len(router._grants) == 0
        assert messages(daemon)[-1]["id"] == 81
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_explicit_hands_disconnect_revokes_session_grants_transparently() -> None:
    client = Writer()
    daemon = Writer()
    router = ProxyRouter(client, daemon, "secret")
    session_request = (
        b'{ "jsonrpc":"2.0", "id":"new", "method":"session/new", "params":'
        b'{"cwd":"/workspace","mcpServers":[{"serverId":"explicit-hands",'
        b'"name":"mimir-hands","type":"acp","extra":{"kept":true}}]} }\n'
    )
    connect = (
        b'{ "jsonrpc":"2.0", "id":70, "method":"mcp/connect", '
        b'"params":{"serverId":"explicit-hands"} }\n'
    )
    connected = (
        b'{ "jsonrpc":"2.0", "id":70, '
        b'"result":{"connectionId":"explicit-connection"} }\n'
    )
    disconnect = (
        b'{ "jsonrpc":"2.0", "id":72, "method":"mcp/disconnect", '
        b'"params":{"connectionId":"explicit-connection"} }\n'
    )
    try:
        await router.route_client(json.loads(session_request), session_request)
        assert bytes(daemon.data) == session_request
        client.data.clear()
        daemon.data.clear()
        await router.route_daemon(json.loads(connect), connect)
        assert bytes(client.data) == connect
        await router.route_client(json.loads(connected), connected)
        assert bytes(daemon.data) == connected
        assert "explicit-connection" not in router._explicit_connection_sessions
        await router.route_daemon({
            "jsonrpc": "2.0", "id": "new", "result": {"sessionId": "session"}
        })
        assert router._explicit_connection_sessions["explicit-connection"] == "session"
        await grant_session(router, 71, "session")
        assert router._grants.allows("session", "hands_edit")
        client.data.clear()
        await router.route_daemon(json.loads(disconnect), disconnect)
        assert bytes(client.data) == disconnect
        assert len(router._grants) == 0
        client.data.clear()
        await router.route_daemon(permission_request(73, "session"))
        assert messages(client)[-1]["id"] == 73
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_framing_and_generation_failures_revoke_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _, _ = await active_router()
    await grant_session(router, 90, "session")
    router._fail_generation(ConnectionError("daemon failed"))
    assert len(router._grants) == 0
    await router.close()

    framing_router, _, _ = await active_router()
    await grant_session(framing_router, 91, "session")
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(b"malformed\n")
    client_reader.feed_eof()
    daemon_reader = asyncio.StreamReader()
    monkeypatch.setattr(
        "mimir.acp.proxy.ProxyRouter",
        lambda client_writer, daemon_writer, credential, timeout_seconds=60: framing_router,
    )
    with pytest.raises(ProxyError, match="invalid frame"):
        await run_router(
            client_reader, Writer(), daemon_reader, Writer(), "secret",
            close_on_daemon_exit=True,
        )
    assert len(framing_router._grants) == 0


def test_permission_grant_store_keys_exact_session_and_wrapper() -> None:
    store = PermissionGrantStore()
    store.add("session", "hands_edit")
    assert store.allows("session", "hands_edit")
    assert not store.allows("other", "hands_edit")
    assert not store.allows("session", "hands_shell")


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
        assert all(value.startswith("mimir-hosted:") for value in server_ids)
        assert all(len(value) == len("mimir-hosted:") + 24 for value in server_ids)
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
async def test_session_new_first_python_call_has_fresh_empty_namespace(
    tmp_path: Path, lifecycle_backend: str,
) -> None:
    from mimir.acp.execution_scope import UNCONFINED_WARNING

    router, _, daemon, _, connection_id = await hosted_router(
        tmp_path, approve_unconfined_for_lifecycle=False,
    )
    acceptance = AsyncMock(return_value=True)
    router._provider._request_unconfined_permission = acceptance
    try:
        async with asyncio.timeout(120):
            result = await call_hosted_python(
                router,
                daemon,
                connection_id,
                "first-python",
                "globals().get('value_created_before_session_new')",
            )
            assert result == {
                "ok": True,
                "stdout": "",
                "stderr": UNCONFINED_WARNING + "\n" if lifecycle_backend == "unavailable" else "",
                "value": "None",
                "exception": "",
                "timedOut": False,
                "kernel": "fresh",
            }
            assert acceptance.await_count == (1 if lifecycle_backend == "unavailable" else 0)
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
        later_connection_id = messages(daemon)[-1]["result"]["connectionId"]
        assert later_connection_id != connection_id
        first_session = router._provider._connections[connection_id].session
        later_session = router._provider._connections[later_connection_id].session
        assert first_session is later_session
        assert first_session.cwd == tmp_path
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
                "cwd": str(tmp_path), "sessionId": "session", "mcpServers": [],
            },
        })
        replacement_server_id = messages(daemon)[-1]["params"]["mcpServers"][0]["serverId"]
        replacement_connection_id = await connect_hosted(
            router, daemon, replacement_server_id, 20
        )
        assert replacement_connection_id != connection_id
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 22, "method": "mcp/disconnect",
            "params": {"connectionId": connection_id},
        })
        assert messages(daemon)[-1] == {"jsonrpc": "2.0", "id": 22, "result": {}}
        assert replacement_connection_id in router._connection_sessions
        with pytest.raises(ProxyError, match="stale hosted connection ID"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 23, "method": "mcp/message",
                "params": {"connectionId": connection_id, "method": "tools/list"},
            })
        assert replacement_connection_id in router._connection_sessions
        with pytest.raises(ProxyError, match="stale hosted server ID"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 24, "method": "mcp/connect",
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
                "jsonrpc": "2.0", "id": 25, "method": "mcp/connect",
                "params": {"serverId": failed_server},
            })
        assert messages(client)[-1]["error"]["message"] == "failed"
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_load_releases_kernel_and_next_python_reuses_namespace(
    tmp_path: Path,
) -> None:
    router, _, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        async with asyncio.timeout(120):
            first = await call_hosted_python(router, daemon, connection_id, 100, "value = 41")
            assert first["kernel"] == "fresh"
            worker = next(iter(router._provider._python_kernels._processes))
            await router.route_client({
                "jsonrpc": "2.0", "id": "load-python", "method": "session/load",
                "params": {"cwd": str(tmp_path), "sessionId": "session", "mcpServers": []},
            })
            assert worker.returncode is None
            load_request = messages(daemon)[-1]
            server_id = load_request["params"]["mcpServers"][0]["serverId"]
            await router.route_daemon({
                "jsonrpc": "2.0", "id": "load-python", "result": {}
            })
            replacement = await connect_hosted(router, daemon, server_id, 101)
            result = await call_hosted_python(
                router, daemon, replacement, 103, "globals().get('value')"
            )
            assert result["kernel"] == "reused"
            assert result["value"] == "41"
            assert list(router._provider._python_kernels._processes) == [worker]
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_disconnect_and_cancel_kill_active_execution_but_release_idle_kernel(
    tmp_path: Path, lifecycle_backend: str,
) -> None:
    from mimir.acp.execution_scope import UNCONFINED_WARNING

    router, _, daemon, server_id, connection_id = await hosted_router(
        tmp_path, approve_unconfined_for_lifecycle=False,
    )
    acceptance = AsyncMock(return_value=True)
    router._provider._request_unconfined_permission = acceptance
    active_session_id = "session"
    try:
        async with asyncio.timeout(120):
            initial = await call_hosted_python(router, daemon, connection_id, 110, "value = 1")
            hosted_session = next(iter(router._provider._sessions.values()))
            if lifecycle_backend == "unavailable":
                assert acceptance.await_count == 1
                assert hosted_session.scope.unconfined_approved is True
                assert initial["stderr"] == UNCONFINED_WARNING + "\n"
            else:
                acceptance.assert_not_awaited()
                assert initial["stderr"] == ""
            second_connection = await connect_hosted(router, daemon, server_id, 111)
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 113, "method": "mcp/message",
                "params": {
                    "connectionId": second_connection,
                    "method": "tools/call",
                    "params": {
                        "name": "python",
                        "arguments": {
                            "code": "import pathlib,time\npathlib.Path('disconnect-entered').write_text('yes')\ntime.sleep(30)"
                        },
                    },
                },
            })
            async with asyncio.timeout(5):
                while not (tmp_path / "disconnect-entered").exists():
                    await asyncio.sleep(0.01)
            worker = next(iter(router._provider._python_kernels._processes))
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 119, "method": "mcp/message",
                "params": {
                    "connectionId": connection_id,
                    "method": "tools/call",
                    "params": {
                        "name": "python",
                        "arguments": {
                            "code": "import pathlib\npathlib.Path('queued-ran').write_text('bad')"
                        },
                    },
                },
            })
            await asyncio.sleep(0)
            await asyncio.wait_for(router.route_daemon({
                "jsonrpc": "2.0", "id": 120, "method": "mcp/disconnect",
                "params": {"connectionId": connection_id},
            }), 2)
            assert router._provider._python_kernels._processes == {}
            assert await owned_process_reaped(worker.pid)
            assert not any(
                item.get("id") in {113, 119} and ("result" in item or "error" in item)
                for item in messages(daemon)
            )
            assert not (tmp_path / "queued-ran").exists()
            if lifecycle_backend == "unavailable":
                assert hosted_session.scope.unconfined_approved is False
                assert hosted_session.scope.risk_requested is True
                with pytest.raises(HostedMcpError, match="final and no active risk grant remains"):
                    await call_hosted_python(
                        router, daemon, second_connection, 114, "globals().get('value')"
                    )
                # This was an accepted grant that disconnect revoked, not a user
                # rejection. Reconnecting cannot open a second risk prompt.
                assert acceptance.await_count == 1
                assert router._provider._python_kernels._processes == {}
                await router.route_client({
                    "jsonrpc": "2.0", "id": "new-after-disconnect", "method": "session/new",
                    "params": {"cwd": str(tmp_path)},
                })
                new_server = messages(daemon)[-1]["params"]["mcpServers"][0]["serverId"]
                second_connection = await connect_hosted(router, daemon, new_server, 121)
                active_session_id = "replacement-session"
                await router.route_daemon({
                    "jsonrpc": "2.0", "id": "new-after-disconnect",
                    "result": {"sessionId": active_session_id},
                })
                assert acceptance.await_count == 1
                fresh_after_disconnect = await call_hosted_python(
                    router, daemon, second_connection, 123, "globals().get('value')"
                )
                assert acceptance.await_count == 2
                assert fresh_after_disconnect["stderr"] == UNCONFINED_WARNING + "\n"
            else:
                fresh_after_disconnect = await call_hosted_python(
                    router, daemon, second_connection, 114, "globals().get('value')"
                )
                acceptance.assert_not_awaited()
                assert fresh_after_disconnect["stderr"] == ""
            assert fresh_after_disconnect["kernel"] == "fresh"
            assert fresh_after_disconnect["value"] == "None"

            await router.route_daemon({
                "jsonrpc": "2.0", "id": 115, "method": "mcp/message",
                "params": {
                    "connectionId": second_connection,
                    "method": "tools/call",
                    "params": {
                        "name": "python",
                        "arguments": {
                            "code": "import pathlib,time\npathlib.Path('cancel-entered').write_text('yes')\ntime.sleep(30)"
                        },
                    },
                },
            })
            async with asyncio.timeout(5):
                while not (tmp_path / "cancel-entered").exists():
                    await asyncio.sleep(0.01)
            await router.route_daemon({
                "jsonrpc": "2.0", "method": "mcp/message",
                "params": {
                    "connectionId": second_connection,
                    "method": "notifications/cancelled",
                    "params": {"requestId": 115},
                },
            })
            async with asyncio.timeout(5):
                while router._provider._python_kernels._processes:
                    await asyncio.sleep(0.01)
            fresh = await call_hosted_python(router, daemon, second_connection, 116, "value = 8")
            assert fresh["kernel"] == "fresh"
            idle_worker = next(iter(router._provider._python_kernels._processes))
            await router.route_client({
                "jsonrpc": "2.0", "method": "session/cancel",
                "params": {"sessionId": active_session_id},
            })
            assert idle_worker.returncode is None
            reused = await call_hosted_python(router, daemon, second_connection, 117, "value")
            assert reused["kernel"] == "reused"
            assert reused["value"] == "8"
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shutdown_signal",
    [None, signal.SIGTERM, signal.SIGINT, signal.SIGHUP],
    ids=["atexit", "sigterm", "sigint", "sighup"],
)
async def test_owned_child_cleans_on_atexit_and_each_supported_signal(
    shutdown_signal: signal.Signals | None,
) -> None:
    source = """
import asyncio, json, os, sys
from mimir.acp.proxy import ProxyRouter, _ShutdownHooks
class Writer:
 def write(self, data): pass
 async def drain(self): pass
async def setup():
 router=ProxyRouter(Writer(),Writer(),'secret')
 # Explicit operator consent for this portable process-lifecycle fixture.
 async def accept_unavailable_backend_risk(session_id): return True
 router._provider._request_unconfined_permission=accept_unavailable_backend_risk
 router._provider.bind_session('session',os.getcwd())
 await router._provider.execute_python(router._provider._sessions['session'],'value=1')
 worker=next(iter(router._provider._python_kernels._processes))
 hooks=_ShutdownHooks(router);hooks.install()
 session=router._provider._sessions['session']
 shell=asyncio.create_task(router._provider._shell(session,'sleep 30'))
 while not router._provider._processes: await asyncio.sleep(0)
 shell_process=next(iter(router._provider._processes))
 pids=[worker.pid,shell_process.pid]
 print(json.dumps({'pids':pids,'pgids':[os.getpgid(pid) for pid in pids],
                   'unconfined':session.scope.unconfined_approved}),flush=True)
loop=asyncio.new_event_loop();asyncio.set_event_loop(loop)
loop.run_until_complete(setup())
if sys.argv[1]=='wait': loop.run_forever()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        source,
        "exit" if shutdown_signal is None else "wait",
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # One hang guard covers readiness, signal delivery, exit and reaping;
        # the ownership marker, not a per-read deadline, orders the signal.
        async with asyncio.timeout(120):
            assert process.stdout is not None
            ownership = json.loads(await process.stdout.readline())
            assert ownership["pgids"] == ownership["pids"]
            if shutdown_signal is not None:
                process.send_signal(shutdown_signal)
            stdout, stderr = await process.communicate()
            assert process.returncode == (128 + shutdown_signal if shutdown_signal else 0), (
                f"owned-child exit mismatch: stdout={stdout!r}, stderr={stderr!r}"
            )
            assert stdout == b""
            if shutdown_signal is not None:
                if ownership["unconfined"]:
                    # The accepted-risk audit is required; no shutdown errors or
                    # arbitrary stderr are hidden by the portable consent fixture.
                    assert json.loads(stderr) == {
                        "type": "acp_permission_outcome",
                        "wrapper_name": "hands_unconfined_execution",
                        "path": "<unconfined>", "outcome": "approved",
                        "resource_resolvable": False,
                    }
                else:
                    assert stderr == b""
            assert all(
                await asyncio.gather(
                    *(owned_process_reaped(pid) for pid in ownership["pids"])
                )
            )
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


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
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 32, "method": "mcp/disconnect",
            "params": {"connectionId": connection_id},
        })
        assert messages(daemon)[-1] == {"jsonrpc": "2.0", "id": 32, "result": {}}
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
async def test_client_session_cancel_tombstones_hosted_request(tmp_path: Path) -> None:
    router, _, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 50, "method": "mcp/message", "params": {
                "connectionId": connection_id, "method": "tools/call",
                "params": {"name": "shell", "arguments": {"command": "sleep 30"}},
            },
        })
        await asyncio.sleep(0.05)
        cancel_raw = b'{ "jsonrpc":"2.0", "method":"session/cancel", "params":{"sessionId":"session"} }\n'
        before = bytes(daemon.data)
        await router.route_client(json.loads(cancel_raw), cancel_raw)
        assert bytes(daemon.data).startswith(before + cancel_raw)
        await asyncio.sleep(0.05)
        assert not router._generation_failed, repr(router._failure.result())
        with pytest.raises(ProxyError, match="duplicate outstanding"):
            await router.route_daemon({"jsonrpc": "2.0", "id": 50, "method": "foreign"})
        assert not any(item.get("id") == 50 and ("result" in item or "error" in item) for item in messages(daemon))
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_hosted_error_response_writer_failure_fails_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    router, _, _, _, connection_id = await hosted_router(tmp_path)

    class FailedWriter(Writer):
        def write(self, data: bytes) -> None:
            raise BrokenPipeError("response failed")

    async def hosted_error(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise HostedMcpError(-32000, "hosted failed")

    try:
        router._daemon._writer = FailedWriter()
        monkeypatch.setattr(router._provider, "request", hosted_error)
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 60, "method": "mcp/message",
            "params": {"connectionId": connection_id, "method": "tools/list"},
        })
        failure = await asyncio.wait_for(router.wait_failed(), 1)
        assert isinstance(failure, BrokenPipeError)
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_grace_period_stream_failure_is_propagated() -> None:
    client_reader = asyncio.StreamReader()
    client_reader.feed_eof()

    class FailedReader:
        async def read(self, size: int) -> bytes:
            await asyncio.sleep(0.01)
            raise ConnectionError("grace read failed")

    with pytest.raises(ConnectionError, match="grace read failed"):
        await run_router(client_reader, Writer(), FailedReader(), Writer(), "secret")


@pytest.mark.asyncio
async def test_live_and_cumulative_hosted_connection_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    router, _, daemon, server_id, connection_id = await hosted_router(tmp_path)
    try:
        monkeypatch.setattr("mimir.acp.proxy.MAX_LIVE_CONNECTIONS", 1)
        with pytest.raises(ProxyError, match="too many hosted connections"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 70, "method": "mcp/connect",
                "params": {"serverId": server_id},
            })
        await router.route_daemon({
            "jsonrpc": "2.0", "id": 71, "method": "mcp/disconnect",
            "params": {"connectionId": connection_id},
        })
        monkeypatch.setattr("mimir.acp.proxy.MAX_LIVE_CONNECTIONS", 10)
        monkeypatch.setattr("mimir.acp.proxy.MAX_GENERATION_CONNECTION_IDS", 1)
        with pytest.raises(ProxyError, match="too many hosted connections"):
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 72, "method": "mcp/connect",
                "params": {"serverId": server_id},
            })
        assert messages(daemon)[-1]["id"] == 71
    finally:
        await router.close()


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


@pytest.mark.asyncio
async def test_local_proxy_propagates_only_selected_profile_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    profile = Profile("default", tmp_path, timeout_seconds=17)
    upstream_reader = asyncio.StreamReader()
    upstream_writer = Writer()
    input_reader = asyncio.StreamReader()
    output_writer = Writer()
    transport = type("Transport", (), {"close": lambda self: None})()
    observed: list[int] = []

    async def route(
        client_reader: object,
        client_writer: object,
        daemon_reader: object,
        daemon_writer: object,
        credential: str,
        *,
        timeout_seconds: int,
    ) -> None:
        router = ProxyRouter(client_writer, daemon_writer, credential, timeout_seconds)
        await router.route_client({
            "jsonrpc": "2.0", "id": "new", "method": "session/new",
            "params": {
                "cwd": str(tmp_path),
                "timeoutSeconds": 1,
            },
        })
        hosted_session = next(iter(router._provider._sessions.values()))
        observed.append(hosted_session.timeout_seconds)
        await router.close()

    monkeypatch.setattr("mimir.acp.proxy.socket_path", lambda selected: tmp_path / "socket")
    monkeypatch.setattr(
        "mimir.acp.proxy.asyncio.open_unix_connection",
        lambda path: asyncio.sleep(0, result=(upstream_reader, upstream_writer)),
    )
    monkeypatch.setattr(
        "mimir.acp.proxy.open_stdio",
        lambda output: asyncio.sleep(0, result=(input_reader, output_writer, transport)),
    )
    monkeypatch.setattr("mimir.acp.proxy.run_router", route)
    monkeypatch.setenv("MIMIR_ACP_TIMEOUT", "1")

    await run_local_proxy(profile, "secret", io.BytesIO())
    assert observed == [17]


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
        arguments = {"path": "note", "old_text": "old", "new_text": "new"}
        self.bus.publish({
            "turn_id": turn_id,
            "channel_id": event.channel_id,
            "seq": 1,
            "ts": "now",
            "type": "tool_call",
            "phase": "start",
            "id": "edit-1",
            "tool_name": "legacy_edit",
            "args": arguments,
        })
        decision = await context.permission_broker.request_permission(
            PermissionEligibility("edit-1", "legacy_edit", "other", arguments)
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
    raw = await process.stdout.readline()
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
        # Responses and turn entry order the protocol; only the whole exchange
        # through EOF and process exit has a harness deadline.
        async with asyncio.timeout(120):
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
            await core.entered.wait()
            await _send(process, {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
            while True:
                response = await _receive(process, raw_stdout)
                if response.get("id") == 6:
                    break
                await _answer_client_request(process, response)
            assert response["result"]["stopReason"] == "cancelled"
            assert daemon._agent is not None and daemon._agent._bundle is bundle
            assert core.calls == 2
            assert process.stdin
            process.stdin.close()
            await process.stdin.wait_closed()
            stdout, stderr = await process.communicate()
            raw_stdout.extend(stdout)
            assert process.returncode == 0, stderr.decode()
    finally:
        try:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        finally:
            await daemon.stop()
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
        # Observe rejection before EOF, then require a clean protocol exit
        # under the same hang guard rather than timing individual pipe reads.
        async with asyncio.timeout(120):
            response = await _request(process, 1, "authenticate", {"methodId": "mimir-web-key"})
            assert response["error"]["code"] == -32000
            assert daemon._agent is not None
            assert daemon._agent._auth_context is None
            assert process.stdin
            process.stdin.close()
            await process.stdin.wait_closed()
            stdout, stderr = await process.communicate()
            assert process.returncode == 0, stderr.decode()
            assert stdout == b""
            assert b"invalid-key" not in stderr
    finally:
        try:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        finally:
            await daemon.stop()


async def start_scope_permission(
    router: ProxyRouter, connection_id: str, request_id: int, path: str,
    monkeypatch: pytest.MonkeyPatch,
    *, unconfined: bool = False, recursive: bool = False,
) -> asyncio.Task[Any]:
    """Exercise the real hosted request owner without depending on a sandbox."""
    provider_id = router._connection_provider_sessions[connection_id]

    async def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        approved = (
            await router._request_unconfined_permission(provider_id)
            if unconfined else await router._request_scope_permission(
                provider_id, ScopeApproval(path=Path(path), recursive=recursive),
            )
        )
        return {"approved": approved}

    monkeypatch.setattr(router._provider, "request", request)
    await router.route_daemon({
        "jsonrpc": "2.0", "id": request_id, "method": "mcp/message",
        "params": {
            "connectionId": connection_id, "method": "tools/call",
            "params": {"name": "request_scope", "arguments": {"path": path}},
        },
    })
    task = router._local_requests[(int, request_id)]
    assert task is not None
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("recursive", [False, True], ids=["file", "directory"])
async def test_scope_permission_is_local_path_only_and_separate_from_wrapper_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recursive: bool,
) -> None:
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        await grant_session(router, 10, "session", "hands_shell")
        client.data.clear()
        daemon.data.clear()
        path = str(tmp_path.parent / "outside")
        task = await start_scope_permission(
            router, connection_id, 11, path, monkeypatch, recursive=recursive,
        )
        request, = messages(client)
        assert request["method"] == "session/request_permission"
        params = request["params"]
        assert params["sessionId"] == "session"
        assert params["toolCall"]["rawInput"] == {"path": path}
        assert "restarts the Python kernel" in params["toolCall"]["title"]
        assert "loses all REPL state" in params["toolCall"]["title"]
        scope_description = "this directory and everything beneath it" if recursive else "this file alone"
        assert params["toolCall"]["title"] == (
            f"Allow read/write access to {scope_description} for this session? "
            "Approval restarts the Python kernel and loses all REPL state."
        )
        assert params["options"][0]["name"] == "Allow this scope for this session"
        assert params["_meta"] == {"mimir.execution_scope": True}
        assert [item["optionId"] for item in params["options"]] == [
            "allow_session", "reject_once",
        ]
        assert messages(daemon) == []
        answer = {
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        }
        await router.route_client(answer)
        await task
        assert messages(daemon) == [{"jsonrpc": "2.0", "id": 11, "result": {"approved": True}}]
        assert len(router._grants) == 1
        assert router._grants.allows("session", "hands_shell")
        assert not router._grants.allows("session", "hands_python")
        # A duplicate local answer is consumed, never sent to the daemon.
        await router.route_client(answer)
        assert len(messages(daemon)) == 1
        assert not router._execution_permissions
        client.data.clear()
        # Even after a scope approval, a tainted wrapper still reaches the editor.
        await router.route_daemon(permission_request(18, "session", "hands_shell", tainted=True))
        assert messages(client)[-1]["params"]["_meta"] == {
            "mimir.wrapper": "hands_shell", "mimir.tainted": True,
        }
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
    {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
    {"outcome": {"outcome": "selected", "optionId": "unknown"}},
    {"outcome": {"outcome": "cancelled"}},
    {"outcome": {"outcome": "selected", "optionId": "allow_session", "extra": True}},
    {"outcome": "allow_session"},
])
@pytest.mark.parametrize("unconfined", [False, True])
async def test_scope_permission_rejects_unoffered_cancelled_and_malformed_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: dict[str, Any],
    unconfined: bool,
) -> None:
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        client.data.clear()
        daemon.data.clear()
        task = await start_scope_permission(router, connection_id, 12, "/outside", monkeypatch, unconfined=unconfined)
        request, = messages(client)
        await router.route_client({"jsonrpc": "2.0", "id": request["id"], "result": result})
        await task
        assert messages(daemon) == [{"jsonrpc": "2.0", "id": 12, "result": {"approved": False}}]
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unconfined", [False, True])
async def test_scope_permission_timeout_and_late_answer_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    unconfined: bool,
) -> None:
    monkeypatch.setattr("mimir.acp.proxy.SCOPE_PERMISSION_TIMEOUT_SECONDS", 0.01)
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        client.data.clear()
        daemon.data.clear()
        task = await start_scope_permission(router, connection_id, 13, "/outside", monkeypatch, unconfined=unconfined)
        request, = messages(client)
        await asyncio.wait_for(task, 1)
        assert messages(daemon)[-1]["result"] == {"approved": False}
        await router.route_client({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        assert len(messages(daemon)) == 1
        assert not router._execution_permissions
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "reload", "disconnect", "generation"])
@pytest.mark.parametrize("unconfined", [False, True])
async def test_scope_permission_cannot_outlive_its_hosted_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
    unconfined: bool,
) -> None:
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        client.data.clear()
        daemon.data.clear()
        task = await start_scope_permission(router, connection_id, 14, "/outside", monkeypatch, unconfined=unconfined)
        request, = messages(client)
        if action == "cancel":
            await router.route_client({
                "jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "session"},
            })
        elif action == "reload":
            await router.route_client({
                "jsonrpc": "2.0", "id": "reload", "method": "session/load",
                "params": {"cwd": str(tmp_path), "sessionId": "session"},
            })
        elif action == "disconnect":
            await router.route_daemon({
                "jsonrpc": "2.0", "id": 15, "method": "mcp/disconnect",
                "params": {"connectionId": connection_id},
            })
        else:
            router._fail_generation(RuntimeError("generation failed"))
        await asyncio.gather(task, return_exceptions=True)
        if action != "generation":
            await router.route_client({
                "jsonrpc": "2.0", "id": request["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
            })
        assert not any(m.get("id") == 14 and m.get("result", {}).get("approved") for m in messages(daemon))
        assert not router._execution_permissions
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_scope_permission_requires_owned_hosted_task_and_bounds_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        client.data.clear()
        daemon.data.clear()
        provider_id = router._connection_provider_sessions[connection_id]
        assert not await router._request_scope_permission(
            provider_id, ScopeApproval(path=Path("/outside"), recursive=False),
        )
        assert messages(client) == []
        monkeypatch.setattr("mimir.acp.proxy.MAX_SCOPE_PERMISSION_REQUESTS", 1)
        task = await start_scope_permission(router, connection_id, 16, "/outside", monkeypatch)
        request, = messages(client)
        await router.route_client({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
        })
        await task
        task2 = await start_scope_permission(router, connection_id, 17, "/other", monkeypatch)
        await task2
        assert len(messages(client)) == 1
        assert messages(daemon)[-1]["result"] == {"approved": False}
        # Daemon cannot occupy the local permission ID namespace.
        with pytest.raises(ProxyError, match="duplicate outstanding request ID"):
            await router.route_daemon(permission_request("mimir-scope:2", "session"))
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unconfined", [False, True])
async def test_scope_permission_allows_only_one_outstanding_request_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    unconfined: bool,
) -> None:
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    try:
        client.data.clear()
        daemon.data.clear()
        first = await start_scope_permission(router, connection_id, 19, "/outside", monkeypatch, unconfined=unconfined)
        request, = messages(client)
        second = await start_scope_permission(router, connection_id, 20, "/other", monkeypatch, unconfined=unconfined)
        await second
        assert messages(daemon)[-1] == {"jsonrpc": "2.0", "id": 20, "result": {"approved": False}}
        assert len(messages(client)) == 1
        await router.route_client({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        await first
        assert messages(daemon)[-1] == {"jsonrpc": "2.0", "id": 19, "result": {"approved": True}}
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_daemon_cannot_forge_local_scope_permission_metadata() -> None:
    router, client, daemon = await active_router()
    try:
        request = permission_request(1, "session")
        request["params"]["_meta"] = {"mimir.execution_scope": True}
        with pytest.raises(ProxyError, match="invalid reserved permission metadata"):
            await router.route_daemon(request)
        assert messages(client) == []
        assert messages(daemon) == []
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_hosted_scope_tool_round_trip_queries_grants_and_final_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    allowed = tmp_path / "allowed.txt"
    allowed.write_text("outside")
    rejected = tmp_path / "rejected.txt"
    rejected.write_text("outside")
    # This test covers provider -> proxy -> operator -> provider, not the OS
    # backend (which has separate real confinement probes).
    monkeypatch.setattr(
        "mimir.acp.hosted.prepare_command",
        lambda *args, **kwargs: SimpleNamespace(execution_mode="confined"),
    )
    monkeypatch.setattr("mimir.acp.hosted.validate_scope", lambda **kwargs: None)
    router, client, daemon, _, connection_id = await hosted_router(cwd)

    async def start(request_id: int, path: str) -> asyncio.Task[Any]:
        await router.route_daemon({
            "jsonrpc": "2.0", "id": request_id, "method": "mcp/message",
            "params": {
                "connectionId": connection_id, "method": "tools/call",
                "params": {"name": "request_scope", "arguments": {"path": path}},
            },
        })
        task = router._local_requests[(int, request_id)]
        assert task is not None
        await asyncio.sleep(0)
        return task

    try:
        client.data.clear()
        daemon.data.clear()
        task = await start(30, "")
        await task
        assert messages(client) == []
        assert messages(daemon)[-1]["result"]["structuredContent"]["paths"] == [str(cwd.resolve())]
        task = await start(31, str(allowed))
        request, = messages(client)
        await router.route_client({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        await task
        result = messages(daemon)[-1]["result"]["structuredContent"]
        assert result["approved"] is True
        assert set(result["paths"]) == {str(cwd.resolve()), str(allowed.resolve())}
        assert str(tmp_path.resolve()) not in result["paths"]
        assert "restart" in result["message"]
        client.data.clear()
        task = await start(32, str(rejected))
        request, = messages(client)
        await router.route_client({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
        })
        await task
        assert messages(daemon)[-1]["result"]["structuredContent"]["approved"] is False
        client.data.clear()
        task = await start(33, str(rejected))
        await task
        assert messages(client) == []
        result = messages(daemon)[-1]["result"]["structuredContent"]
        assert result["approved"] is False
        assert "do not retry" in result["message"]
        task = await start(34, "")
        await task
        assert set(messages(daemon)[-1]["result"]["structuredContent"]["paths"]) == {
            str(cwd.resolve()), str(allowed.resolve()),
        }
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_unconfined_risk_warning_is_fixed_separate_and_session_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, client, daemon, _, connection_id = await hosted_router(tmp_path)
    original_request = router._provider.request
    try:
        await grant_session(router, 40, "session", "hands_shell")
        client.data.clear()
        daemon.data.clear()
        # A path approval is not acceptance of unrestricted execution.
        scope_task = await start_scope_permission(router, connection_id, 41, "/outside", monkeypatch)
        scope_request, = messages(client)
        await router.route_client({
            "jsonrpc": "2.0", "id": scope_request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        await scope_task
        client.data.clear()
        risk_task = await start_scope_permission(
            router, connection_id, 42, "secret command/file contents", monkeypatch, unconfined=True,
        )
        risk_request, = messages(client)
        params = risk_request["params"]
        assert risk_request["method"] == "session/request_permission"
        assert risk_request["id"].startswith("mimir-unconfined:")
        assert params["_meta"] == {"mimir.unconfined_execution": True}
        assert params["toolCall"]["rawInput"] == {}
        title = params["toolCall"]["title"]
        assert "local proxy user's unrestricted filesystem permissions" in title
        assert "cwd and path-scope grants do NOT protect files" in title
        assert "session only" in title and "not persisted" in title
        assert "Rejection is final" in title
        assert "restarts any existing Python kernel and loses variables/imports" in title
        assert "secret" not in json.dumps(risk_request)
        assert [item["optionId"] for item in params["options"]] == ["allow_session", "reject_once"]
        # A duplicate answer to the earlier scope cannot answer the risk prompt.
        await router.route_client({
            "jsonrpc": "2.0", "id": scope_request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        assert not risk_task.done()
        await router.route_client({
            "jsonrpc": "2.0", "id": risk_request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
        })
        await risk_task
        assert messages(daemon)[-1]["result"] == {"approved": True}
        assert len(router._grants) == 1
        client.data.clear()
        await router.route_daemon(permission_request(43, "session", "hands_shell", tainted=True))
        assert messages(client)[-1]["params"]["_meta"]["mimir.tainted"] is True
        # A new session must have its own risk prompt; the first answer is not a
        # wrapper grant, scope grant, taint acknowledgement, or connection grant.
        monkeypatch.setattr(router._provider, "request", original_request)
        await router.route_client({
            "jsonrpc": "2.0", "id": "new-two", "method": "session/new",
            "params": {"cwd": str(tmp_path)},
        })
        server = messages(daemon)[-1]["params"]["mcpServers"][0]["serverId"]
        second_connection = await connect_hosted(router, daemon, server, 44)
        await router.route_daemon({
            "jsonrpc": "2.0", "id": "new-two", "result": {"sessionId": "two"},
        })
        client.data.clear()
        second_task = await start_scope_permission(
            router, second_connection, 46, "unused", monkeypatch, unconfined=True,
        )
        second_request, = messages(client)
        assert second_request["params"]["sessionId"] == "two"
        await router.route_client({
            "jsonrpc": "2.0", "id": second_request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
        })
        await second_task
        assert messages(daemon)[-1]["result"] == {"approved": False}
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_unconfined_permission_is_bounded_and_needs_owned_client_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, client, daemon, server_id, connection_id = await hosted_router(tmp_path)
    try:
        client.data.clear()
        daemon.data.clear()
        assert not await router._request_unconfined_permission(server_id)
        assert messages(client) == []
        monkeypatch.setattr("mimir.acp.proxy.MAX_UNCONFINED_PERMISSION_REQUESTS", 0)
        task = await start_scope_permission(
            router, connection_id, 47, "unused", monkeypatch, unconfined=True,
        )
        await task
        assert messages(client) == []
        assert messages(daemon)[-1]["result"] == {"approved": False}
        with pytest.raises(ProxyError, match="duplicate outstanding request ID"):
            await router.route_daemon(permission_request("mimir-unconfined:1", "session"))
        forged = permission_request(48, "session")
        forged["params"]["_meta"] = {"mimir.unconfined_execution": True}
        with pytest.raises(ProxyError, match="invalid reserved permission metadata"):
            await router.route_daemon(forged)
        assert messages(client) == []
        assert len(router._grants) == 0
    finally:
        await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow_session", "reject_once"])
async def test_unconfined_execution_real_operator_round_trip_and_final_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str,
) -> None:
    from mimir.acp import confinement

    if sys.platform == "darwin":
        # Exercise unavailable-platform behavior on the macOS development host.
        # Linux takes its real, unavailable backend path without this patch.
        def unavailable() -> object:
            raise confinement.BackendUnavailable("test backend unavailable")

        monkeypatch.setattr(confinement, "_backend", unavailable)
    router, client, daemon, _, connection_id = await hosted_router(
        tmp_path, approve_unconfined_for_lifecycle=False,
    )

    async def start(request_id: int, name: str, arguments: dict[str, Any]) -> asyncio.Task[Any]:
        await router.route_daemon({
            "jsonrpc": "2.0", "id": request_id, "method": "mcp/message",
            "params": {
                "connectionId": connection_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        })
        task = router._local_requests[(int, request_id)]
        assert task is not None
        await asyncio.sleep(0)
        return task

    try:
        async with asyncio.timeout(120):
            client.data.clear()
            daemon.data.clear()
            query = await start(50, "request_scope", {"path": ""})
            await query
            assert messages(client) == []
            initial_message = messages(daemon)[-1]["result"]["structuredContent"]["message"].lower()
            assert initial_message.startswith("blocked:")
            assert "operator risk acceptance" in initial_message
            assert "existing children retain their launch policy" in initial_message
            # No model-supplied switch can claim consent or change the exact schema.
            spoof = await start(51, "shell", {"command": "true", "allow_unconfined": True})
            await spoof
            assert messages(daemon)[-1]["error"]["code"] == -32602
            assert messages(client) == []
            execution = await start(52, "shell", {"command": "printf safe-output"})
            request, = messages(client)
            assert request["params"]["_meta"] == {"mimir.unconfined_execution": True}
            assert request["params"]["toolCall"]["rawInput"] == {}
            assert "safe-output" not in json.dumps(request)
            assert router._provider._processes == {}
            await router.route_client({
                "jsonrpc": "2.0", "id": request["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": decision}},
            })
            await execution
            response = messages(daemon)[-1]
            if decision == "allow_session":
                result = response["result"]["structuredContent"]
                assert result["stdout"] == "safe-output"
                assert "unconfined" in result["stderr"].lower()
            else:
                assert "error" in response
            client.data.clear()
            retry = await start(53, "shell", {"command": "printf repeated-output"})
            await retry
            assert messages(client) == []
            response = messages(daemon)[-1]
            if decision == "allow_session":
                assert response["result"]["structuredContent"]["stdout"] == "repeated-output"
            else:
                assert "error" in response
            query = await start(54, "request_scope", {"path": ""})
            await query
            assert messages(client) == []
            message = messages(daemon)[-1]["result"]["structuredContent"]["message"].lower()
            if decision == "allow_session":
                assert "unconfined" in message and "do not" in message
            else:
                assert message.startswith("blocked:")
    finally:
        await router.close()
