"""``operator_live_notice``: the titled text reaches only the web chat bridge."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.bridges.base import SendResult
from mimir.bridges.web_chat import WebChatBridge, _Subscriber
from mimir.channel_registry import ChannelRegistry
from mimir.server import _operator_notice_callbacks

LIVE = "Tracker issue withheld: github_issues acme/widget#7 (x) https://u - TITLE"
FREE = "Tracker issue withheld: github_issues acme/widget#7 (x) https://u"


class _HistoryBridge:
    prefixes = ("discord-",)
    name = "discord"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []

    async def send(self, channel_id, text, attachment_paths=None, *, final=True, **kw):
        self.sent.append((channel_id, text, kw))
        return SendResult(sent=True, message_id="m1", chunks=1)


def _config(channel: str | None) -> SimpleNamespace:
    return SimpleNamespace(operator_alert_channel=channel)


@pytest.mark.asyncio
async def test_web_alert_channel_gets_title_live_only(tmp_path: Path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from mimir.history import MessageBuffer, set_global_buffer

    buffer = MessageBuffer(tmp_path / "chat_history.jsonl")
    set_global_buffer(buffer)
    bridge = WebChatBridge(enqueue=lambda e: asyncio.sleep(0, result=True), home=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    bridge._subscribers.append(_Subscriber(queue=queue))
    channels = ChannelRegistry()
    channels.register(bridge)
    _, live_notice = _operator_notice_callbacks(_config("web-ops"), channels)

    @web.middleware
    async def authenticate(request, handler):
        request["auth_identity"] = SimpleNamespace(
            canonical="owner", access=SimpleNamespace(is_admin=True),
        )
        return await handler(request)

    app = web.Application(middlewares=[authenticate])
    bridge.register_routes(app)
    client = TestClient(TestServer(app))
    try:
        await live_notice(LIVE, FREE)

        payload = queue.get_nowait()
        assert payload["text"] == LIVE
        assert buffer.channel_count("web-ops") == 0
        assert not [r for r in buffer.recent_for_channel("web-ops", 10) if "TITLE" in r.content]
        assert not [
            p for p in tmp_path.rglob("*")
            if p.is_file() and "TITLE" in p.read_text(errors="replace")
        ]
        await client.start_server()
        response = await client.get("/api/v1/chat/history")
        assert response.status == 200
        messages = (await response.json())["data"]["messages"]
        assert not [m for m in messages if "TITLE" in m["text"]]
    finally:
        await client.close()
        set_global_buffer(None)


@pytest.mark.asyncio
async def test_history_bridges_get_title_free_line():
    bridge = _HistoryBridge()
    channels = ChannelRegistry()
    channels.register(bridge)
    _, live_notice = _operator_notice_callbacks(_config("discord-123"), channels)

    await live_notice(LIVE, FREE)

    assert bridge.sent == [("discord-123", FREE, {})]


@pytest.mark.asyncio
async def test_no_alert_channel_sends_nothing():
    bridge = _HistoryBridge()
    channels = ChannelRegistry()
    channels.register(bridge)
    for channel in ("", "  ", None):
        _, live_notice = _operator_notice_callbacks(_config(channel), channels)
        await live_notice(LIVE, FREE)

    assert bridge.sent == []


@pytest.mark.asyncio
async def test_unknown_alert_channel_sends_nothing():
    channels = ChannelRegistry()
    _, live_notice = _operator_notice_callbacks(_config("nowhere-1"), channels)

    await live_notice(LIVE, FREE)


@pytest.mark.asyncio
async def test_operator_notice_behaves_as_before():
    bridge = _HistoryBridge()
    channels = ChannelRegistry()
    channels.register(bridge)
    notice, _ = _operator_notice_callbacks(_config("discord-9"), channels)

    await notice("hello")

    assert bridge.sent == [("discord-9", "hello", {})]
