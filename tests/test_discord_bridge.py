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

from mimir.bridges.base import MessageUpdate
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

    sent: list[dict] = []
    edited: list[dict] = []
    deleted: list[dict] = []

    class FakeChannel:
        def __init__(self, cid: int):
            self.id = cid
            self.type = getattr(discord.ChannelType, "text", None)
            self.name = "test"
            self._next_msg_id = 1000

        def get_partial_message(self, mid: int):
            return SimpleNamespace(id=mid)

        async def send(self, content: str = "", files=None, **kwargs):
            sent.append(
                {
                    "channel_id": self.id,
                    "content": content,
                    "files": files or [],
                    **kwargs,
                }
            )
            self._next_msg_id += 1
            return SimpleNamespace(id=self._next_msg_id)

        async def fetch_message(self, mid: int):
            msg = SimpleNamespace(id=mid)
            msg.add_reaction = AsyncMock()

            async def fake_edit(**kwargs):
                edited.append({"channel_id": self.id, "message_id": mid, **kwargs})
                return SimpleNamespace(id=mid)

            msg.edit = fake_edit

            async def fake_delete():
                deleted.append({"channel_id": self.id, "message_id": mid})

            msg.delete = fake_delete
            return msg

    # The bridge duck-types the messageable check (callable .send), so the
    # SimpleNamespace-shaped FakeChannel is fine without an ABC dance.

    class FakeClient:
        user = SimpleNamespace(id=42, name="mimir-bot")

        def __init__(self):
            self._channels = {1: FakeChannel(1), 2: FakeChannel(2)}
            self._edits = edited
            self._deletes = deleted

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
    assert all(item["channel_id"] == 1 for item in sent)


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
async def test_send_closes_attachment_files_when_discord_send_fails(
    bridge_with_fake_client, tmp_path: Path,
):
    import discord

    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[union-attr]
    attachment_a = tmp_path / "a.txt"
    attachment_b = tmp_path / "b.txt"
    attachment_a.write_text("alpha")
    attachment_b.write_text("bravo")
    opened_files: list[discord.File] = []

    async def boom(content: str = "", files=None, **kwargs):
        del content, kwargs
        opened_files.extend(files or [])
        raise discord.DiscordException("missing permissions")

    channel.send = boom

    result = await bridge.send(
        "discord-1",
        "hello",
        attachment_paths=[attachment_a, attachment_b],
    )

    assert result.sent is False
    assert "discord send error" in (result.error or "")
    assert len(opened_files) == 2
    assert all(file.fp.closed for file in opened_files)


@pytest.mark.asyncio
async def test_send_passes_embed_and_reply_reference(bridge_with_fake_client):
    bridge, _, sent = bridge_with_fake_client

    result = await bridge.send(
        "discord-1",
        "",
        final=False,
        reply_to_message_id="999",
        embed={"title": "Working", "description": "[ ] Working", "color": 0x5865F2},
    )

    assert result.sent is True
    assert result.chunks == 1
    assert sent[0]["content"] == ""
    assert sent[0]["reference"].id == 999
    assert sent[0]["embed"].title == "Working"
    assert sent[0]["embed"].description == "[ ] Working"


@pytest.mark.asyncio
async def test_edit_message_calls_message_edit_with_embed(bridge_with_fake_client):
    bridge, _, _ = bridge_with_fake_client
    embed = SimpleNamespace(title="Activity")

    result = await bridge.edit_message("discord-1", "1001", MessageUpdate(text="updated", blocks=[{"type": "ignored"}], embed=embed))

    assert result.sent is True
    assert result.message_id == "1001"
    assert bridge._client._edits == [  # type: ignore[union-attr]
        {
            "channel_id": 1,
            "message_id": 1001,
            "content": "updated",
            "embed": embed,
        }
    ]


@pytest.mark.asyncio
async def test_edit_message_coerces_activity_panel_embed_dict(bridge_with_fake_client):
    bridge, _, _ = bridge_with_fake_client

    result = await bridge.edit_message(
        "discord-1",
        "1001",
        MessageUpdate(
            text="",
            embed={"title": "Done", "description": "Done 1 steps", "color": 0x2ECC71},
        ),
    )

    assert result.sent is True
    edited = bridge._client._edits[0]  # type: ignore[union-attr]
    assert edited["content"] == ""
    assert edited["embed"].title == "Done"
    assert edited["embed"].description == "Done 1 steps"


