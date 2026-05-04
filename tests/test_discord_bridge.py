"""DiscordBridge — message chunking, channel-id helpers, and the Bridge ABC
contract under a fake discord client (SPEC §7.2.1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Skip the whole module if discord-py isn't installed in the test env.
pytest.importorskip("discord")

from mimir.bridges.discord import (
    DISCORD_MESSAGE_CHAR_LIMIT,
    DiscordBridge,
    _channel_conversation_type,
    _channel_id_to_int,
    _channel_to_id,
    _channel_visibility,
    _chunk_message,
)
from mimir.event_logger import init_logger
from mimir.models import AgentEvent


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


# ---- chunking -----------------------------------------------------------


def test_chunk_short_returns_one():
    assert _chunk_message("hi") == ["hi"]


def test_chunk_respects_limit():
    text = "x" * (DISCORD_MESSAGE_CHAR_LIMIT * 3)
    chunks = _chunk_message(text)
    assert all(len(c) <= DISCORD_MESSAGE_CHAR_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_chunk_prefers_paragraph_boundaries():
    # Two paragraphs that each fit, separated by a blank line. Should NOT be
    # split mid-paragraph if both fit in one chunk together.
    a = "alpha\nbeta\n"
    b = "gamma\ndelta"
    text = a + "\n" + b
    chunks = _chunk_message(text, limit=200)
    assert chunks == [text]


def test_chunk_falls_back_to_lines_when_paragraph_too_big():
    # One huge paragraph with line breaks — should split on \n boundaries.
    big_para = "\n".join("L" * 100 for _ in range(30))
    chunks = _chunk_message(big_para, limit=500)
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


def test_chunk_handles_no_break_at_all():
    text = "z" * 5000
    chunks = _chunk_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == text


# ---- channel-id helpers --------------------------------------------------


def _fake_channel(*, id: int, type_name: str = "text", is_dm: bool = False, name: str | None = "general"):
    """Tiny ``discord.abc.Messageable``-shaped stand-in for the helpers."""
    import discord

    if is_dm:
        # _channel_conversation_type checks ``type == ChannelType.private`` first
        # (before isinstance), so setting type=private is enough — no need to
        # masquerade as a real DMChannel.
        type_name = "private"
    obj = SimpleNamespace(id=id, type=getattr(discord.ChannelType, type_name, None), name=name)
    return obj


def test_channel_to_id_text_channel():
    ch = _fake_channel(id=12345, type_name="text")
    assert _channel_to_id(ch) == "discord-12345"


def test_channel_to_id_dm():
    """A DM channel routes through the dm-discord- prefix."""
    ch = _fake_channel(id=99, is_dm=True, name=None)
    assert _channel_to_id(ch) == "dm-discord-99"


def test_channel_id_to_int_round_trip():
    assert _channel_id_to_int("discord-42") == 42
    assert _channel_id_to_int("dm-discord-7") == 7
    assert _channel_id_to_int("slack-foo") is None
    assert _channel_id_to_int("discord-not-an-int") is None


def test_channel_conversation_type_dm():
    assert _channel_conversation_type(_fake_channel(id=99, is_dm=True)) == "dm"


def test_channel_conversation_type_text():
    assert _channel_conversation_type(_fake_channel(id=1, type_name="text")) == "multi_user"


def test_channel_visibility_dm_is_private():
    assert _channel_visibility(_fake_channel(id=1, is_dm=True), "dm") == "private"


# ---- bridge surface ------------------------------------------------------


@pytest.fixture
def bridge_with_fake_client(tmp_path: Path):
    """Build a DiscordBridge with a fake discord client + mock enqueue.
    Lets us exercise send/react/_on_message without a real network connection.
    """
    import discord

    enqueued: list[AgentEvent] = []

    async def fake_enqueue(e: AgentEvent) -> bool:
        enqueued.append(e)
        return True

    bridge = DiscordBridge(token="TEST", enqueue=fake_enqueue)

    sent: list[tuple[int, str, list]] = []

    class FakeChannel:
        def __init__(self, cid: int):
            self.id = cid
            self.type = getattr(discord.ChannelType, "text", None)
            self.name = "test"
            self._next_msg_id = 1000

        async def send(self, content: str = "", files=None):
            sent.append((self.id, content, files or []))
            self._next_msg_id += 1
            return SimpleNamespace(id=self._next_msg_id)

        async def fetch_message(self, mid: int):
            msg = SimpleNamespace(id=mid)
            msg.add_reaction = AsyncMock()
            return msg

    # The bridge duck-types the messageable check (callable .send), so the
    # SimpleNamespace-shaped FakeChannel is fine without an ABC dance.

    class FakeClient:
        user = SimpleNamespace(id=42, name="mimir-bot")

        def __init__(self):
            self._channels = {1: FakeChannel(1), 2: FakeChannel(2)}

        def is_closed(self):
            return False

        def get_channel(self, cid: int):
            return self._channels.get(cid)

        async def fetch_channel(self, cid: int):
            return self._channels.get(cid)

        async def close(self):
            pass

    bridge._client = FakeClient()  # type: ignore[assignment]
    return bridge, enqueued, sent


@pytest.mark.asyncio
async def test_send_chunks_long_text(bridge_with_fake_client):
    bridge, _, sent = bridge_with_fake_client
    long_text = "y" * (DISCORD_MESSAGE_CHAR_LIMIT * 2 + 100)
    result = await bridge.send("discord-1", long_text)
    assert result.sent is True
    assert result.chunks == 3  # 2*limit + 100 → 3 chunks under the limit
    # All chunks landed on the right channel.
    assert all(channel_id == 1 for channel_id, _, _ in sent)


@pytest.mark.asyncio
async def test_send_rejects_unknown_channel_id_format(bridge_with_fake_client):
    bridge, _, sent = bridge_with_fake_client
    result = await bridge.send("not-a-discord-channel", "hi")
    assert result.sent is False
    assert "bad channel_id" in (result.error or "")
    assert sent == []


@pytest.mark.asyncio
async def test_send_returns_message_id(bridge_with_fake_client):
    bridge, _, _ = bridge_with_fake_client
    result = await bridge.send("discord-2", "hello")
    assert result.sent is True
    assert result.message_id is not None
    assert result.chunks == 1


@pytest.mark.asyncio
async def test_react_returns_false_for_bad_message_id(bridge_with_fake_client):
    bridge, _, _ = bridge_with_fake_client
    ok = await bridge.react("discord-1", "not-a-number", "👍")
    assert ok is False


@pytest.mark.asyncio
async def test_on_message_enqueues_user_message(bridge_with_fake_client):
    """A real-shape inbound message lands on the dispatcher with the right
    channel_id, source, and metadata."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client

    channel = SimpleNamespace(
        id=1,
        type=getattr(discord.ChannelType, "text", None),
        name="general",
        guild=None,
        permissions_for=None,
    )
    author = SimpleNamespace(
        id=99,
        bot=False,
        # Discord exposes display_name (server nickname OR global name) and
        # global_name on the Member/User object — bridge prefers these
        # over str() now.
        display_name="Alice in this server",
        global_name="Alice Smith",
    )
    msg = SimpleNamespace(
        id=555,
        author=author,
        channel=channel,
        content="hello mimir",
        mentions=[],
    )
    await bridge._on_message(msg)
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "discord-1"
    assert e.content == "hello mimir"
    assert e.source == "discord"
    assert e.source_id == "555"
    assert e.author_id == "99"
    # FUTURE_WORK §6.1: author is the platform-prefixed matching key.
    assert e.author == "discord-99"
    # Discord display preference: server-nickname (display_name) wins over
    # global_name and over str(author).
    assert e.author_display == "Alice in this server"
    assert e.extra["channel_conversation_type"] == "multi_user"


