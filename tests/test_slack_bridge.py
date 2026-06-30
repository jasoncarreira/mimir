"""SlackBridge — chunking, channel-id helpers, and the Bridge ABC contract
under a fake slack-bolt AsyncApp (SPEC §7.2.1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Skip the whole module if slack-bolt isn't installed in the test env.
pytest.importorskip("slack_bolt")

from mimir.bridges.base import Bridge, MessageUpdate, SendResult
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


def test_is_dm_channel_mpim_via_channel_type():
    """Multi-party DMs (MPIMs) have G-prefix IDs but are conversation-shaped
    DMs — channel_type='mpim' is the authoritative signal."""
    # MPIM via the channel_type signal — must be DM.
    assert _is_dm_channel("G09ABCDEF", "mpim") is True
    # Legacy private channel (group) shares the G prefix but is org-shaped.
    assert _is_dm_channel("G09ABCDEF", "group") is False
    # 1:1 DM via signal.
    assert _is_dm_channel("D01XYZ", "im") is True
    # Public channel.
    assert _is_dm_channel("C01ABC", "channel") is False


def test_slack_channel_to_id_mpim():
    """MPIMs (group DMs) route through dm-slack- so the privacy filter
    treats them as DMs even though the Slack channel id starts with G."""
    assert _slack_channel_to_id("G09GROUP1", "mpim") == "dm-slack-G09GROUP1"
    # Without channel_type, G-prefix is ambiguous so we don't assume DM.
    assert _slack_channel_to_id("G09GROUP1") == "slack-G09GROUP1"
    # Private channel (group) stays non-DM.
    assert _slack_channel_to_id("G09GROUP1", "group") == "slack-G09GROUP1"


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
    updated: list[dict] = []
    deleted: list[dict] = []
    users_info_calls: list[str] = []

    async def fake_post(**kwargs):
        sent.append(kwargs)
        return {"ts": f"1234567890.{len(sent):06d}"}

    async def fake_reactions_add(*, channel: str, timestamp: str, name: str):
        sent.append({"reaction": name, "channel": channel, "ts": timestamp})
        return {"ok": True}

    async def fake_chat_update(**kwargs):
        updated.append(kwargs)
        return {"ok": True, "ts": kwargs["ts"]}

    async def fake_chat_delete(**kwargs):
        deleted.append(kwargs)
        return {"ok": True}

    # Plausible users.info response shape — tests that want a different
    # response can override `fake_client.users_info`.
    _profiles: dict[str, dict] = {
        "U05ALICE": {
            "user": {
                "id": "U05ALICE",
                "name": "alice",
                "real_name": "Alice Smith",
                "profile": {
                    "display_name": "Alice",
                    "email": "alice@example.com",
                },
            }
        },
    }

    async def fake_users_info(*, user: str):
        users_info_calls.append(user)
        if user not in _profiles:
            # Mimic a generic resolved-but-thin profile when an unknown
            # user shows up — Slack always returns *some* user object.
            return {"user": {"id": user, "name": user, "profile": {}}}
        return _profiles[user]

    fake_client = SimpleNamespace(
        chat_postMessage=fake_post,
        chat_update=fake_chat_update,
        chat_delete=fake_chat_delete,
        reactions_add=fake_reactions_add,
        auth_test=AsyncMock(return_value={"user_id": "UBOT123"}),
        files_upload_v2=AsyncMock(return_value={"file": {}}),
        users_info=fake_users_info,
    )
    fake_app = SimpleNamespace(
        client=fake_client,
        _users_info_calls=users_info_calls,
        _updates=updated,
        _deletes=deleted,
    )
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
async def test_send_can_post_threaded_block_kit_panel(bridge_with_fake_app):
    bridge, _, sent = bridge_with_fake_app
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Working*"}}]

    result = await bridge.send(
        "slack-C01ABC",
        "Working",
        final=False,
        reply_to_message_id="111.222",
        blocks=blocks,
    )

    assert result.sent is True
    assert sent[0]["channel"] == "C01ABC"
    assert sent[0]["thread_ts"] == "111.222"
    assert sent[0]["blocks"] == blocks


@pytest.mark.asyncio
async def test_bridge_default_edit_message_is_soft_noop():
    class _NoEditBridge(Bridge):
        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def send(
            self,
            channel_id: str,
            text: str,
            attachment_paths: list[Path] | None = None,
            *,
            final: bool = True,
        ) -> SendResult:
            del channel_id, text, attachment_paths, final
            return SendResult(sent=True, message_id="m1", chunks=1)

        async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
            del channel_id, message_id, emoji
            return False

    result = await _NoEditBridge().edit_message("c1", "m1", MessageUpdate(text="updated"))
    assert result.sent is False
    assert result.error == "edit unsupported"

    result = await _NoEditBridge().delete_message("c1", "m1")
    assert result.sent is False
    assert result.error == "delete unsupported"


@pytest.mark.asyncio
async def test_edit_message_calls_chat_update_with_blocks(bridge_with_fake_app):
    bridge, _, _ = bridge_with_fake_app
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]

    result = await bridge.edit_message(
        "slack-C01ABC",
        "1234567890.000001",
        MessageUpdate(text="updated", blocks=blocks, embed=object()),
    )

    assert result.sent is True
    assert result.message_id == "1234567890.000001"
    assert bridge._app._updates == [  # type: ignore[union-attr]
        {
            "channel": "C01ABC",
            "ts": "1234567890.000001",
            "text": "updated",
            "blocks": blocks,
        }
    ]


@pytest.mark.asyncio
async def test_edit_message_slack_error_is_soft(bridge_with_fake_app):
    from slack_sdk.errors import SlackApiError

    bridge, _, _ = bridge_with_fake_app

    async def boom(**kwargs):
        del kwargs
        raise SlackApiError(
            "message_not_found",
            response={"ok": False, "error": "message_not_found"},
        )

    bridge._app.client.chat_update = boom  # type: ignore[union-attr]

    result = await bridge.edit_message("slack-C01ABC", "1234567890.000001", MessageUpdate(text="updated"))
    assert result.sent is False
    assert result.message_id == "1234567890.000001"
    assert "slack edit error" in (result.error or "")


@pytest.mark.asyncio
async def test_delete_message_calls_chat_delete(bridge_with_fake_app):
    bridge, _, _ = bridge_with_fake_app

    result = await bridge.delete_message("slack-C01ABC", "1234567890.000001")

    assert result.sent is True
    assert result.message_id == "1234567890.000001"
    assert bridge._app._deletes == [  # type: ignore[union-attr]
        {"channel": "C01ABC", "ts": "1234567890.000001"}
    ]


@pytest.mark.asyncio
async def test_delete_message_slack_error_is_soft(bridge_with_fake_app):
    from slack_sdk.errors import SlackApiError

    bridge, _, _ = bridge_with_fake_app

    async def boom(**kwargs):
        del kwargs
        raise SlackApiError(
            "message_not_found",
            response={"ok": False, "error": "message_not_found"},
        )

    bridge._app.client.chat_delete = boom  # type: ignore[union-attr]

    result = await bridge.delete_message("slack-C01ABC", "1234567890.000001")
    assert result.sent is False
    assert result.message_id == "1234567890.000001"
    assert "slack delete error" in (result.error or "")


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
    # FUTURE_WORK §6.1: author is the platform-prefixed matching key.
    assert e.author == "slack-U05ALICE"
    # users.info enrichment: real_name takes precedence over user_id.
    assert e.author_display == "Alice Smith"
    # Email is captured into extra for any future use (identity proposal,
    # EmailBridge cross-reference).
    assert e.extra["slack_email"] == "alice@example.com"
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
            "channel_type": "im",
        }
    )
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "dm-slack-D01ALICE"
    assert e.extra["channel_conversation_type"] == "dm"
    assert e.extra["channel_visibility"] == "private"


@pytest.mark.asyncio
async def test_on_message_mpim_marks_private(bridge_with_fake_app):
    """A multi-party DM (MPIM) has a G-prefix id but channel_type='mpim'.
    Must route to dm-slack- so the §5.4 privacy filter treats it as a DM."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "G09GROUPDM",
            "text": "shh",
            "ts": "1.000",
            "channel_type": "mpim",
        }
    )
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "dm-slack-G09GROUPDM"
    assert e.extra["channel_conversation_type"] == "dm"
    assert e.extra["channel_visibility"] == "private"


