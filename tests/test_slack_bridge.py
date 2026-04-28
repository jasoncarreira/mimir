"""SlackBridge — chunking, channel-id helpers, and the Bridge ABC contract
under a fake slack-bolt AsyncApp (SPEC §7.2.1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Skip the whole module if slack-bolt isn't installed in the test env.
pytest.importorskip("slack_bolt")

from mimir.bridges.slack import (
    SLACK_MESSAGE_CHAR_LIMIT,
    SlackBridge,
    _channel_id_to_slack,
    _chunk_message,
    _is_dm_channel,
    _normalize_emoji,
    _slack_channel_to_id,
)
from mimir.event_logger import init_logger
from mimir.models import AgentEvent


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


# ---- channel-id helpers --------------------------------------------------


def test_slack_channel_to_id_public():
    assert _slack_channel_to_id("C01ABCDEF") == "slack-C01ABCDEF"


def test_slack_channel_to_id_dm():
    """A DM channel routes through the dm-slack- prefix."""
    assert _slack_channel_to_id("D01XYZ123") == "dm-slack-D01XYZ123"


def test_channel_id_to_slack_round_trip():
    assert _channel_id_to_slack("slack-C01ABCDEF") == "C01ABCDEF"
    assert _channel_id_to_slack("dm-slack-D01XYZ") == "D01XYZ"
    assert _channel_id_to_slack("discord-foo") is None
    assert _channel_id_to_slack("not-a-prefix") is None


def test_channel_id_to_slack_dm_prefix_wins():
    """``dm-slack-`` must be matched before ``slack-`` — otherwise a DM id
    starting with literally ``dm-`` would get the wrong prefix stripped."""
    # ChannelRegistry sorts by descending prefix length, but the helper
    # is called directly here. Its iteration order in the helper is
    # ("dm-slack-", "slack-") so the longer prefix wins.
    assert _channel_id_to_slack("dm-slack-D-edge") == "D-edge"


def test_is_dm_channel():
    assert _is_dm_channel("D01XYZ") is True
    assert _is_dm_channel("C01ABC") is False


def test_normalize_emoji_strips_wrapping():
    assert _normalize_emoji(":thumbsup:") == "thumbsup"
    assert _normalize_emoji("thumbsup") == "thumbsup"
    assert _normalize_emoji("  raised_hands  ") == "raised_hands"
    assert _normalize_emoji("") == ""


# ---- chunking ------------------------------------------------------------


def test_chunk_short_returns_one():
    assert _chunk_message("hi") == ["hi"]


def test_chunk_respects_limit():
    text = "x" * (SLACK_MESSAGE_CHAR_LIMIT * 3)
    chunks = _chunk_message(text)
    assert all(len(c) <= SLACK_MESSAGE_CHAR_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_chunk_prefers_paragraph_boundaries():
    a = "alpha\nbeta\n"
    b = "gamma\ndelta"
    text = a + "\n" + b
    chunks = _chunk_message(text, limit=200)
    assert chunks == [text]


# ---- bridge surface ------------------------------------------------------


@pytest.fixture
def bridge_with_fake_app():
    """Build a SlackBridge with a fake AsyncApp + mock enqueue."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(e: AgentEvent) -> bool:
        enqueued.append(e)
        return True

    bridge = SlackBridge(
        bot_token="xoxb-TEST",
        app_token="xapp-TEST",
        enqueue=fake_enqueue,
    )

    sent: list[dict] = []

    async def fake_post(*, channel: str, text: str):
        sent.append({"channel": channel, "text": text})
        return {"ts": f"1234567890.{len(sent):06d}"}

    async def fake_reactions_add(*, channel: str, timestamp: str, name: str):
        sent.append({"reaction": name, "channel": channel, "ts": timestamp})
        return {"ok": True}

    fake_client = SimpleNamespace(
        chat_postMessage=fake_post,
        reactions_add=fake_reactions_add,
        auth_test=AsyncMock(return_value={"user_id": "UBOT123"}),
        files_upload_v2=AsyncMock(return_value={"file": {}}),
    )
    fake_app = SimpleNamespace(client=fake_client)
    bridge._app = fake_app  # type: ignore[assignment]
    bridge._bot_user_id = "UBOT123"
    return bridge, enqueued, sent


@pytest.mark.asyncio
async def test_send_chunks_long_text(bridge_with_fake_app):
    bridge, _, sent = bridge_with_fake_app
    long_text = "y" * (SLACK_MESSAGE_CHAR_LIMIT * 2 + 100)
    result = await bridge.send("slack-C01ABC", long_text)
    assert result.sent is True
    assert result.chunks == 3
    assert all(s.get("channel") == "C01ABC" for s in sent)