@pytest.mark.asyncio
async def test_edit_message_discord_error_is_soft(bridge_with_fake_client):
    import discord

    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[union-attr]

    async def boom(mid: int):
        del mid
        raise discord.DiscordException("message deleted")

    channel.fetch_message = boom

    result = await bridge.edit_message("discord-1", "1001", MessageUpdate(text="updated"))
    assert result.sent is False
    assert result.message_id == "1001"
    assert "discord edit error" in (result.error or "")


@pytest.mark.asyncio
async def test_delete_message_calls_message_delete(bridge_with_fake_client):
    bridge, _, _ = bridge_with_fake_client

    result = await bridge.delete_message("discord-1", "1001")

    assert result.sent is True
    assert result.message_id == "1001"
    assert bridge._client._deletes == [  # type: ignore[union-attr]
        {"channel_id": 1, "message_id": 1001}
    ]


@pytest.mark.asyncio
async def test_delete_message_discord_error_is_soft(bridge_with_fake_client):
    import discord

    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[union-attr]

    async def boom(mid: int):
        del mid
        raise discord.DiscordException("message deleted")

    channel.fetch_message = boom

    result = await bridge.delete_message("discord-1", "1001")
    assert result.sent is False
    assert result.message_id == "1001"
    assert "discord delete error" in (result.error or "")


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
async def test_on_message_dedupes_resume_protocol_redelivery(
    bridge_with_fake_client,
):
    """chainlink #232: Discord's resume protocol can redeliver around
    disconnects. The bridge must enqueue exactly once for the same
    message id, no matter how many times it arrives."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    channel = SimpleNamespace(
        id=1, type=getattr(discord.ChannelType, "text", None), name="g"
    )
    author = SimpleNamespace(id=99, bot=False, display_name="Alice")
    msg = SimpleNamespace(
        id=12345, author=author, channel=channel, content="hello", mentions=[]
    )
    await bridge._on_message(msg)
    await bridge._on_message(msg)  # simulated resume redelivery
    await bridge._on_message(msg)  # and again
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_on_message_redelivery_after_rejected_enqueue_is_accepted(
    bridge_with_fake_client,
):
    """Queue-full rejection must not commit the message id as seen.

    The platform can redeliver the same source id after the dispatcher rejects
    admission; that retry must still reach enqueue instead of being deduped.
    """
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    attempts: list[AgentEvent] = []

    async def reject_once_then_accept(event: AgentEvent) -> bool:
        attempts.append(event)
        if len(attempts) == 1:
            return False
        enqueued.append(event)
        return True

    bridge.enqueue = reject_once_then_accept
    channel = SimpleNamespace(
        id=1, type=getattr(discord.ChannelType, "text", None), name="g"
    )
    author = SimpleNamespace(id=99, bot=False, display_name="Alice")
    msg = SimpleNamespace(
        id=12345, author=author, channel=channel, content="hello", mentions=[]
    )

    await bridge._on_message(msg)
    assert len(attempts) == 1
    assert "12345" not in bridge._seen_ids

    await bridge._on_message(msg)
    assert len(attempts) == 2
    assert len(enqueued) == 1
    assert enqueued[0].source_id == "12345"
    assert "12345" in bridge._seen_ids

    await bridge._on_message(msg)
    assert len(attempts) == 2
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_on_message_does_not_dedupe_distinct_ids(
    bridge_with_fake_client,
):
    """Distinct source_ids must each produce an enqueue — guards against
    a regression where the dedup short-circuits all messages."""
    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    channel = SimpleNamespace(
        id=1, type=getattr(discord.ChannelType, "text", None), name="g"
    )
    author = SimpleNamespace(id=99, bot=False, display_name="Alice")
    for mid in (1, 2, 3):
        msg = SimpleNamespace(
            id=mid, author=author, channel=channel, content=f"m{mid}", mentions=[]
        )
        await bridge._on_message(msg)
    assert len(enqueued) == 3
    assert [e.source_id for e in enqueued] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_send_reports_disconnected_client(tmp_path: Path):
    """An unconnected bridge fails send rather than crashing."""
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    result = await bridge.send("discord-1", "hi")
    assert result.sent is False
    assert "not connected" in (result.error or "")


class _FakeTyping:
    """Stand-in for discord.py's ``Typing`` async context manager.
    Records aenter/aexit on the fake channel for test assertions."""
    def __init__(self, channel):
        self.channel = channel
    async def __aenter__(self):
        self.channel.typing_aenter_calls = (
            getattr(self.channel, "typing_aenter_calls", 0) + 1
        )
        return self
    async def __aexit__(self, *exc):
        self.channel.typing_aexit_calls = (
            getattr(self.channel, "typing_aexit_calls", 0) + 1
        )
        return None


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.01):
    """Loop until ``predicate()`` is truthy or ``timeout`` elapses. Used
    to poll the typing-hold task's state without sleep-then-assert
    races (the task spawns asynchronously)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.mark.asyncio