@pytest.mark.asyncio
async def test_on_message_falls_back_to_global_name(bridge_with_fake_client):
    """When the user has no server-specific nickname (display_name),
    fall back to global_name (their cross-server display)."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    channel = SimpleNamespace(
        id=1, type=getattr(discord.ChannelType, "text", None), name="general", guild=None,
    )
    # display_name absent → global_name picks up. (id=77 to avoid colliding
    # with the fake bot's user_id=42 in the fixture.)
    author = SimpleNamespace(id=77, bot=False, global_name="Alice Smith")
    msg = SimpleNamespace(id=1, author=author, channel=channel, content="hi", mentions=[])
    await bridge._on_message(msg)
    assert enqueued[0].author_display == "Alice Smith"


@pytest.mark.asyncio
async def test_on_message_skips_self(bridge_with_fake_client):
    """Messages from the bot's own user id are dropped on the floor."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    channel = SimpleNamespace(
        id=1, type=getattr(discord.ChannelType, "text", None), name="g"
    )
    self_author = SimpleNamespace(id=42, bot=True, __str__=lambda self: "mimir-bot")
    msg = SimpleNamespace(
        id=1, author=self_author, channel=channel, content="echo of own msg", mentions=[]
    )
    await bridge._on_message(msg)
    assert enqueued == []


