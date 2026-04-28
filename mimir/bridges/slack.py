"""Slack bridge (SPEC §7.2.1) — port of lettabot's slack channel adapter.

Owns a long-lived Socket Mode connection inside the mimir process. Inbound
messages → ``dispatcher.enqueue(AgentEvent)``. Outbound text is sent via
``chat.postMessage``. Reactions use ``reactions.add``.

Channel-id convention (SPEC §7.2.3):
- ``slack-<channel_id>``       for public/private channels (Slack ids start with C)
- ``dm-slack-<channel_id>``    for DMs (channel ids start with D; suppressed
  from cross-channel author pulls per the §5.4 privacy rule)

Auth requires both:
- ``SLACK_BOT_TOKEN`` (xoxb-...) — bot user OAuth token
- ``SLACK_APP_TOKEN`` (xapp-...) — app-level token for Socket Mode

The ``slack-bolt`` library is an optional dependency (``mimir[slack]``).
Importing this module without it raises a clear error so non-Slack
deployments don't pay the dep cost.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from slack_bolt.async_app import AsyncApp  # type: ignore[import-not-found]
    from slack_bolt.adapter.socket_mode.async_handler import (  # type: ignore[import-not-found]
        AsyncSocketModeHandler,
    )
    from slack_sdk.errors import SlackApiError  # type: ignore[import-not-found]
except ImportError as _exc:  # pragma: no cover - optional dep
    raise ImportError(
        "mimir.bridges.slack requires `slack-bolt` — install with "
        "`pip install mimir[slack]` or `pip install slack-bolt`."
    ) from _exc

from ..models import AgentEvent
from .base import Bridge, SendResult

log = logging.getLogger(__name__)

# Slack mrkdwn doesn't have a hard char cap on chat.postMessage like Discord's
# 2000, but the Slack API's text field is capped at 40k. Realistic ergonomics
# (mobile clients, search) prefer ~3-4k per message. Match Discord's chunk
# helper but at the larger limit so the agent's longer replies don't
# fragment unnecessarily.
SLACK_MESSAGE_CHAR_LIMIT = 3500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slack_channel_to_id(channel: str, channel_type: str | None = None) -> str:
    """Convert a Slack channel to mimir's channel_id.

    Both 1:1 DMs (``im``, ``D...``) and multi-party DMs (``mpim``, ``G...``)
    route through the ``dm-slack-`` prefix so the SPEC §5.4 privacy filter
    treats them as private. Legacy private channels (``group``) keep the
    plain ``slack-`` prefix — they're org-shaped, not DM-shaped.

    ``channel_type`` is the authoritative signal (from the event payload).
    Prefix is a fallback for callers that don't have the event in hand.
    """
    if channel_type in ("im", "mpim"):
        return f"dm-slack-{channel}"
    if channel_type is None and channel.startswith("D"):
        # Fallback when the caller didn't carry channel_type — only D-prefix
        # IDs are unambiguous DMs.
        return f"dm-slack-{channel}"
    return f"slack-{channel}"


def _channel_id_to_slack(channel_id: str) -> str | None:
    """Pull the Slack channel id back out of mimir's channel_id."""
    for prefix in ("dm-slack-", "slack-"):
        if channel_id.startswith(prefix):
            return channel_id[len(prefix):]
    return None


def _is_dm_channel(channel: str, channel_type: str | None = None) -> bool:
    """Both 1:1 IMs and multi-party DMs (MPIMs) are DMs for the §5.4
    privacy filter. ``channel_type`` is authoritative; prefix is a
    fallback (only ``D...`` is unambiguous)."""
    if channel_type in ("im", "mpim"):
        return True
    if channel_type is None:
        return channel.startswith("D")
    return False


def _chunk_message(text: str, limit: int = SLACK_MESSAGE_CHAR_LIMIT) -> list[str]:
    """Split ``text`` into chunks each ≤ ``limit`` chars.

    Prefers paragraph boundaries; falls back to line boundaries; last resort
    is hard slicing. Same algorithm as the Discord chunker — Slack just has
    a larger natural ceiling.
    """
    if limit <= 0:
        limit = SLACK_MESSAGE_CHAR_LIMIT
    if len(text) <= limit:
        return [text]

    import re

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


def _normalize_emoji(emoji: str) -> str:
    """Slack's reactions.add API takes an alias name (``thumbsup``), not the
    unicode glyph or wrapped form (``:thumbsup:``). Strip the wrapping if
    present; otherwise pass through (caller may have given a name already).

    Note: this doesn't translate unicode → name. The agent should call with
    Slack's canonical alias names (``thumbsup``, ``raised_hands``, etc.).
    """
    s = emoji.strip()
    if len(s) >= 2 and s.startswith(":") and s.endswith(":"):
        return s[1:-1]
    return s


# ---------------------------------------------------------------------------
# SlackBridge
# ---------------------------------------------------------------------------