async def test_send_typing_indicator_spawns_hold_task(bridge_with_fake_client):
    """send_typing_indicator now holds the typing context open via a
    background task, not a one-shot enter/exit. The task aenters the
    typing context and stays inside until cancelled — discord.py's own
    auto-refresh keeps the indicator alive while the body runs."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel.typing = lambda: _FakeTyping(channel)
    await bridge.send_typing_indicator("discord-1")

    # Task is registered and alive.
    assert "discord-1" in bridge._typing_tasks
    task = bridge._typing_tasks["discord-1"]
    assert not task.done()
    # Wait for the hold body to enter the context.
    assert await _wait_for(lambda: getattr(channel, "typing_aenter_calls", 0) >= 1)
    # It hasn't exited yet — that's the whole point.
    assert getattr(channel, "typing_aexit_calls", 0) == 0

    # Cleanup so the test doesn't leak a task into the loop.
    await bridge.cancel_typing("discord-1")
    assert await _wait_for(lambda: task.done())


@pytest.mark.asyncio
async def test_send_cancels_typing_for_destination_channel(
    bridge_with_fake_client,
):
    """send() cancels the typing-hold task for the destination channel
    before the actual reply lands. The aexit on the typing context
    runs once cancellation propagates."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel.typing = lambda: _FakeTyping(channel)
    await bridge.send_typing_indicator("discord-1")
    assert await _wait_for(lambda: getattr(channel, "typing_aenter_calls", 0) >= 1)

    result = await bridge.send("discord-1", "ok")
    assert result.sent is True
    # Task should be removed from the registry on send.
    assert "discord-1" not in bridge._typing_tasks
    # Wait for aexit to land — task cancellation runs the context's __aexit__.
    assert await _wait_for(lambda: getattr(channel, "typing_aexit_calls", 0) >= 1)