@pytest.mark.asyncio
async def test_on_message_skips_bot_unless_opted_in(bridge_with_fake_client):
    """A non-self bot message is dropped unless ``respond_to_bots=True``."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    bridge.respond_to_bots = False

    channel = SimpleNamespace(
        id=1, type=getattr(discord.ChannelType, "text", None), name="g"
    )
    bot_author = SimpleNamespace(id=999, bot=True, __str__=lambda self: "OtherBot")
    msg = SimpleNamespace(
        id=2, author=bot_author, channel=channel, content="hi", mentions=[]
    )
    await bridge._on_message(msg)
    assert enqueued == []

    bridge.respond_to_bots = True
    await bridge._on_message(msg)
    assert len(enqueued) == 1
    assert enqueued[0].author_id == "999"


@pytest.mark.asyncio
async def test_send_reports_disconnected_client(tmp_path: Path):
    """An unconnected bridge fails send rather than crashing."""
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    result = await bridge.send("discord-1", "hi")
    assert result.sent is False
    assert "not connected" in (result.error or "")


@pytest.mark.asyncio
async def test_send_typing_indicator_calls_trigger_typing(bridge_with_fake_client):
    """The Discord typing-dots fire fires when the bridge gets a real
    inbound message — verify trigger_typing is invoked on the channel."""
    bridge, _, _ = bridge_with_fake_client
    # Attach trigger_typing to the fake channel so we can observe the call.
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel.trigger_typing = AsyncMock()
    await bridge.send_typing_indicator("discord-1")
    channel.trigger_typing.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_typing_indicator_swallows_failures(bridge_with_fake_client):
    """trigger_typing errors don't propagate — typing is best-effort."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]

    async def boom():
        raise RuntimeError("rate-limited")

    channel.trigger_typing = boom
    # Should not raise.
    await bridge.send_typing_indicator("discord-1")


@pytest.mark.asyncio
async def test_send_typing_indicator_unconnected_noops(tmp_path: Path):
    """No client → silent no-op, no exception."""
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    await bridge.send_typing_indicator("discord-1")  # must not raise


@pytest.mark.asyncio
async def test_on_message_downloads_inbound_attachments(
    bridge_with_fake_client, tmp_path: Path,
):
    """When attachments_dir is set, message attachments are downloaded
    to the per-channel-per-chat dir and surfaced on the AgentEvent."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    bridge.attachments_dir = tmp_path / "att"

    # Patch download_to_path to a fake that writes a marker file.
    from mimir.bridges import _attachments as att_mod

    async def fake_download(url, target, **kw):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FAKE-DOWNLOAD")
        return True

    monkey_orig = att_mod.download_to_path
    att_mod.download_to_path = fake_download
    try:
        channel = SimpleNamespace(
            id=1, type=getattr(discord.ChannelType, "text", None),
            name="general", guild=None, permissions_for=None,
        )
        author = SimpleNamespace(
            id=11, bot=False, display_name="A", global_name="A",
            __str__=lambda self: "a",
        )
        # Discord attachment objects expose url, filename, size.
        attachments = [
            SimpleNamespace(
                id="a1", url="https://cdn.example/a.png",
                filename="report.png", size=1024, content_type="image/png",
            ),
        ]
        msg = SimpleNamespace(
            id=42, author=author, channel=channel,
            content="see attached", attachments=attachments, mentions=[],
        )
        await bridge._on_message(msg)
    finally:
        att_mod.download_to_path = monkey_orig

    assert len(enqueued) == 1
    ev = enqueued[0]
    assert len(ev.attachment_names) == 1
    saved = Path(ev.attachment_names[0])
    assert saved.exists()
    assert saved.read_bytes() == b"FAKE-DOWNLOAD"
    # Layout: <root>/discord/<channel_id>/<ts>-<token>-report.png
    assert saved.parent.name == "1"
    assert saved.parent.parent.name == "discord"
    # Original URL preserved on extra for fallback.
    assert ev.extra.get("inbound_attachment_urls") == ["https://cdn.example/a.png"]


@pytest.mark.asyncio
async def test_on_message_skips_oversized_attachments(
    bridge_with_fake_client, tmp_path: Path,
):
    """Files over attachments_max_bytes don't get downloaded; the URL
    still surfaces in extra so the agent can fetch_url manually if it
    wants to."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    bridge.attachments_dir = tmp_path / "att"
    bridge.attachments_max_bytes = 100

    from mimir.bridges import _attachments as att_mod
    download_calls: list[str] = []

    async def fake_download(url, target, **kw):
        download_calls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return True

    att_mod.download_to_path, prev = fake_download, att_mod.download_to_path
    try:
        channel = SimpleNamespace(
            id=1, type=getattr(discord.ChannelType, "text", None),
            name="g", guild=None, permissions_for=None,
        )
        author = SimpleNamespace(
            id=22, bot=False, display_name="B", global_name="B",
            __str__=lambda self: "b",
        )
        attachments = [
            SimpleNamespace(
                id="a1", url="https://cdn.example/big.bin",
                filename="big.bin", size=99999, content_type=None,
            ),
        ]
        msg = SimpleNamespace(
            id=43, author=author, channel=channel,
            content="big file", attachments=attachments, mentions=[],
        )
        await bridge._on_message(msg)
    finally:
        att_mod.download_to_path = prev

    assert download_calls == []  # oversized → never called
    ev = enqueued[0]
    assert ev.attachment_names == []  # no local copy
    # URL still listed so the agent has the option to retrieve.
    assert "https://cdn.example/big.bin" in ev.extra.get("inbound_attachment_urls", [])