@pytest.mark.asyncio
async def test_send_to_dm_routes_to_d_channel(bridge_with_fake_app):
    bridge, _, sent = bridge_with_fake_app
    result = await bridge.send("dm-slack-D01XYZ", "hello DM")
    assert result.sent is True
    assert sent[0]["channel"] == "D01XYZ"
    assert sent[0]["text"] == "hello DM"


@pytest.mark.asyncio
async def test_send_rejects_unknown_channel_id_format(bridge_with_fake_app):
    bridge, _, sent = bridge_with_fake_app
    result = await bridge.send("not-a-slack-channel", "hi")
    assert result.sent is False
    assert "bad channel_id" in (result.error or "")
    assert sent == []


@pytest.mark.asyncio
async def test_send_returns_message_ts(bridge_with_fake_app):
    bridge, _, _ = bridge_with_fake_app
    result = await bridge.send("slack-C01ABC", "hi")
    assert result.sent is True
    assert result.message_id is not None
    assert "." in result.message_id  # Slack ts format: epoch.microseconds


@pytest.mark.asyncio
async def test_react_calls_reactions_add(bridge_with_fake_app):
    bridge, _, sent = bridge_with_fake_app
    ok = await bridge.react("slack-C01ABC", "1234567890.000001", ":thumbsup:")
    assert ok is True
    assert sent[0]["reaction"] == "thumbsup"
    assert sent[0]["channel"] == "C01ABC"


@pytest.mark.asyncio
async def test_react_rejects_unknown_channel_id(bridge_with_fake_app):
    bridge, _, _ = bridge_with_fake_app
    ok = await bridge.react("discord-foo", "1.0", "thumbsup")
    assert ok is False


@pytest.mark.asyncio
async def test_on_message_enqueues_user_message(bridge_with_fake_app):
    """A real-shape Slack message lands on the dispatcher with the right
    channel_id, source, and metadata."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "C01ENG",
            "text": "hello mimir",
            "ts": "1234567890.000001",
            "thread_ts": None,
        }
    )
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "slack-C01ENG"
    assert e.content == "hello mimir"
    assert e.source == "slack"
    assert e.source_id == "1234567890.000001"
    assert e.author_id == "U05ALICE"
    assert e.extra["channel_conversation_type"] == "multi_user"
    assert e.extra["channel_visibility"] == "public"


@pytest.mark.asyncio
async def test_on_message_dm_marks_private(bridge_with_fake_app):
    """A message in a D... channel surfaces as DM/private."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "D01ALICE",
            "text": "private",
            "ts": "1.000",
        }
    )
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "dm-slack-D01ALICE"
    assert e.extra["channel_conversation_type"] == "dm"
    assert e.extra["channel_visibility"] == "private"


@pytest.mark.asyncio
async def test_on_message_skips_self(bridge_with_fake_app):
    """Messages from the bot's own user id are dropped."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "UBOT123",  # matches _bot_user_id from the fixture
            "channel": "C01ENG",
            "text": "echo of own msg",
            "ts": "1.000",
        }
    )
    assert enqueued == []


@pytest.mark.asyncio
async def test_on_message_skips_subtype_channel_join(bridge_with_fake_app):
    """Subtype events (channel_join, message_changed, etc.) are dropped —
    the agent shouldn't react to system noise."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "C01ENG",
            "text": "@alice has joined",
            "ts": "1.000",
            "subtype": "channel_join",
        }
    )
    assert enqueued == []


@pytest.mark.asyncio
async def test_on_message_allows_file_share_subtype(bridge_with_fake_app):
    """``file_share`` subtype carries content + attachments — keep it."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "C01ENG",
            "text": "check this out",
            "ts": "1.000",
            "subtype": "file_share",
        }
    )
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_on_message_skips_bot_unless_opted_in(bridge_with_fake_app):
    """A non-self bot is dropped unless ``respond_to_bots=True``."""
    bridge, enqueued, _ = bridge_with_fake_app
    bridge.respond_to_bots = False
    await bridge._on_message(
        {
            "user": "UOTHERBOT",
            "channel": "C01ENG",
            "text": "hi from another bot",
            "ts": "1.000",
            "bot_id": "B12345",
        }
    )
    assert enqueued == []

    bridge.respond_to_bots = True
    await bridge._on_message(
        {
            "user": "UOTHERBOT",
            "channel": "C01ENG",
            "text": "hi from another bot",
            "ts": "1.000",
            "bot_id": "B12345",
        }
    )
    assert len(enqueued) == 1
    assert enqueued[0].author_id == "UOTHERBOT"


@pytest.mark.asyncio
async def test_send_reports_disconnected_app(tmp_path: Path):
    """An unconnected bridge fails send rather than crashing."""
    bridge = SlackBridge(
        bot_token="x",
        app_token="x",
        enqueue=AsyncMock(return_value=True),
    )
    result = await bridge.send("slack-C01ABC", "hi")
    assert result.sent is False
    assert "not connected" in (result.error or "")