@pytest.mark.asyncio
async def test_repeat_send_typing_replaces_prior_task(bridge_with_fake_client):
    """Two send_typing_indicator calls in a row: first task is
    cancelled, second is alive. Most-recent-inbound wins."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel.typing = lambda: _FakeTyping(channel)

    await bridge.send_typing_indicator("discord-1")
    first = bridge._typing_tasks["discord-1"]

    await bridge.send_typing_indicator("discord-1")
    second = bridge._typing_tasks["discord-1"]

    assert first is not second
    assert await _wait_for(lambda: first.done())
    assert not second.done()

    await bridge.cancel_typing("discord-1")


@pytest.mark.asyncio
async def test_send_to_channel_b_does_not_cancel_typing_on_channel_a(
    bridge_with_fake_client,
):
    """Per-channel isolation: a send to channel B must leave channel A's
    typing-hold task alone. (Without this guard the test would
    regress the cross-channel edge case the spec calls out.)"""
    bridge, _, _ = bridge_with_fake_client
    channel_a = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel_b = bridge._client._channels[2]  # type: ignore[attr-defined]
    channel_a.typing = lambda: _FakeTyping(channel_a)
    channel_b.typing = lambda: _FakeTyping(channel_b)

    await bridge.send_typing_indicator("discord-1")
    task_a = bridge._typing_tasks["discord-1"]
    assert await _wait_for(lambda: getattr(channel_a, "typing_aenter_calls", 0) >= 1)

    await bridge.send("discord-2", "hi over here")
    # Channel A's typing task still alive — only channel B was affected.
    assert "discord-1" in bridge._typing_tasks
    assert not task_a.done()
    assert getattr(channel_a, "typing_aexit_calls", 0) == 0

    await bridge.cancel_typing("discord-1")


@pytest.mark.asyncio
async def test_cancel_typing_when_no_task_is_safe(bridge_with_fake_client):
    """cancel_typing() on a channel that never had a typing task is a
    no-op — programmatic sends and scheduled-tick replies hit this
    path. Must not raise."""
    bridge, _, _ = bridge_with_fake_client
    await bridge.cancel_typing("discord-1")  # never had a task
    assert "discord-1" not in bridge._typing_tasks


@pytest.mark.asyncio
async def test_send_without_prior_typing_task_is_safe(bridge_with_fake_client):
    """A send() to a channel with no in-flight typing task works
    normally — the cancel_typing() call inside send() is a no-op."""
    bridge, _, sent = bridge_with_fake_client
    result = await bridge.send("discord-2", "scheduled tick reply")
    assert result.sent is True
    assert sent  # actually delivered


@pytest.mark.asyncio
async def test_typing_hold_capped_at_timeout(bridge_with_fake_client):
    """When neither send() nor cancel_typing() arrives, the hold task
    exits naturally at the hard cap — defense against errored turns
    that would otherwise leak a typing task forever."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel.typing = lambda: _FakeTyping(channel)
    # Shrink the cap so the test runs in a sane wall-clock window.
    bridge._TYPING_HOLD_TIMEOUT_SECONDS = 0.05  # 50ms

    await bridge.send_typing_indicator("discord-1")
    task = bridge._typing_tasks["discord-1"]
    # Task should exit on its own once the inner asyncio.sleep wakes.
    assert await _wait_for(lambda: task.done(), timeout=1.0)
    # And aexit has run.
    assert getattr(channel, "typing_aexit_calls", 0) >= 1


