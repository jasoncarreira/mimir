"""WebChatBridge — POST /chat inbound + SSE /chat/stream outbound (SPEC §7.2.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.bridges.web_chat import DEFAULT_CHANNEL, WebChatBridge, _Subscriber
from mimir.models import AgentEvent
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

    @web.middleware
    async def header_identity(request, handler):
        if request.headers.get("X-API-Key") != "stream-secret":
            return web.json_response({"error": "unauthorized"}, status=401)
        request["auth_identity"] = SimpleNamespace(canonical="foo", display_name="Foo")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[header_identity])
    bridge.register_routes(a)
    return bridge, a, enqueued


@pytest.mark.asyncio
async def test_post_chat_enqueues_authenticated_user_message(tmp_path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    async with TestClient(TestServer(a)) as client:
        resp = await client.post(
            "/chat",
            json={"content": "hello", "author": "spoofed"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "channel_id": "web-alice"}
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "web-alice"
    assert e.content == "hello"
    assert e.author == "Alice"
    assert e.author_id == "alice"
    assert e.source == "web"


@pytest.mark.asyncio
async def test_post_chat_v1_enqueues_and_returns_contract_envelope(tmp_path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    async with TestClient(TestServer(a)) as client:
        resp = await client.post(
            "/api/v1/chat",
            json={"channel_id": "web-bob", "content": "hello", "msg_id": "client-1"},
        )
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"] == {"channel_id": "web-alice", "source_id": "client-1"}
    assert enqueued[0].source_id == "client-1"
    assert enqueued[0].channel_id == "web-alice"


@pytest.mark.asyncio
async def test_post_chat_rejects_anonymous_dev_open_request(bridge_app):
    _, a, enqueued = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/chat", json={"content": "x"})
        body = await resp.json()
    assert resp.status == 401
    assert body["error"] == "chat_login_required"
    assert enqueued == []


@pytest.mark.asyncio
async def test_post_chat_v1_rejects_anonymous_dev_open_request(bridge_app):
    _, a, enqueued = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/api/v1/chat", json={"content": "x"})
        body = await resp.json()
    assert resp.status == 401
    validate_api_envelope(body, expect_ok=False)
    assert body["error"]["code"] == "chat_login_required"
    assert enqueued == []


@pytest.mark.asyncio
async def test_post_chat_rejects_empty_content(tmp_path):
    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)
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
async def test_send_fans_out_to_authenticated_subscribers(authed_bridge_app):
    """An SSE subscriber receives its own-channel JSON event when the bridge sends."""
    bridge, a, _ = authed_bridge_app

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/chat/stream", headers={"X-API-Key": "stream-secret"})
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"

        await asyncio.sleep(0.05)
        await bridge.send("web-other", "not for this user")
        result = await bridge.send("web-foo", "hello there")
        assert result.sent is True

        chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
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
async def test_stream_rejects_anonymous_dev_open_request(bridge_app):
    _, a, _ = bridge_app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/chat/stream")
        body = await resp.json()
    assert resp.status == 401
    assert body["error"] == "chat_login_required"


@pytest.mark.asyncio
async def test_chat_stream_rejects_when_subscriber_cap_reached(authed_bridge_app, monkeypatch):
    from mimir.bridges import web_chat

    monkeypatch.setattr(web_chat, "CHAT_STREAM_MAX_SUBSCRIBERS", 1)
    bridge, a, _ = authed_bridge_app

    async with TestClient(TestServer(a)) as client:
        resp1 = await client.get("/chat/stream", headers={"X-API-Key": "stream-secret"})
        assert resp1.status == 200
        await asyncio.sleep(0.05)
        assert len(bridge._subscribers) == 1

        resp2 = await client.get("/chat/stream", headers={"X-API-Key": "stream-secret"})
        assert resp2.status == 429
        assert await resp2.text() == "too many chat streams"
        assert len(bridge._subscribers) == 1


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
async def test_react_emits_event(tmp_path):
    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="x", display_name="X")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

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
    bridge._subscribers.append(_Subscriber(q))
    await bridge.disconnect()
    assert q.get_nowait() is None  # sentinel pushed


@pytest.mark.asyncio
async def test_chat_history_returns_authenticated_user_conversation(tmp_path):
    """GET /api/v1/chat/history restores the authenticated user's conversation."""
    import mimir.history as history_mod
    from mimir.history import MessageBuffer, set_global_buffer

    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    (tmp_path / "hist").mkdir()
    buf = MessageBuffer(history_path=tmp_path / "hist" / "chat_history.jsonl")
    await buf.append(buf.make_message(channel_id="web-alice", kind="user_message", content="hello", author="alice"))
    await buf.append(buf.make_message(channel_id="web-alice", kind="assistant_message", content="hi alice", author="mimir"))
    await buf.append(buf.make_message(channel_id="web-other", kind="user_message", content="elsewhere", author="bob"))
    await buf.append(buf.make_message(channel_id="web-alice", kind="system_note", content="note", author=None))

    prev = history_mod.get_global_buffer()
    set_global_buffer(buf)
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get("/api/v1/chat/history?channel_id=web-other")
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["channel_id"] == "web-alice"
    msgs = body["data"]["messages"]
    assert [m["text"] for m in msgs] == ["hello", "hi alice"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert all(m["channel_id"] == "web-alice" for m in msgs)


@pytest.mark.asyncio
async def test_chat_history_empty_without_buffer(tmp_path):
    """No global buffer registered (test paths) → empty history, not a 500."""
    import mimir.history as history_mod

    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    prev = history_mod.get_global_buffer()
    history_mod._global_buffer = None
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get("/api/v1/chat/history")
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert resp.status == 200
    assert body["data"] == {"channel_id": "web-alice", "messages": []}


def test_web_channel_for_uses_canonical_verbatim():
    """The canonical is the unique per-user key, so the channel uses it verbatim —
    a lossy slug could collapse distinct ids onto one channel and leak history."""
    from mimir.bridges.web_chat import DEFAULT_CHANNEL, _web_channel_for

    assert _web_channel_for("alice") == "web-alice"
    # distinct canonicals a slug would collapse must stay DISTINCT channels
    assert _web_channel_for("a.b") != _web_channel_for("a_b")
    assert _web_channel_for("Alice") != _web_channel_for("alice")
    # Empty canonicals still map to the historical constant at the pure helper
    # layer, but no chat route can reach this without an authenticated identity.
    assert _web_channel_for("") == DEFAULT_CHANNEL
    # reserved shared default channel must not be reachable by a real identity
    assert _web_channel_for("default") != DEFAULT_CHANNEL


@pytest.mark.asyncio
async def test_authenticated_default_channel_routes_per_user(tmp_path):
    """An authenticated user's post routes to web-<canonical>;
    client-supplied channel ids are ignored."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)
    async with TestClient(TestServer(a)) as client:
        r1 = await client.post("/api/v1/chat", json={"content": "hi"})
        b1 = await r1.json()
        r2 = await client.post("/api/v1/chat", json={"content": "yo", "channel_id": "web-bob"})
        b2 = await r2.json()

    assert b1["data"]["channel_id"] == "web-alice"
    assert enqueued[0].channel_id == "web-alice" and enqueued[0].author == "Alice"
    assert b2["data"]["channel_id"] == "web-alice"
    assert enqueued[1].channel_id == "web-alice"


@pytest.mark.asyncio
async def test_history_authenticated_resolves_to_per_user_channel(tmp_path):
    """GET /api/v1/chat/history with no channel resolves to the authed user's
    channel and returns only their conversation, not another user's."""
    import mimir.history as history_mod
    from mimir.history import MessageBuffer, set_global_buffer

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    (tmp_path / "hist").mkdir()
    buf = MessageBuffer(history_path=tmp_path / "hist" / "chat_history.jsonl")
    await buf.append(buf.make_message(channel_id="web-alice", kind="user_message", content="alice question", author="Alice"))
    await buf.append(buf.make_message(channel_id="web-alice", kind="assistant_message", content="alice answer", author="mimir"))
    await buf.append(buf.make_message(channel_id="web-bob", kind="user_message", content="bob question", author="Bob"))

    prev = history_mod.get_global_buffer()
    set_global_buffer(buf)
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get("/api/v1/chat/history")  # no channel_id
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert body["data"]["channel_id"] == "web-alice"
    assert [m["text"] for m in body["data"]["messages"]] == ["alice question", "alice answer"]


@pytest.mark.asyncio
async def test_authenticated_default_canonical_does_not_share_web_default(tmp_path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(
            canonical="default",
            display_name="Default User",
        )
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/api/v1/chat", json={"content": "hi"})
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["channel_id"] != DEFAULT_CHANNEL
    assert body["data"]["channel_id"].startswith("web-user:")
    assert enqueued[0].channel_id == body["data"]["channel_id"]


@pytest.mark.asyncio
async def test_authenticated_user_channel_body_is_ignored(tmp_path):
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/api/v1/chat", json={"content": "yo", "channel_id": "web-bob"})
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["channel_id"] == "web-alice"
    assert enqueued[0].channel_id == "web-alice"


@pytest.mark.asyncio
async def test_default_canonical_history_does_not_read_shared_web_default(tmp_path):
    import mimir.history as history_mod
    from mimir.history import MessageBuffer, set_global_buffer

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(
            canonical="default",
            display_name="Default User",
        )
        return await handler(request)

    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    (tmp_path / "hist").mkdir()
    buf = MessageBuffer(history_path=tmp_path / "hist" / "chat_history.jsonl")
    await buf.append(buf.make_message(channel_id=DEFAULT_CHANNEL, kind="user_message", content="shared", author="curl"))

    prev = history_mod.get_global_buffer()
    set_global_buffer(buf)
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get(f"/api/v1/chat/history?channel_id={DEFAULT_CHANNEL}")
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert resp.status == 200
    assert body["data"]["channel_id"] != DEFAULT_CHANNEL
    assert body["data"]["messages"] == []


@pytest.mark.asyncio
async def test_authenticated_history_ignores_other_web_channel_query(tmp_path):
    import mimir.history as history_mod
    from mimir.history import MessageBuffer, set_global_buffer

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    (tmp_path / "hist").mkdir()
    buf = MessageBuffer(history_path=tmp_path / "hist" / "chat_history.jsonl")
    await buf.append(buf.make_message(channel_id="web-bob", kind="user_message", content="bob question", author="Bob"))

    prev = history_mod.get_global_buffer()
    set_global_buffer(buf)
    try:
        async with TestClient(TestServer(a)) as client:
            resp = await client.get("/api/v1/chat/history?channel_id=web-bob")
            body = await resp.json()
    finally:
        history_mod._global_buffer = prev

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["channel_id"] == "web-alice"
    assert body["data"]["messages"] == []


@pytest.mark.asyncio
async def test_authenticated_stream_only_receives_own_web_channel(tmp_path):
    async def fake_enqueue(event: AgentEvent) -> bool:
        return True

    @web.middleware
    async def inject_identity(request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="alice", display_name="Alice")
        return await handler(request)

    bridge = WebChatBridge(enqueue=fake_enqueue, home=tmp_path)
    a = web.Application(middlewares=[inject_identity])
    bridge.register_routes(a)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/chat/stream")
        assert resp.status == 200
        await asyncio.sleep(0.05)

        await bridge.send("web-bob", "bob secret")
        await bridge.send("web-alice", "alice visible")
        chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        while chunk and not chunk.startswith(b"data:"):
            chunk = await asyncio.wait_for(resp.content.readline(), timeout=2.0)
        payload = json.loads(chunk[len(b"data: "):].strip())

    assert payload["channel_id"] == "web-alice"
    assert payload["text"] == "alice visible"