@pytest.mark.asyncio
async def test_on_message_private_channel_not_dm(bridge_with_fake_app):
    """A legacy private channel (channel_type='group') has a G-prefix id
    but is org-shaped — keep the plain slack- prefix, not dm-slack-."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "G09PRIVCHAN",
            "text": "in private channel",
            "ts": "1.000",
            "channel_type": "group",
        }
    )
    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.channel_id == "slack-G09PRIVCHAN"
    assert e.extra["channel_conversation_type"] == "multi_user"
    assert e.extra["channel_visibility"] == "public"


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
async def test_on_message_dedupes_socket_mode_redelivery(bridge_with_fake_app):
    """chainlink #232: Socket Mode is documented to redeliver events on
    ACK loss. The bridge must enqueue exactly once for the same ts,
    no matter how many times Slack redelivers."""
    bridge, enqueued, _ = bridge_with_fake_app
    event = {
        "user": "U05ALICE",
        "channel": "C01ENG",
        "channel_type": "channel",
        "text": "hello mimir",
        "ts": "1234567890.000042",
    }
    await bridge._on_message(event)
    await bridge._on_message(event)  # simulated redelivery
    await bridge._on_message(event)  # and again
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_on_message_does_not_dedupe_distinct_ts(bridge_with_fake_app):
    """Distinct ``ts`` values must each enqueue — guards against the
    dedup cache short-circuiting all messages."""
    bridge, enqueued, _ = bridge_with_fake_app
    for ts in ("1.001", "1.002", "1.003"):
        await bridge._on_message(
            {
                "user": "U05ALICE",
                "channel": "C01ENG",
                "channel_type": "channel",
                "text": f"msg {ts}",
                "ts": ts,
            }
        )
    assert len(enqueued) == 3
    assert [e.source_id for e in enqueued] == ["1.001", "1.002", "1.003"]


@pytest.mark.asyncio
async def test_users_info_cached_per_user(bridge_with_fake_app):
    """Repeated messages from the same user hit users.info exactly once —
    the second message resolves from the in-memory cache."""
    bridge, enqueued, _ = bridge_with_fake_app
    # Distinct ts values per message — chainlink #232 dedup keys on ts,
    # so reusing the same value would collapse to a single enqueue.
    for i in range(3):
        await bridge._on_message(
            {
                "user": "U05ALICE",
                "channel": "C01ENG",
                "text": "hello",
                "ts": f"1.{i:03d}",
                "channel_type": "channel",
            }
        )
    assert len(enqueued) == 3
    # All three messages used the enriched display name.
    assert all(e.author_display == "Alice Smith" for e in enqueued)
    # users.info called once total (cached after the first lookup).
    assert bridge._app._users_info_calls == ["U05ALICE"]


@pytest.mark.asyncio
async def test_users_info_failure_falls_back_to_user_id(bridge_with_fake_app):
    """If users.info raises (e.g., users:read scope missing), the bridge
    falls back to user_id for display and leaves email as None. Failures
    are NOT cached — next message re-tries (so the bridge self-heals if
    the operator adds the scope post-deploy)."""
    from slack_sdk.errors import SlackApiError

    bridge, enqueued, _ = bridge_with_fake_app
    call_count = 0

    async def boom(*, user: str):
        nonlocal call_count
        call_count += 1
        raise SlackApiError("missing_scope", response={"ok": False, "error": "missing_scope"})

    bridge._app.client.users_info = boom

    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "C01ENG",
            "text": "first",
            "ts": "1.000",
            "channel_type": "channel",
        }
    )
    await bridge._on_message(
        {
            "user": "U05ALICE",
            "channel": "C01ENG",
            "text": "second",
            "ts": "2.000",
            "channel_type": "channel",
        }
    )
    assert len(enqueued) == 2
    # Both fell back to user_id for display.
    assert enqueued[0].author_display == "U05ALICE"
    assert enqueued[1].author_display == "U05ALICE"
    # Email is None when lookup fails.
    assert enqueued[0].extra["slack_email"] is None
    # Failures retry — both messages tried users.info (no failure cache).
    assert call_count == 2


@pytest.mark.asyncio
async def test_users_info_thin_profile_falls_back(bridge_with_fake_app):
    """If users.info returns a profile without real_name or display_name
    (some workspaces don't expose either), fall back to user_id rather
    than crashing."""
    bridge, enqueued, _ = bridge_with_fake_app
    await bridge._on_message(
        {
            "user": "U99STRANGER",  # not in the fake fixture's _profiles
            "channel": "C01ENG",
            "text": "hi",
            "ts": "1.000",
            "channel_type": "channel",
        }
    )
    assert len(enqueued) == 1
    # The fake fixture returns a thin profile (no real_name, no display_name)
    # for users not in _profiles, which falls back to user.name (= user_id
    # in this case).
    assert enqueued[0].author_display == "U99STRANGER"
    assert enqueued[0].extra["slack_email"] is None


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


# ─── fetch_history ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_history_returns_oldest_first(bridge_with_fake_app):
    """Slack's conversations.history returns newest-first; our
    fetch_history flips to oldest-first so the agent reads in
    conversational order."""
    bridge, _, _ = bridge_with_fake_app
    # Newest-first stream — what Slack's API returns.
    payload = {
        "messages": [
            {"ts": "1714768922.000300", "user": "U05ALICE", "text": "third"},
            {"ts": "1714768921.000200", "user": "U05ALICE", "text": "second"},
            {"ts": "1714768920.000100", "user": "U05ALICE", "text": "first"},
        ],
    }

    async def fake_history(**kw):
        return payload

    bridge._app.client.conversations_history = fake_history  # type: ignore[attr-defined]
    out = await bridge.fetch_history("slack-C01ABC", limit=10)
    assert [m.content for m in out] == ["first", "second", "third"]
    assert out[0].id == "1714768920.000100"
    # ISO ts derived from Slack ts.
    assert out[0].ts.startswith("2024-")  # 1714768920 → April 2024
    assert out[0].author_display == "Alice Smith"


@pytest.mark.asyncio
async def test_fetch_history_passes_before_cursor(bridge_with_fake_app):
    """``before`` is forwarded as Slack's ``latest`` with inclusive=False."""
    bridge, _, _ = bridge_with_fake_app
    captured: dict = {}

    async def fake_history(**kw):
        captured.update(kw)
        return {"messages": []}

    bridge._app.client.conversations_history = fake_history  # type: ignore[attr-defined]
    await bridge.fetch_history(
        "slack-C01ABC", limit=20, before="1714768920.000100",
    )
    assert captured["latest"] == "1714768920.000100"
    assert captured["inclusive"] is False
    assert captured["channel"] == "C01ABC"
    assert captured["limit"] == 20


@pytest.mark.asyncio
async def test_fetch_history_clamps_to_100(bridge_with_fake_app):
    bridge, _, _ = bridge_with_fake_app
    captured: dict = {}

    async def fake_history(**kw):
        captured.update(kw)
        return {"messages": []}

    bridge._app.client.conversations_history = fake_history  # type: ignore[attr-defined]
    await bridge.fetch_history("slack-C01ABC", limit=999)
    assert captured["limit"] == 100


@pytest.mark.asyncio
async def test_fetch_history_unconnected_returns_empty():
    bridge = SlackBridge(
        bot_token="x", app_token="x",
        enqueue=AsyncMock(return_value=True),
    )
    out = await bridge.fetch_history("slack-C01ABC")
    assert out == []


@pytest.mark.asyncio
async def test_fetch_history_marks_bot_messages(bridge_with_fake_app):
    """Slack uses ``bot_id`` instead of (or alongside) ``user`` for
    bot-authored messages; the bridge sets is_bot accordingly."""
    bridge, _, _ = bridge_with_fake_app
    payload = {"messages": [
        {"ts": "1714768925.000100", "bot_id": "B01BOT", "user": "B01BOT",
         "text": "auto-posted"},
    ]}

    async def fake_history(**kw):
        return payload

    bridge._app.client.conversations_history = fake_history  # type: ignore[attr-defined]
    out = await bridge.fetch_history("slack-C01ABC")
    assert len(out) == 1
    assert out[0].is_bot is True
    assert out[0].content == "auto-posted"


@pytest.mark.asyncio
async def test_fetch_history_files_surface_as_attachment_urls(bridge_with_fake_app):
    bridge, _, _ = bridge_with_fake_app
    payload = {"messages": [
        {
            "ts": "1714768926.000200", "user": "U05ALICE",
            "text": "see file",
            "files": [
                {"name": "x.png", "url_private": "https://files.slack.com/x.png"},
            ],
        },
    ]}

    async def fake_history(**kw):
        return payload

    bridge._app.client.conversations_history = fake_history  # type: ignore[attr-defined]
    out = await bridge.fetch_history("slack-C01ABC")
    assert out[0].attachment_urls == ("https://files.slack.com/x.png",)


# ---- supervisor: retry-with-backoff around handler.start_async() ---------


@pytest.mark.asyncio
async def test_slack_connect_retains_runner_task(monkeypatch):
    bridge = SlackBridge(
        bot_token="xoxb-x",
        app_token="xapp-x",
        enqueue=AsyncMock(return_value=True),
    )
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_start_async():
        started.set()
        await release.wait()

    fake_handler = SimpleNamespace(
        start_async=fake_start_async,
        close_async=AsyncMock(),
    )
    fake_app = SimpleNamespace(
        message=lambda handler: handler,
        event=lambda name: (lambda handler: handler),
        client=SimpleNamespace(auth_test=AsyncMock()),
    )
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncApp", lambda token: fake_app,
    )
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    await bridge.connect()
    assert bridge._runner in bridge._background_tasks
    await asyncio.wait_for(started.wait(), timeout=1.0)

    release.set()
    await asyncio.wait_for(bridge._runner, timeout=1.0)
    await asyncio.sleep(0)
    assert bridge._runner not in bridge._background_tasks