@pytest.mark.asyncio
async def test_send_typing_indicator_swallows_failures(bridge_with_fake_client):
    """Errors during typing don't propagate — best-effort. Even when
    the typing context aenter raises, the bridge should not bubble
    the exception."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]

    class _BoomTyping:
        async def __aenter__(self):
            raise RuntimeError("rate-limited")
        async def __aexit__(self, *exc):
            return None

    channel.typing = lambda: _BoomTyping()
    # Should not raise.
    await bridge.send_typing_indicator("discord-1")
    task = bridge._typing_tasks["discord-1"]
    # Task exits cleanly (the exception is swallowed inside _hold).
    assert await _wait_for(lambda: task.done())


@pytest.mark.asyncio
async def test_send_typing_indicator_unconnected_noops(tmp_path: Path):
    """No client → silent no-op, no exception, no dangling task."""
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    await bridge.send_typing_indicator("discord-1")  # must not raise
    assert "discord-1" not in bridge._typing_tasks


@pytest.mark.asyncio
async def test_disconnect_cancels_dangling_typing_tasks(bridge_with_fake_client):
    """Bridge disconnect must cancel any in-flight typing-hold tasks so
    they don't try to POST against a closing client."""
    bridge, _, _ = bridge_with_fake_client
    channel = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel.typing = lambda: _FakeTyping(channel)
    await bridge.send_typing_indicator("discord-1")
    task = bridge._typing_tasks["discord-1"]
    assert not task.done()

    await bridge.disconnect()
    # All tasks cancelled and dict cleared.
    assert bridge._typing_tasks == {}
    assert await _wait_for(lambda: task.done())


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
    fires it as a background task so enqueue isn't blocked — verify the
    ``channel.typing()`` context manager got entered."""
    import asyncio

    import discord

    bridge, enqueued, _ = bridge_with_fake_client
    channel_obj = bridge._client._channels[1]  # type: ignore[attr-defined]
    channel_obj.typing = lambda: _FakeTyping(channel_obj)

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
    # let the event loop tick a few times so the asyncio.create_task
    # gets dispatched and the context manager runs to completion.
    for _ in range(5):
        await asyncio.sleep(0)
    assert getattr(channel_obj, "typing_aenter_calls", 0) >= 1
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


# ---- supervisor: retry-with-backoff around client.start() ---------------


@pytest.mark.asyncio
async def test_discord_connect_retains_runner_task(monkeypatch):
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_start(token):
        started.set()
        await release.wait()

    fake_client = SimpleNamespace(
        start=fake_start,
        close=AsyncMock(),
        is_closed=lambda: False,
    )
    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient", lambda owner: fake_client,
    )

    await bridge.connect()
    assert bridge._runner in bridge._background_tasks
    await asyncio.wait_for(started.wait(), timeout=1.0)

    release.set()
    await asyncio.wait_for(bridge._runner, timeout=1.0)
    await asyncio.sleep(0)
    assert bridge._runner not in bridge._background_tasks


@pytest.mark.asyncio
async def test_supervisor_retries_on_transient_5xx(monkeypatch, tmp_path: Path):
    """A 503 at token-auth (the actual production failure mode that
    motivated this) gets retried with backoff, not a one-shot kill.
    First attempt 503s; second succeeds; supervisor exits cleanly."""
    import discord
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    # Tighten backoffs for the test so it doesn't hang the suite.
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.05

    attempts = {"n": 0}

    async def fake_start(token):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Fake DiscordServerError shape — discord.py wraps a response
            # object, but for the supervisor we only care about the type.
            raise discord.DiscordServerError(
                _FakeResp(status=503), {"code": 0, "message": "no healthy upstream"}
            )
        # Second attempt: clean exit.
        return None

    async def fake_close():
        pass

    async def fake_is_closed():
        return False

    # Patch BEFORE connect so the first ``_DiscordClient`` we build has
    # the fake start. The supervisor builds a fresh client between
    # attempts; both need the same patched class.
    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient",
        lambda owner: SimpleNamespace(
            start=fake_start, close=fake_close, is_closed=lambda: False,
        ),
    )

    await bridge.connect()
    # Wait for the supervisor task to finish — the second attempt
    # should return cleanly.
    await asyncio.wait_for(bridge._runner, timeout=2.0)
    assert attempts["n"] == 2  # one failure, one success


@pytest.mark.asyncio
async def test_supervisor_does_not_retry_on_login_failure(monkeypatch, tmp_path: Path):
    """``LoginFailure`` is operator-actionable (bad token). Retrying
    just spams. The supervisor must propagate it and the task must die."""
    import discord
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start(token):
        attempts["n"] += 1
        raise discord.LoginFailure("invalid token")

    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient",
        lambda owner: SimpleNamespace(
            start=fake_start, close=AsyncMock(), is_closed=lambda: False,
        ),
    )

    await bridge.connect()
    # The runner task should fail with LoginFailure.
    with pytest.raises(discord.LoginFailure):
        await asyncio.wait_for(bridge._runner, timeout=1.0)
    # Only one attempt — no retries on operator-actionable errors.
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_supervisor_caps_backoff(monkeypatch, tmp_path: Path):
    """Backoff doubles per attempt but caps at the configured ceiling.
    Pin behavior so a regression doesn't introduce unbounded growth."""
    import discord
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.04

    sleeps: list[float] = []

    real_sleep = asyncio.sleep

    async def capturing_sleep(delay, *a, **kw):
        sleeps.append(delay)
        await real_sleep(0)  # yield without actually sleeping

    monkeypatch.setattr(asyncio, "sleep", capturing_sleep)

    attempts = {"n": 0}

    async def fake_start(token):
        attempts["n"] += 1
        if attempts["n"] >= 5:
            return None  # let supervisor exit after a few retries
        raise discord.DiscordServerError(
            _FakeResp(status=503), {"code": 0, "message": "boom"},
        )

    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient",
        lambda owner: SimpleNamespace(
            start=fake_start, close=AsyncMock(), is_closed=lambda: False,
        ),
    )

    await bridge.connect()
    await asyncio.wait_for(bridge._runner, timeout=2.0)
    # First sleep is the initial 0.01; subsequent doublings 0.02, 0.04 (cap), 0.04.
    assert sleeps[0] == pytest.approx(0.01)
    # After 4 retries (5th attempt succeeds), the last sleep we recorded
    # before that should be ≤ cap. None should exceed cap.
    assert all(s <= 0.04 + 1e-9 for s in sleeps)


