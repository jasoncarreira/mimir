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