@pytest.mark.asyncio
async def test_slack_supervisor_retries_on_transient(monkeypatch, tmp_path: Path):
    """A transient SlackApiError (5xx-shape, not in fatal_codes set) gets
    retried with exponential backoff. First attempt fails; second
    succeeds; supervisor exits cleanly."""
    import asyncio
    from slack_sdk.errors import SlackApiError
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.05

    attempts = {"n": 0}

    async def fake_start_async():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise SlackApiError(
                message="503", response={"ok": False, "error": "service_unavailable"},
            )
        return None  # second attempt: clean exit

    fake_handler = SimpleNamespace(
        start_async=fake_start_async,
        close_async=AsyncMock(),
    )
    fake_app = SimpleNamespace(
        client=SimpleNamespace(auth_test=AsyncMock(return_value={"user_id": "U_BOT"})),
    )

    bridge._app = fake_app
    bridge._handler = fake_handler

    # Force a fresh handler on each retry — mimic the real init.
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    await asyncio.wait_for(bridge._runner, timeout=2.0)
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_slack_supervisor_does_not_retry_on_invalid_auth(monkeypatch, tmp_path: Path):
    """``invalid_auth`` is operator-actionable (rotated bot token).
    Retrying just spams. The supervisor must propagate."""
    import asyncio
    from slack_sdk.errors import SlackApiError
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start_async():
        attempts["n"] += 1
        raise SlackApiError(
            message="bad token", response={"ok": False, "error": "invalid_auth"},
        )

    fake_handler = SimpleNamespace(start_async=fake_start_async, close_async=AsyncMock())
    fake_app = SimpleNamespace(client=SimpleNamespace(auth_test=AsyncMock()))
    bridge._app = fake_app
    bridge._handler = fake_handler
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    with pytest.raises(SlackApiError):
        await asyncio.wait_for(bridge._runner, timeout=1.0)
    assert attempts["n"] == 1  # no retries on operator-actionable errors