class _FakeResp:
    """Minimal aiohttp-Response stand-in for discord.DiscordServerError
    construction. The real class only reads ``.status`` from the response
    on init."""
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Service Unavailable"


@pytest.mark.asyncio
async def test_supervisor_fires_algedonic_after_three_attempts(monkeypatch, tmp_path: Path):
    """``discord_bridge_retry`` event should fire only after attempts >= 3
    so a one-off transient doesn't spam the algedonic block. Pinned via
    a captured event log."""
    import discord
    from mimir import event_logger

    captured: list[tuple[str, dict]] = []

    async def fake_log_event(kind: str, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.bridges.discord._safe_log_event", fake_log_event)

    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.001
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start(token):
        attempts["n"] += 1
        if attempts["n"] >= 5:
            return None
        raise discord.DiscordServerError(
            _FakeResp(status=503), {"code": 0, "message": "boom"},
        )

    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient",
        lambda owner: SimpleNamespace(
            start=fake_start, close=AsyncMock(), is_closed=lambda: False,
        ),
    )

    await bridge.connect()
    await asyncio.wait_for(bridge._runner, timeout=2.0)
    # Wait briefly for any pending log-event tasks to drain.
    for _ in range(20):
        await asyncio.sleep(0)

    retry_events = [(k, f) for k, f in captured if k == "discord_bridge_retry"]
    # 4 failed attempts before success (5th); algedonic fires on attempts 3, 4.
    assert len(retry_events) == 2
    assert retry_events[0][1]["attempt"] == 3
    assert retry_events[1][1]["attempt"] == 4


@pytest.mark.asyncio
async def test_supervisor_clean_exit_when_client_returns(monkeypatch, tmp_path: Path):
    """If ``client.start()`` returns cleanly (operator initiated
    disconnect via ``client.close()``), the supervisor exits the loop
    rather than retrying. Otherwise normal shutdown would re-spawn."""
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start(token):
        attempts["n"] += 1
        return None  # immediate clean exit

    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient",
        lambda owner: SimpleNamespace(
            start=fake_start, close=AsyncMock(), is_closed=lambda: False,
        ),
    )

    await bridge.connect()
    await asyncio.wait_for(bridge._runner, timeout=1.0)
    assert attempts["n"] == 1  # no retries on clean exit


@pytest.mark.asyncio
async def test_disconnect_cancels_supervisor_cleanly(monkeypatch, tmp_path: Path):
    """``disconnect()`` during a backoff sleep must cancel the supervisor
    cleanly without re-raising CancelledError into the caller."""
    import discord
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 5.0  # long enough to interrupt mid-sleep

    async def fake_start(token):
        raise discord.DiscordServerError(
            _FakeResp(status=503), {"code": 0, "message": "boom"},
        )

    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient",
        lambda owner: SimpleNamespace(
            start=fake_start, close=AsyncMock(), is_closed=lambda: False,
        ),
    )

    await bridge.connect()
    # Let the supervisor hit its first failure + start the backoff sleep.
    await asyncio.sleep(0.05)
    # Now disconnect — should cancel the supervisor task, not raise.
    await bridge.disconnect()
    assert bridge._runner is None


@pytest.mark.asyncio
async def test_supervisor_closes_old_client_before_constructing_new(monkeypatch, tmp_path: Path):
    """Between retry attempts, the previous client is closed before the
    next one is constructed. Without this the previous client could leak
    file descriptors / WebSocket sessions on a long-running outage."""
    import discord
    bridge = DiscordBridge(token="x", enqueue=AsyncMock(return_value=True))
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.001
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.01

    construct_calls: list[int] = []
    close_calls: list[int] = []

    def make_fake_client(owner):
        instance_id = len(construct_calls)
        construct_calls.append(instance_id)

        async def fake_close():
            close_calls.append(instance_id)

        async def fake_start(token):
            if instance_id < 2:
                raise discord.DiscordServerError(
                    _FakeResp(status=503), {"code": 0, "message": "x"},
                )
            return None

        return SimpleNamespace(
            start=fake_start, close=fake_close, is_closed=lambda: False,
        )

    monkeypatch.setattr(
        "mimir.bridges.discord._DiscordClient", make_fake_client,
    )

    await bridge.connect()
    await asyncio.wait_for(bridge._runner, timeout=2.0)

    # Three constructions: initial + 2 retries (2 failures + 1 success).
    assert construct_calls == [0, 1, 2]
    # Two closes: between 0→1 and 1→2 (no close after the successful
    # start exits the loop).
    assert close_calls == [0, 1]


