"""SSE capacity belongs to the authenticated canonical, not the whole server."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from mimir import web_ui
from mimir.bridges.web_chat import WebChatBridge
from mimir.turn_event_bus import TurnEventBus


@pytest.mark.parametrize("path", ["/api/v1/live-events", "/api/v1/turn-events", "/chat/stream"])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("release", ["cancel", "prepare_failure"])
async def test_stream_caps_isolate_identities_and_release(tmp_path, monkeypatch, path, admin, release):
    monkeypatch.setattr(web_ui, "LIVE_EVENTS_MAX_STREAMS", 8)
    monkeypatch.setattr(web_ui, "TURN_EVENTS_MAX_STREAMS", 8)
    app = web.Application()
    web_ui.register_routes(
        app,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        turn_event_bus=TurnEventBus(),
    )
    bridge = WebChatBridge(enqueue=AsyncMock(), home=tmp_path, max_subscribers=8)
    bridge.register_routes(app)
    handler = next(r.handler for r in app.router.routes() if r.method == "GET" and r.resource.canonical == path)
    prepared = asyncio.Queue()

    async def hold_prepare(response, request):
        await prepared.put(request)
        await request["disconnect"].wait()
        raise ConnectionResetError("handshake failed")

    monkeypatch.setattr(web.StreamResponse, "prepare", hold_prepare)

    def request(canonical, key, channel):
        req = make_mocked_request("GET", f"{path}?channel={channel}", headers={"X-API-Key": key})
        # Distinct resolved objects/keys with the same canonical share capacity.
        req["auth_identity"] = SimpleNamespace(canonical=canonical)
        req["auth_is_admin"] = admin
        req["disconnect"] = asyncio.Event()
        return req

    tasks = []
    try:
        alice = request("alice", "alice-key-1", "web-alice")
        first = asyncio.create_task(handler(alice))
        tasks.append(first)
        assert await asyncio.wait_for(prepared.get(), 2) is alice
        for _ in range(7):
            tab = request("alice", "alice-key-1", "web-alice")
            tasks.append(asyncio.create_task(handler(tab)))
            assert await asyncio.wait_for(prepared.get(), 2) is tab
        bob = request("bob", "bob-key", "web-bob")
        tasks.append(asyncio.create_task(handler(bob)))
        assert await asyncio.wait_for(prepared.get(), 2) is bob

        # Admin channel selection must not provide extra quota buckets.
        again = request("alice", "alice-key-2", "web-other" if admin else "web-alice")
        denied = await asyncio.wait_for(handler(again), 2)
        assert denied.status == 429
        if release == "cancel":
            first.cancel()
        else:
            alice["disconnect"].set()
        await asyncio.wait_for(first, 2)

        tasks.append(asyncio.create_task(handler(again)))
        assert await asyncio.wait_for(prepared.get(), 2) is again
        denied = await asyncio.wait_for(handler(again), 2)
        assert denied.status == 429
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert bridge._subscribers == []


def test_dashboard_stream_bucket_semantics():
    def bucket(canonical=None, **auth):
        request = dict(auth)
        if canonical is not None:
            request["auth_identity"] = SimpleNamespace(canonical=canonical)
        return web_ui._stream_identity_bucket(request)

    assert bucket("alice", auth_is_admin=True) == bucket("alice")
    assert bucket(auth_is_master=True) == bucket("alice", auth_is_master=True)
    assert len({bucket(), bucket(auth_is_master=True), bucket("master"), bucket("unauthenticated")}) == 4
