"""Discord bridge (SPEC §7.2.1) — port of open-strix's discord.py.

Owns a long-lived ``discord.Client`` connection inside the mimir process.
Inbound messages → ``dispatcher.enqueue(AgentEvent)``. Outbound text is
chunked at Discord's 2000-char limit. Reactions use the native
``message.add_reaction`` API.

Channel-id convention (SPEC §7.2.3):
- ``discord-<channel_id>``   for guild text channels and threads
- ``dm-discord-<channel_id>``  for DMs (private; suppressed from cross-channel
  author pulls per the §5.4 privacy rule)

The ``discord-py`` library is an optional dependency (``mimir[discord]``).
Importing this module without it raises a clear error so non-Discord
deployments don't pay the dep cost.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    import discord  # type: ignore[import-not-found]
except ImportError as _exc:  # pragma: no cover - optional dep
    raise ImportError(
        "mimir.bridges.discord requires `discord-py` — install with "
        "`pip install mimir[discord]` or `pip install discord-py`."
    ) from _exc

from ..models import AgentEvent
from .base import Bridge, SendResult

log = logging.getLogger(__name__)

DISCORD_MESSAGE_CHAR_LIMIT = 2000


# ---------------------------------------------------------------------------
# Helpers (ported verbatim from open-strix)
# ---------------------------------------------------------------------------


def _channel_conversation_type(channel: Any) -> str:
    """Classify a discord channel into ``"dm"`` or ``"multi_user"``."""
    channel_type = getattr(channel, "type", None)
    dm_type = getattr(discord.ChannelType, "private", None)
    if channel_type == dm_type or isinstance(channel, discord.DMChannel):
        return "dm"
    return "multi_user"


def _channel_visibility(channel: Any, conversation_type: str) -> str:
    """Best-effort public/private classification for the channel."""
    if conversation_type == "dm":
        return "private"
    channel_type = getattr(channel, "type", None)
    private_threadlike = {
        getattr(discord.ChannelType, "group", None),
        getattr(discord.ChannelType, "private_thread", None),
    }
    private_threadlike.discard(None)
    if channel_type in private_threadlike:
        return "private"
    public_threadlike = {
        getattr(discord.ChannelType, "public_thread", None),
        getattr(discord.ChannelType, "news_thread", None),
    }
    public_threadlike.discard(None)
    if channel_type in public_threadlike:
        return "public"
    guild = getattr(channel, "guild", None)
    permissions_for = getattr(channel, "permissions_for", None)
    default_role = getattr(guild, "default_role", None)
    if guild is not None and callable(permissions_for) and default_role is not None:
        permissions = permissions_for(default_role)
        can_view = getattr(permissions, "view_channel", None)
        if can_view is None:
            can_view = getattr(permissions, "read_messages", None)
        if can_view is not None:
            return "public" if bool(can_view) else "private"
    return "unknown"


def _chunk_message(text: str, limit: int = DISCORD_MESSAGE_CHAR_LIMIT) -> list[str]:
    """Split ``text`` so each chunk fits Discord's per-message char limit.

    Prefers paragraph boundaries; falls back to line boundaries; last resort
    is hard slicing. Same algorithm as open-strix's ``_chunk_discord_message``.
    """
    if limit <= 0:
        limit = DISCORD_MESSAGE_CHAR_LIMIT
    if len(text) <= limit:
        return [text]

    def _split_oversized_block(block: str) -> list[str]:
        if len(block) <= limit:
            return [block]
        lines = block.splitlines(keepends=True)
        if len(lines) <= 1:
            return [block[idx : idx + limit] for idx in range(0, len(block), limit)]
        out: list[str] = []
        current = ""
        for line in lines:
            if len(line) > limit:
                if current:
                    out.append(current)
                    current = ""
                out.extend(line[idx : idx + limit] for idx in range(0, len(line), limit))
                continue
            if not current:
                current = line
                continue
            if len(current) + len(line) <= limit:
                current += line
                continue
            out.append(current)
            current = line
        if current:
            out.append(current)
        return out

    paragraph_blocks: list[str] = []
    cursor = 0
    for match in re.finditer(r"\n\s*\n+", text):
        end = match.end()
        paragraph_blocks.append(text[cursor:end])
        cursor = end
    if cursor < len(text):
        paragraph_blocks.append(text[cursor:])
    if not paragraph_blocks:
        paragraph_blocks = [text]

    chunks: list[str] = []
    current = ""
    for block in paragraph_blocks:
        if not block:
            continue
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_oversized_block(block))
            continue
        if not current:
            current = block
            continue
        if len(current) + len(block) <= limit:
            current += block
            continue
        chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks


def _channel_to_id(channel: Any) -> str:
    """Convert a discord channel object to mimir's channel_id string."""
    raw = str(getattr(channel, "id", "") or "")
    if _channel_conversation_type(channel) == "dm":
        return f"dm-discord-{raw}"
    return f"discord-{raw}"