def test_should_emit_retry_algedonic_throttling():
    """The throttle helper fires every attempt 3-9 (early-warning),
    then every 10th thereafter (sustained-outage cap)."""
    from mimir.bridges.discord import _should_emit_retry_algedonic
    # Pre-threshold: silent.
    assert _should_emit_retry_algedonic(1) is False
    assert _should_emit_retry_algedonic(2) is False
    # Early-warning band: every attempt fires.
    for n in range(3, 10):
        assert _should_emit_retry_algedonic(n) is True, n
    # Sustained-outage band: every 10th only.
    assert _should_emit_retry_algedonic(10) is True
    assert _should_emit_retry_algedonic(11) is False
    assert _should_emit_retry_algedonic(15) is False
    assert _should_emit_retry_algedonic(19) is False
    assert _should_emit_retry_algedonic(20) is True
    assert _should_emit_retry_algedonic(100) is True
    assert _should_emit_retry_algedonic(101) is False


def test_fatal_discord_exceptions_built_correctly():
    """Module-level fatal-exception map should include LoginFailure and
    PrivilegedIntentsRequired. Validates the ``getattr`` defensiveness
    didn't accidentally include ``Exception`` (the prior bug)."""
    import discord
    from mimir.bridges.discord import (
        _FATAL_DISCORD_EXCEPTIONS,
        _FATAL_DISCORD_EXCEPTION_INFO,
    )
    # Both classes should be present in current discord-py.
    assert discord.LoginFailure in _FATAL_DISCORD_EXCEPTIONS
    assert discord.PrivilegedIntentsRequired in _FATAL_DISCORD_EXCEPTIONS
    # And — critically — the catch-all ``Exception`` is NOT in the
    # tuple. If it were, the supervisor would propagate every
    # transient as if it were operator-actionable.
    assert Exception not in _FATAL_DISCORD_EXCEPTIONS
    assert BaseException not in _FATAL_DISCORD_EXCEPTIONS
    # The info map should have a (event_kind, log_msg) tuple per class.
    assert _FATAL_DISCORD_EXCEPTION_INFO[discord.LoginFailure][0] == "discord_bridge_login_failure"
    assert _FATAL_DISCORD_EXCEPTION_INFO[discord.PrivilegedIntentsRequired][0] == "discord_bridge_intents_failure"


@pytest.mark.asyncio
async def test_react_resolves_alias_to_glyph(bridge_with_fake_client):
    """chainlink #412: the prompt documents alias-name acks (``thumbsup`` /
    ``:thumbsup:``), but Discord's API only accepts unicode glyphs — the
    resolver existed (ee0e9b9) with no caller, so alias reacts 400'd. The
    bridge now resolves aliases before add_reaction; raw glyphs and custom
    emoji literals pass through untouched."""
    bridge, _, _ = bridge_with_fake_client
    seen: list[str] = []
    channel = bridge._client._channels[1]

    async def fetch_message(mid):
        msg = SimpleNamespace(id=mid)

        async def add_reaction(emoji):
            seen.append(emoji)
        msg.add_reaction = add_reaction
        return msg

    channel.fetch_message = fetch_message

    assert await bridge.react("discord-1", "1000", "thumbsup") is True
    assert await bridge.react("discord-1", "1000", ":rocket:") is True
    assert await bridge.react("discord-1", "1000", "👀") is True
    assert await bridge.react("discord-1", "1000", "<:custom:12345>") is True
    assert seen == ["👍", "\U0001F680", "👀", "<:custom:12345>"]
