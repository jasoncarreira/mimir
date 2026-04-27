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
        assert payload["channel_id"] == "web-foo"
        assert payload["text"] == "hello there"
        assert payload["message_id"] == result.message_id


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