def _channel_id_to_int(channel_id: str) -> int | None:
    """Pull the numeric id back out of a ``discord-<n>`` / ``dm-discord-<n>`` channel id."""
    for prefix in ("dm-discord-", "discord-"):
        if channel_id.startswith(prefix):
            tail = channel_id[len(prefix) :]
            try:
                return int(tail)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Internal discord client — composition over inheritance
# ---------------------------------------------------------------------------


class _DiscordClient(discord.Client):
    """Thin subclass that forwards events to the bridge."""

    def __init__(self, bridge: "DiscordBridge") -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        # Reaction intents — needed so on_raw_reaction_add fires for
        # reactions to messages the bot didn't see live (i.e. bot
        # restart between send and reaction). The "raw" variants
        # don't require the message to be in the client's cache.
        intents.reactions = True
        super().__init__(intents=intents)
        self._bridge = bridge

    async def on_ready(self) -> None:  # pragma: no cover - network
        log.info("DiscordBridge ready as %s", self.user)
        await self._bridge._on_ready(self.user)

    async def on_message(self, message: discord.Message) -> None:  # pragma: no cover - network
        await self._bridge._on_message(message)

    async def on_raw_reaction_add(self, payload) -> None:  # pragma: no cover - network
        await self._bridge._on_reaction(payload, action="add")

    async def on_raw_reaction_remove(self, payload) -> None:  # pragma: no cover - network
        # We surface "react add" as the algedonic signal; removes are
        # noise (people fix typo'd reactions). Hook is here for future
        # use (e.g. counting net-positive reactions).
        return None


# ---------------------------------------------------------------------------
# DiscordBridge
# ---------------------------------------------------------------------------


