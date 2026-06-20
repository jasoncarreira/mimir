"""WebChatBridge — POST /chat inbound + SSE /chat/stream outbound (SPEC §7.2.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.bridges.web_chat import DEFAULT_CHANNEL, WebChatBridge
from mimir.models import AgentEvent
from mimir.server import _make_auth_middleware
from mimir.web_contracts import validate_api_envelope, validate_live_event


@pytest.fixture
def bridge_app(tmp_path: Path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application()
    bridge.register_routes(a)
    return bridge, a, enqueued


@pytest.fixture
def authed_bridge_app(tmp_path: Path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[_make_auth_middleware("stream-secret")])
    bridge.register_routes(a)
    return bridge, a, enqueued


@pytest.mark.asyncio
async def test_post_chat_enqueues_user_message(bridge_app):
    bridge, a, enqueued = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post(
            "/chat",
            json={"channel_id": "web-foo", "content": "hello", "author": "alice"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "channel_id": "web-foo"}
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "web-foo"
    assert e.content == "hello"
    assert e.author == "alice"
    assert e.source == "web"


@pytest.mark.asyncio
async def test_post_chat_v1_enqueues_and_returns_contract_envelope(bridge_app):
    _, a, enqueued = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post(
            "/api/v1/chat",
            json={"channel_id": "web-foo", "content": "hello", "msg_id": "client-1"},
        )
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"] == {"channel_id": "web-foo", "source_id": "client-1"}
    assert enqueued[0].source_id == "client-1"


@pytest.mark.asyncio
async def test_post_chat_default_channel(bridge_app):
    _, a, enqueued = bridge_app
    async with TestClient(TestServer(a)) as client:
        await client.post("/chat", json={"content": "x"})
    assert enqueued[0].channel_id == DEFAULT_CHANNEL


@pytest.mark.asyncio
async def test_post_chat_prefixes_channel_id(bridge_app):
    """Client sending channel_id='alice' (no prefix) gets normalized to web-alice
    so the registry's prefix dispatch lands here."""
    _, a, enqueued = bridge_app
    async with TestClient(TestServer(a)) as client:
        await client.post("/chat", json={"channel_id": "alice", "content": "hi"})
    assert enqueued[0].channel_id == "web-alice"


@pytest.mark.asyncio
async def test_post_chat_rejects_empty_content(bridge_app):
    _, a, _ = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/chat", json={"content": "   "})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_post_chat_v1_errors_use_stable_envelope(bridge_app):
    _, a, _ = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/api/v1/chat", json={"content": "   "})
        body = await resp.json()

    assert resp.status == 400
    validate_api_envelope(body, expect_ok=False)
    assert body["error"]["code"] == "bad_request"


@pytest.mark.asyncio
async def test_post_chat_rejects_bad_json(bridge_app):
    _, a, _ = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/chat", data="not json")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_send_fans_out_to_subscribers(bridge_app):
    """An SSE subscriber receives a JSON event when the bridge ``send``s."""
    bridge, a, _ = bridge_app

    async with TestClient(TestServer(a)) as client:
        # Open the SSE stream in a background task and read the first chunk.
        resp = await client.get("/chat/stream")
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"

        # Give the handler a tick to register the subscriber, then trigger send.
        await asyncio.sleep(0.05)
        result = await bridge.send("web-foo", "hello there")
        assert result.sent is True

        # Read until we see a data: line.
        chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        # Heartbeats arrive as ": heartbeat\n" — keep reading until a data line.
        while chunk and not chunk.startswith(b"data:"):
            chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        assert chunk.startswith(b"data: ")
        payload = json.loads(chunk[len(b"data: "):].strip())
        validate_live_event(payload)
        assert payload["kind"] == "chat.message"
        assert payload["channel_id"] == "web-foo"
        assert payload["text"] == "hello there"
        assert payload["message_id"] == result.message_id


@pytest.mark.asyncio
async def test_stream_auth_uses_header_not_query_param(authed_bridge_app):
    bridge, a, _ = authed_bridge_app
    async with TestClient(TestServer(a)) as client:
        query_resp = await client.get("/chat/stream?api_key=stream-secret")
        assert query_resp.status == 401

        resp = await client.get(
            "/chat/stream",
            headers={"X-API-Key": "stream-secret"},
        )
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"

        await asyncio.sleep(0.05)
        await bridge.send("web-foo", "header authed")
        chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        while chunk and not chunk.startswith(b"data:"):
            chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        payload = json.loads(chunk[len(b"data: "):].strip())
        assert payload["text"] == "header authed"


@pytest.mark.asyncio
async def test_send_with_no_subscribers_succeeds(bridge_app):
    """No SSE clients connected — send still succeeds (drops on the floor)."""
    bridge, _, _ = bridge_app
    result = await bridge.send("web-x", "hi")
    assert result.sent is True
    assert result.message_id is not None


@pytest.mark.asyncio
async def test_react_emits_event(bridge_app):
    bridge, a, _ = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/chat/stream")
        await asyncio.sleep(0.05)
        ok = await bridge.react("web-x", "msg-1", "👍")
        assert ok is True
        chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        while chunk and not chunk.startswith(b"data:"):
            chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        payload = json.loads(chunk[len(b"data: "):].strip())
        validate_live_event(payload)
        assert payload["kind"] == "chat.reaction"
        assert payload["_event"] == "react"
        assert payload["emoji"] == "👍"


@pytest.mark.asyncio
async def test_disconnect_drains_subscribers(tmp_path: Path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(e: AgentEvent) -> bool:
        enqueued.append(e)
        return True

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    # Subscribe by hand to test the close path without an HTTP client.
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    bridge._subscribers.append(q)
    await bridge.disconnect()
    assert q.get_nowait() is None  # sentinel pushed


@pytest.mark.asyncio
async def test_chat_history_returns_channel_conversation(bridge_app, tmp_path):
    """GET /api/v1/chat/history restores a web channel's prior conversation —
    this channel only, user+assistant only, oldest→newest."""
    import mimir.history as history_mod
    from mimir.history import MessageBuffer, set_global_buffer

    _, a, _ = bridge_app
    (tmp_path / "hist").mkdir()
    buf = MessageBuffer(history_path=tmp_path / "hist" / "chat_history.jsonl")
    await buf.append(buf.make_message(channel_id="web-default", kind="user_message", content="hello", author="alice"))
    await buf.append(buf.make_message(channel_id="web-default", kind="assistant_message", content="hi alice", author="mimir"))
    await buf.append(buf.make_message(channel_id="web-other", kind="user_message", content="elsewhere", author="bob"))
    await buf.append(buf.make_message(channel_id="web-default", kind="system_note", content="note", author=None))

    prev = history_mod.get_global_buffer()
    set_global_buffer(buf)
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get("/api/v1/chat/history?channel_id=web-default")
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    msgs = body["data"]["messages"]
    assert [m["text"] for m in msgs] == ["hello", "hi alice"]  # web-default, no system_note, in order
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert all(m["channel_id"] == "web-default" for m in msgs)


@pytest.mark.asyncio
async def test_chat_history_empty_without_buffer(bridge_app):
    """No global buffer registered (test paths) → empty history, not a 500."""
    import mimir.history as history_mod

    _, a, _ = bridge_app
    prev = history_mod.get_global_buffer()
    history_mod._global_buffer = None
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get("/api/v1/chat/history")
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert resp.status == 200
    assert body["data"] == {"channel_id": "web-default", "messages": []}