@pytest.mark.asyncio
async def test_slack_supervisor_does_not_retry_on_missing_scope(monkeypatch, tmp_path: Path):
    """``missing_scope`` requires Slack app dashboard config change.
    Same fail-fast posture as invalid_auth."""
    import asyncio
    from slack_sdk.errors import SlackApiError
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start_async():
        attempts["n"] += 1
        raise SlackApiError(
            message="missing scope", response={"ok": False, "error": "missing_scope"},
        )

    fake_handler = SimpleNamespace(start_async=fake_start_async, close_async=AsyncMock())
    bridge._app = SimpleNamespace(client=SimpleNamespace(auth_test=AsyncMock()))
    bridge._handler = fake_handler
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    with pytest.raises(SlackApiError):
        await asyncio.wait_for(bridge._runner, timeout=1.0)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_slack_supervisor_fires_algedonic_after_three_attempts(monkeypatch, tmp_path: Path):
    """``slack_bridge_retry`` event should fire only after attempts >= 3
    so a one-off transient doesn't spam the algedonic block."""
    import asyncio
    from slack_sdk.errors import SlackApiError

    captured: list[tuple[str, dict]] = []

    async def fake_log_event(kind: str, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.bridges.slack._safe_log_event", fake_log_event)

    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.001
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start_async():
        attempts["n"] += 1
        if attempts["n"] >= 5:
            return None
        raise SlackApiError(
            message="503", response={"ok": False, "error": "service_unavailable"},
        )

    fake_handler = SimpleNamespace(start_async=fake_start_async, close_async=AsyncMock())
    bridge._app = SimpleNamespace(client=SimpleNamespace(auth_test=AsyncMock()))
    bridge._handler = fake_handler
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    await asyncio.wait_for(bridge._runner, timeout=2.0)
    for _ in range(20):
        await asyncio.sleep(0)

    retry_events = [(k, f) for k, f in captured if k == "slack_bridge_retry"]
    assert len(retry_events) == 2  # attempts 3, 4 (5th succeeded)
    assert retry_events[0][1]["attempt"] == 3
    assert retry_events[0][1]["slack_error"] == "service_unavailable"


@pytest.mark.asyncio
async def test_slack_supervisor_clean_exit_when_handler_returns(monkeypatch, tmp_path: Path):
    """If ``handler.start_async()`` returns cleanly the supervisor exits
    rather than retrying."""
    import asyncio
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.01

    attempts = {"n": 0}

    async def fake_start_async():
        attempts["n"] += 1
        return None

    fake_handler = SimpleNamespace(start_async=fake_start_async, close_async=AsyncMock())
    bridge._app = SimpleNamespace(client=SimpleNamespace(auth_test=AsyncMock()))
    bridge._handler = fake_handler
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    await asyncio.wait_for(bridge._runner, timeout=1.0)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_slack_supervisor_refreshes_bot_user_id_after_outage(monkeypatch, tmp_path: Path):
    """Initial auth_test 503s; after the supervisor retries
    start_async, _refresh_bot_user_id runs again and resolves the bot
    user_id once Slack accepts the call. Without this fix, an outage
    at startup would leave _bot_user_id None forever."""
    import asyncio
    from slack_sdk.errors import SlackApiError
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    # This test specifically exercises the auth_test refresh path —
    # don't preset _bot_user_id (the global sweep above sets it for
    # other tests that don't care about auth_test behavior).
    bridge._bot_user_id = None
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.001
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.01

    auth_calls = {"n": 0}

    async def fake_auth_test():
        auth_calls["n"] += 1
        if auth_calls["n"] == 1:
            raise SlackApiError(
                message="503", response={"ok": False, "error": "service_unavailable"},
            )
        return {"user_id": "U_BOT_123"}

    start_attempts = {"n": 0}

    async def fake_start_async():
        start_attempts["n"] += 1
        if start_attempts["n"] == 1:
            raise SlackApiError(
                message="503", response={"ok": False, "error": "service_unavailable"},
            )
        return None

    fake_handler = SimpleNamespace(
        start_async=fake_start_async, close_async=AsyncMock(),
    )
    fake_app = SimpleNamespace(
        client=SimpleNamespace(auth_test=fake_auth_test),
    )
    bridge._app = fake_app
    bridge._handler = fake_handler
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    await asyncio.wait_for(bridge._runner, timeout=2.0)

    # Both auth_test attempts ran (first 503'd, second succeeded), and
    # _bot_user_id is now populated.
    assert auth_calls["n"] >= 2
    assert bridge._bot_user_id == "U_BOT_123"


@pytest.mark.asyncio
async def test_slack_supervisor_skips_auth_test_when_user_id_already_set(monkeypatch, tmp_path: Path):
    """Once _bot_user_id is populated, the supervisor doesn't re-call
    auth_test on subsequent retry iterations — that'd be wasted Slack
    API quota during a sustained start_async outage."""
    import asyncio
    from slack_sdk.errors import SlackApiError
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.001
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.01
    bridge._bot_user_id = "U_BOT_PRESET"  # already set by prior connect

    auth_calls = {"n": 0}

    async def fake_auth_test():
        auth_calls["n"] += 1
        return {"user_id": "U_OTHER"}

    start_attempts = {"n": 0}

    async def fake_start_async():
        start_attempts["n"] += 1
        if start_attempts["n"] < 3:
            raise SlackApiError(
                message="503", response={"ok": False, "error": "service_unavailable"},
            )
        return None

    fake_handler = SimpleNamespace(
        start_async=fake_start_async, close_async=AsyncMock(),
    )
    bridge._app = SimpleNamespace(client=SimpleNamespace(auth_test=fake_auth_test))
    bridge._handler = fake_handler
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler",
        lambda app, app_token: fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    await asyncio.wait_for(bridge._runner, timeout=2.0)

    # Auth_test never called — _bot_user_id was preset.
    assert auth_calls["n"] == 0
    assert bridge._bot_user_id == "U_BOT_PRESET"


@pytest.mark.asyncio
async def test_slack_supervisor_closes_old_handler_before_constructing_new(monkeypatch, tmp_path: Path):
    """Between retry attempts, the previous handler is closed before
    the next one is constructed. Mirrors the Discord-side test."""
    import asyncio
    from slack_sdk.errors import SlackApiError
    bridge = SlackBridge(bot_token="xoxb-x", app_token="xapp-x", enqueue=AsyncMock(return_value=True))
    bridge._bot_user_id = "U_BOT_TEST"  # short-circuit auth_test in supervisor
    bridge._RECONNECT_BACKOFF_INITIAL_SECONDS = 0.001
    bridge._RECONNECT_BACKOFF_CAP_SECONDS = 0.01
    bridge._bot_user_id = "U_BOT"  # skip auth_test path

    construct_calls: list[int] = []
    close_calls: list[int] = []

    def make_fake_handler(app, app_token):
        instance_id = len(construct_calls)
        construct_calls.append(instance_id)

        async def fake_close():
            close_calls.append(instance_id)

        async def fake_start_async():
            if instance_id < 2:
                raise SlackApiError(
                    message="503", response={"ok": False, "error": "service_unavailable"},
                )
            return None

        return SimpleNamespace(
            start_async=fake_start_async, close_async=fake_close,
        )

    bridge._handler = make_fake_handler(None, None)  # initial
    bridge._app = SimpleNamespace(client=SimpleNamespace(auth_test=AsyncMock()))
    monkeypatch.setattr(
        "mimir.bridges.slack.AsyncSocketModeHandler", make_fake_handler,
    )

    bridge._runner = asyncio.create_task(bridge._supervised_run())
    await asyncio.wait_for(bridge._runner, timeout=2.0)

    # Three handlers constructed (initial + 2 retries).
    assert construct_calls == [0, 1, 2]
    # Old handlers closed before each replacement (initial and first
    # retry close; final success leaves the third handler open).
    assert close_calls == [0, 1]


def test_slack_should_emit_retry_algedonic_throttling():
    """Mirror of the Discord-side throttle test."""
    from mimir.bridges.slack import _should_emit_retry_algedonic
    assert _should_emit_retry_algedonic(1) is False
    assert _should_emit_retry_algedonic(2) is False
    for n in range(3, 10):
        assert _should_emit_retry_algedonic(n) is True, n
    assert _should_emit_retry_algedonic(10) is True
    assert _should_emit_retry_algedonic(11) is False
    assert _should_emit_retry_algedonic(20) is True
    assert _should_emit_retry_algedonic(101) is False