@dataclass
class DiscordBridge(Bridge):
    """In-process Discord bridge.

    Args:
        token: discord-py bot token (DISCORD_TOKEN env var).
        enqueue: dispatcher's enqueue coroutine.
        respond_to_bots: if True, on_message accepts bot-authored messages
            (useful for inter-bot collaboration). Default False — humans only.
    """

    token: str
    enqueue: Callable[[AgentEvent], Awaitable[bool]]
    respond_to_bots: bool = False
    _client: _DiscordClient | None = field(default=None, init=False, repr=False)
    _runner: asyncio.Task | None = field(default=None, init=False, repr=False)

    prefixes = ("discord-", "dm-discord-")
    name = "discord"

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = _DiscordClient(self)
        # ``client.start`` is a long-running coroutine — run it as a task so
        # connect() returns once the login handshake is in flight.
        self._runner = asyncio.create_task(
            self._client.start(self.token), name="mimir-discord-runner"
        )

    async def disconnect(self) -> None:
        if self._client is not None and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                log.exception("DiscordBridge close failed")
        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
        self._client = None
        self._runner = None

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
    ) -> SendResult:
        if self._client is None or self._client.is_closed():
            return SendResult(sent=False, error="discord client not connected")
        cid_int = _channel_id_to_int(channel_id)
        if cid_int is None:
            return SendResult(sent=False, error=f"bad channel_id: {channel_id!r}")

        channel = self._client.get_channel(cid_int)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(cid_int)
            except discord.DiscordException as exc:
                return SendResult(sent=False, error=f"channel fetch failed: {exc}")
        # Duck-type the messageable check — discord.abc.Messageable isn't a
        # registerable ABC, and forcing isinstance would require tests to
        # build real Channel objects (which need a Client/State). A channel
        # with .send is messageable enough; if the actual call fails, we
        # report the DiscordException cleanly below.
        if not callable(getattr(channel, "send", None)):
            return SendResult(sent=False, error=f"channel {cid_int} is not messageable")

        chunks = [c for c in _chunk_message(text) if c.strip()]
        files = [discord.File(str(p)) for p in (attachment_paths or [])]
        if files and not chunks:
            chunks = [""]

        last_id: str | None = None
        sent_count = 0
        try:
            for i, chunk in enumerate(chunks):
                if i == 0 and files:
                    sent_msg = (
                        await channel.send(chunk, files=files)
                        if chunk
                        else await channel.send(files=files)
                    )
                else:
                    sent_msg = await channel.send(chunk)
                last_id = str(getattr(sent_msg, "id", "") or "") or last_id
                sent_count += 1
        except discord.DiscordException as exc:
            return SendResult(
                sent=sent_count > 0,
                message_id=last_id,
                chunks=sent_count,
                error=f"discord send error after {sent_count} chunk(s): {exc}",
            )
        return SendResult(sent=True, message_id=last_id, chunks=sent_count)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        if self._client is None or self._client.is_closed():
            return False
        cid_int = _channel_id_to_int(channel_id)
        if cid_int is None:
            return False
        try:
            mid_int = int(message_id)
        except ValueError:
            return False
        try:
            channel = self._client.get_channel(cid_int) or await self._client.fetch_channel(cid_int)
            if not hasattr(channel, "fetch_message"):
                return False
            message = await channel.fetch_message(mid_int)
            await message.add_reaction(emoji)
            return True
        except discord.DiscordException as exc:
            log.warning("DiscordBridge.react failed: %s", exc)
            return False

    # ---- inbound callbacks (called by _DiscordClient) ------------------

    async def _on_ready(self, user: Any) -> None:
        from ..event_logger import log_event

        try:
            await log_event("bridge_ready", bridge=self.name, user=str(user))
        except Exception:  # noqa: BLE001
            pass

    async def _on_message(self, message: Any) -> None:
        # Skip our own messages.
        client_user = getattr(self._client, "user", None) if self._client else None
        if client_user is not None and getattr(message.author, "id", None) == getattr(
            client_user, "id", None
        ):
            return
        author_is_bot = bool(getattr(message.author, "bot", False))
        if author_is_bot and not self.respond_to_bots:
            return

        channel = message.channel
        channel_id = _channel_to_id(channel)
        conv_type = _channel_conversation_type(channel)
        visibility = _channel_visibility(channel, conv_type)
        channel_name = (
            str(getattr(channel, "name", "")).strip() or None
            if conv_type != "dm"
            else None
        )

        content = (message.content or "").strip()
        if not content:
            content = "User sent a message with no text."

        author_id = str(getattr(message.author, "id", "") or "") or None
        # Discord exposes display info directly on the User/Member object —
        # no API round-trip needed (unlike Slack). Preference order:
        #   - display_name: server-specific nickname OR global name OR username
        #     (this is the value Discord clients show; what users see)
        #   - global_name: cross-server display name
        #   - str(message.author): legacy-format username[#discriminator]
        # Falling through the chain keeps us robust to Mock/SimpleNamespace
        # test stand-ins that may only provide some fields.
        author_display = (
            getattr(message.author, "display_name", None)
            or getattr(message.author, "global_name", None)
            or str(message.author)
        )
        # Platform-prefixed stable id is the matching key for cross-channel
        # / cross-platform pull (FUTURE_WORK §6.1).
        author_key = f"discord-{author_id}" if author_id else None

        event = AgentEvent(
            trigger="user_message",
            channel_id=channel_id,
            content=content,
            author=author_key,
            author_display=author_display,
            author_id=author_id,
            source_id=str(getattr(message, "id", "") or "") or None,
            source="discord",
            extra={
                "channel_conversation_type": conv_type,
                "channel_visibility": visibility,
                "channel_name": channel_name,
            },
        )
        await self.enqueue(event)

    # VSM: algedonic (in) — inbound reactions on the bot's own messages,
    #                       classified by emoji polarity, time-gated by
    #                       feedback.py's 24h window before reaching the
    #                       next turn's prompt.
    # loop_id: 2.6
    async def _on_reaction(self, payload: Any, action: str = "add") -> None:
        """Handle inbound reaction-add events. Emits a ``react_received``
        event into events.jsonl which the algedonic surfacing in
        ``mimir.feedback`` picks up (24h window, polarity classified
        per emoji). Skips reactions the bot itself added (bot's own
        ``react`` tool calls). Skips non-bot-message targets so we
        only see reactions to OUR replies, not noise from the channel.

        ``payload`` is a discord.RawReactionActionEvent — unlike
        on_reaction_add(reaction, user), the raw variant fires even
        when the target message isn't in the client's message cache
        (i.e., reactions to messages from before the bot started)."""
        from ..event_logger import log_event
        from ..reactions import classify_reaction, normalize_emoji

        # Skip our own reactions (the agent reacting via the react tool).
        client_user = getattr(self._client, "user", None) if self._client else None
        if client_user is not None and getattr(payload, "user_id", None) == getattr(
            client_user, "id", None
        ):
            return

        # Only count reactions on the BOT'S messages — that's what
        # makes them feedback. A user reacting to another user's
        # message isn't a signal about the agent's behavior.
        try:
            channel = self._client.get_channel(payload.channel_id) if self._client else None
            if channel is None:
                return
            target_msg = await channel.fetch_message(payload.message_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("DiscordBridge._on_reaction: could not fetch target: %s", exc)
            return
        target_author = getattr(target_msg, "author", None)
        if target_author is None or getattr(target_author, "id", None) != getattr(
            client_user, "id", None
        ):
            return

        # Resolve emoji string. discord.PartialEmoji can be unicode
        # or custom; ``str()`` works for both.
        emoji_glyph = normalize_emoji(str(payload.emoji))
        polarity = classify_reaction(emoji_glyph)

        # Compute target message age for the algedonic renderer.
        target_age_minutes: float | None = None
        try:
            from datetime import datetime, timezone
            created = target_msg.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            target_age_minutes = (
                datetime.now(tz=timezone.utc) - created
            ).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            pass

        author_id = str(getattr(payload, "user_id", "") or "") or None
        channel_id = _channel_to_id(channel)
        try:
            await log_event(
                "react_received",
                bridge=self.name,
                channel_id=channel_id,
                emoji=emoji_glyph,
                polarity=polarity,
                action=action,
                author=f"discord-{author_id}" if author_id else "?",
                target_message_id=str(getattr(payload, "message_id", "") or "") or None,
                target_age_minutes=target_age_minutes,
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "DiscordBridge",
    "DISCORD_MESSAGE_CHAR_LIMIT",
    "_chunk_message",
    "_channel_to_id",
    "_channel_id_to_int",
    "_channel_conversation_type",
    "_channel_visibility",
]