@pytest.mark.asyncio
async def test_on_message_fires_typing_before_enqueue(bridge_with_fake_client):
    """Inbound messages should trigger the typing indicator. The bridge
    fires it as a background task so enqueue isn't blocked — verify it
    was scheduled."""
    import asyncio

    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    channel_obj = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel_obj.trigger_typing = AsyncMock()

    channel = SimpleNamespace(
        id=1,
        type=getattr(discord.ChannelType, "text", None),
        name="general",
        guild=None,
        permissions_for=None,
    )
    author = SimpleNamespace(
        id=77, bot=False, display_name="Bob", global_name="Bob",
        __str__=lambda self: "bob",
    )
    msg = SimpleNamespace(
        id=99, author=author, channel=channel, content="hello", mentions=[],
    )
    await bridge._on_message(msg)
    # The typing-indicator task is scheduled but may not have run yet —
    # let the event loop tick once so it gets a chance.
    await asyncio.sleep(0)
    channel_obj.trigger_typing.assert_awaited_once()
    assert len(enqueued) == 1


# ─── fetch_history ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_history_returns_oldest_first(bridge_with_fake_client):
    """discord-py's history yields newest-first; our fetch_history
    reverses to oldest-first so the agent reads in conversational
    order."""
    from datetime import datetime, timezone

    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]

    # Newest-first stream — what the discord library produces.
    base_ts = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    msgs = []
    for i in range(3):
        m = SimpleNamespace(
            id=1000 + i,
            content=f"msg {i}",
            created_at=base_ts.replace(minute=i),
            author=SimpleNamespace(
                id=42 + i, display_name=f"u{i}", global_name=f"u{i}",
                bot=False, __str__=lambda self: "u",
            ),
            attachments=[],
        )
        msgs.append(m)

    async def fake_history(limit, **kw):
        # Discord returns newest-first; emulate that.
        for m in reversed(msgs):
            yield m

    channel.history = fake_history
    out = await bridge.fetch_history("discord-1", limit=10)

    assert [m.content for m in out] == ["msg 0", "msg 1", "msg 2"]
    assert out[0].id == "1000"
    assert out[0].author_display == "u0"
    assert out[0].is_bot is False


@pytest.mark.asyncio
async def test_fetch_history_clamps_to_100(bridge_with_fake_client):
    """Caller-requested limits above Discord's 100-per-call ceiling
    are clamped silently — caller paginates via ``before`` if they want
    more."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]

    captured: dict = {}

    async def fake_history(limit, **kw):
        captured["limit"] = limit
        if False:
            yield  # never executes — empty generator with the right type

    channel.history = fake_history
    await bridge.fetch_history("discord-1", limit=500)
    assert captured["limit"] == 100


@pytest.mark.asyncio
async def test_fetch_history_passes_before_cursor(bridge_with_fake_client):
    """``before`` must be forwarded as a discord.Object id for
    pagination to work."""
    import discord

    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]

    captured: dict = {}

    async def fake_history(limit, before=None, **kw):
        captured["before"] = before
        if False:
            yield

    channel.history = fake_history
    await bridge.fetch_history("discord-1", limit=20, before="1234567890")
    assert captured["before"] is not None
    # ``before`` is a discord.Object whose id matches.
    assert getattr(captured["before"], "id", None) == 1234567890


@pytest.mark.asyncio
async def test_fetch_history_unconnected_returns_empty(tmp_path: Path):
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    out = await bridge.fetch_history("discord-1")
    assert out == []


@pytest.mark.asyncio
async def test_fetch_history_bad_channel_id_returns_empty(bridge_with_fake_client):
    bridge, _, _ = bridge_with_fake_client
    out = await bridge.fetch_history("not-a-discord-id")
    assert out == []