@dataclass
class SlackBridge(Bridge):
    """In-process Slack bridge using slack-bolt Socket Mode.

    Args:
        bot_token: ``xoxb-`` bot user OAuth token (env: ``SLACK_BOT_TOKEN``).
        app_token: ``xapp-`` app-level token for Socket Mode (env: ``SLACK_APP_TOKEN``).
        enqueue: dispatcher's enqueue coroutine.
        respond_to_bots: if True, on_message accepts other bots' messages.
            Default False — humans only. Self-messages are always skipped
            via the bot's own user id.
    """

    bot_token: str
    app_token: str
    enqueue: Callable[[AgentEvent], Awaitable[bool]]
    respond_to_bots: bool = False
    _app: Any | None = field(default=None, init=False, repr=False)
    _handler: Any | None = field(default=None, init=False, repr=False)
    _runner: asyncio.Task | None = field(default=None, init=False, repr=False)
    _bot_user_id: str | None = field(default=None, init=False, repr=False)

    prefixes = ("slack-", "dm-slack-")
    name = "slack"

    async def connect(self) -> None:
        if self._app is not None:
            return
        self._app = AsyncApp(token=self.bot_token)
        self._register_handlers(self._app)
        self._handler = AsyncSocketModeHandler(self._app, self.app_token)
        # ``handler.start_async`` is a long-running coroutine — run it as a
        # task so connect() returns once the WebSocket is up.
        self._runner = asyncio.create_task(
            self._handler.start_async(), name="mimir-slack-runner"
        )
        # Best-effort lookup of the bot's own user id, used to skip self-messages.
        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id")
        except SlackApiError as exc:
            log.warning("SlackBridge auth_test failed: %s", exc)

    async def disconnect(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception:  # noqa: BLE001
                log.exception("SlackBridge handler close failed")
        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
        self._app = None
        self._handler = None
        self._runner = None

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
    ) -> SendResult:
        if self._app is None:
            return SendResult(sent=False, error="slack app not connected")
        slack_channel = _channel_id_to_slack(channel_id)
        if slack_channel is None:
            return SendResult(sent=False, error=f"bad channel_id: {channel_id!r}")

        chunks = [c for c in _chunk_message(text) if c.strip()]
        if not chunks and not attachment_paths:
            return SendResult(sent=False, error="empty message")

        last_id: str | None = None
        sent_count = 0
        try:
            for chunk in chunks:
                resp = await self._app.client.chat_postMessage(
                    channel=slack_channel, text=chunk
                )
                last_id = resp.get("ts") or last_id
                sent_count += 1
            for path in attachment_paths or []:
                resp = await self._app.client.files_upload_v2(
                    channel=slack_channel,
                    file=str(path),
                    filename=path.name,
                )
                # files_upload_v2 returns a different shape; ts may be nested.
                file_obj = resp.get("file") or {}
                shares = file_obj.get("shares") or {}
                pub = shares.get("public") or {}
                priv = shares.get("private") or {}
                ts = (
                    next(iter(pub.get(slack_channel, [{}]) or [{}]), {}).get("ts")
                    or next(iter(priv.get(slack_channel, [{}]) or [{}]), {}).get("ts")
                )
                if ts:
                    last_id = ts
                sent_count += 1
        except SlackApiError as exc:
            return SendResult(
                sent=sent_count > 0,
                message_id=last_id,
                chunks=sent_count,
                error=f"slack api error after {sent_count} chunk(s): {exc}",
            )
        return SendResult(sent=True, message_id=last_id, chunks=sent_count)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        if self._app is None:
            return False
        slack_channel = _channel_id_to_slack(channel_id)
        if slack_channel is None:
            return False
        name = _normalize_emoji(emoji)
        if not name:
            return False
        try:
            await self._app.client.reactions_add(
                channel=slack_channel, timestamp=message_id, name=name
            )
            return True
        except SlackApiError as exc:
            # ``already_reacted`` is benign — treat as success.
            if exc.response and exc.response.get("error") == "already_reacted":
                return True
            log.warning("SlackBridge.react failed: %s", exc)
            return False

    # ---- inbound wiring -----------------------------------------------

    def _register_handlers(self, app: Any) -> None:
        async def on_message(event: dict[str, Any]) -> None:
            await self._on_message(event)

        async def on_app_mention(event: dict[str, Any]) -> None:
            # app_mention events overlap with regular message events for
            # mention-bearing messages. Letting both paths fire would
            # double-enqueue. We rely on the message handler for content
            # and just log the mention as observability.
            log.debug("slack app_mention from %s in %s", event.get("user"), event.get("channel"))

        # The slack-bolt decorator registers under the underlying app.
        app.message(on_message)
        app.event("app_mention")(on_app_mention)

    async def _on_message(self, event: dict[str, Any]) -> None:
        # Skip our own messages.
        user_id = event.get("user")
        if user_id is None:
            return
        if self._bot_user_id and user_id == self._bot_user_id:
            return

        # Skip subtypes we don't handle (channel_join, message_changed, etc.)
        # except file_share which carries content + attachments.
        subtype = event.get("subtype")
        if subtype is not None and subtype != "file_share":
            return

        # Skip other bots unless opted in.
        is_bot = bool(event.get("bot_id"))
        if is_bot and not self.respond_to_bots:
            return

        slack_channel = event.get("channel") or ""
        channel_type = event.get("channel_type")
        channel_id = _slack_channel_to_id(slack_channel, channel_type)
        is_dm = _is_dm_channel(slack_channel, channel_type)

        text = (event.get("text") or "").strip()
        if not text:
            text = "User sent a message with no text."

        agent_event = AgentEvent(
            trigger="user_message",
            channel_id=channel_id,
            content=text,
            author=user_id,
            author_id=user_id,
            source_id=event.get("ts"),
            source="slack",
            extra={
                "channel_conversation_type": "dm" if is_dm else "multi_user",
                "channel_visibility": "private" if is_dm else "public",
                "thread_ts": event.get("thread_ts"),
            },
        )
        await self.enqueue(agent_event)


__all__ = [
    "SlackBridge",
    "SLACK_MESSAGE_CHAR_LIMIT",
    "_chunk_message",
    "_slack_channel_to_id",
    "_channel_id_to_slack",
    "_is_dm_channel",
    "_normalize_emoji",
]
