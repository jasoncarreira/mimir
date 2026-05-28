"""Slack bridge (SPEC §7.2.1).

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
from ._attachments import build_inbound_path, download_to_path
from ._history import ChannelMessage
from ._seen_ids import SeenIdCache
from .base import Bridge, SendResult

log = logging.getLogger(__name__)


# chainlink #246: throttle + safe-log helpers are shared with
# DiscordBridge in mimir/bridges/_supervisor.py. Bind locally with a
# bridge label so any logger-side failure shows up under the right name.
from ._supervisor import should_emit_retry_algedonic as _should_emit_retry_algedonic
from ._supervisor import safe_log_event as _shared_safe_log_event


async def _safe_log_event(event_kind: str, **fields: Any) -> None:
    """Slack-flavored wrapper — preserves the prior log-message prefix
    while delegating to the shared implementation."""
    await _shared_safe_log_event("SlackBridge", event_kind, **fields)


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
    attachments_dir: Path | None = None
    attachments_max_bytes: int | None = None
    _app: Any | None = field(default=None, init=False, repr=False)
    _handler: Any | None = field(default=None, init=False, repr=False)
    _runner: asyncio.Task | None = field(default=None, init=False, repr=False)
    _bot_user_id: str | None = field(default=None, init=False, repr=False)
    # In-memory cache of users.info results, populated lazily on first
    # message from each user. Bounded by workspace size; lives one
    # process lifetime. Failed lookups aren't cached so they self-heal
    # if the operator adds the users:read scope post-deploy.
    _user_cache: dict[str, dict[str, str | None]] = field(
        default_factory=dict, init=False, repr=False
    )
    # chainlink #232: inbound message dedup — Slack Socket Mode is
    # documented to redeliver events on ACK loss. Bounded LRU keyed on
    # the message ``ts`` (unique within the workspace).
    _seen_ids: SeenIdCache = field(
        default_factory=SeenIdCache,
        init=False, repr=False,
    )

    prefixes = ("slack-", "dm-slack-")
    name = "slack"

    # Reconnect backoff schedule for the supervisor wrapping
    # ``handler.start_async()``. Mirrors DiscordBridge — Slack 5xx
    # incidents and Socket-Mode WebSocket disconnects typically recover
    # in minutes; 5-minute cap keeps retry cadence reasonable without
    # spamming during a sustained outage.
    _RECONNECT_BACKOFF_INITIAL_SECONDS: float = 5.0
    _RECONNECT_BACKOFF_CAP_SECONDS: float = 300.0

    async def connect(self) -> None:
        if self._app is not None:
            return
        self._app = AsyncApp(token=self.bot_token)
        self._register_handlers(self._app)
        self._handler = AsyncSocketModeHandler(self._app, self.app_token)
        # Wrap ``handler.start_async`` in a supervisor that retries on
        # transient failure (Slack 5xx, network blip, Socket-Mode
        # WebSocket disconnect). Operator-actionable errors —
        # ``invalid_auth`` (rotated bot token), ``missing_scope`` (app
        # config gap), ``token_revoked`` — propagate up so retrying
        # doesn't mask a config issue. Same shape as DiscordBridge's
        # supervisor.
        self._runner = asyncio.create_task(
            self._supervised_run(), name="mimir-slack-runner"
        )
        # ``_bot_user_id`` is filled by the supervisor (see
        # ``_refresh_bot_user_id``) on each retry iteration where it's
        # still None. That way a Slack-side outage that 503s ``auth_test``
        # at startup self-heals once Slack recovers — without it, the
        # one-shot lookup at connect-time would leave ``_bot_user_id``
        # None for the rest of the container's life and self-messages
        # wouldn't get filtered.

    async def _refresh_bot_user_id(self) -> None:
        """Best-effort lookup of the bot's own user_id via ``auth_test``.
        Called from the supervisor at the top of each iteration when
        ``_bot_user_id`` is still None. Swallows all exceptions —
        failures here are common during a Slack outage and shouldn't
        prevent the supervisor from attempting the WebSocket handshake."""
        if self._bot_user_id is not None:
            return
        if self._app is None:
            return
        try:
            auth = await self._app.client.auth_test()
            user_id = auth.get("user_id") if isinstance(auth, dict) else None
            if not user_id:
                # ``slack_sdk`` returns a SlackResponse; treat .get() the
                # same way (it implements __getitem__/get).
                user_id = getattr(auth, "get", lambda _k: None)("user_id")
            if user_id:
                self._bot_user_id = user_id
                log.info("SlackBridge: bot_user_id resolved to %s", user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("SlackBridge auth_test (supervisor) failed: %s", exc)

    async def _supervised_run(self) -> None:
        """Retry-with-exponential-backoff wrapper around
        ``self._handler.start_async()``.

        Mirrors ``DiscordBridge._supervised_run`` shape. Socket-Mode's
        long-lived WebSocket has its own internal reconnect logic
        (``slack_sdk`` retries the SOCKET-mode connection automatically);
        this supervisor only matters when ``start_async`` itself
        terminates — which happens on persistent auth failure, on
        Socket-Mode rejection, or on unexpected exceptions during
        handshake.

        Retryable: ``SlackApiError`` whose Slack-side ``error`` code is
        a transient (5xx-shape, ``service_unavailable``, ``timeout``);
        generic network/connection errors. Backoff: 5s → 10s → 20s →
        40s → 80s, capped at ``_RECONNECT_BACKOFF_CAP_SECONDS``.

        Non-retryable (raise out, runner ends, operator-actionable):
        - ``SlackApiError`` with ``error in {invalid_auth, token_revoked,
          account_inactive}`` — token rotation needed
        - ``SlackApiError`` with ``error == missing_scope`` — Slack app
          config gap, scope must be added in dashboard
        - ``CancelledError`` — clean shutdown via ``disconnect()``

        Fires ``slack_bridge_retry`` (algedonic-negative) on attempts
        3-9 inclusive, then every 10th attempt thereafter — see
        ``_should_emit_retry_algedonic``. Surfaces the early "is this
        real?" signal fast, then throttles during sustained outages.
        ``slack_bridge_auth_failure`` / ``_scope_failure`` fire once on
        terminal failure.

        Also refreshes ``_bot_user_id`` via ``auth_test`` at the top of
        each iteration when it's still None — so a Slack-side outage
        that 503s the initial lookup heals on the next iteration where
        Slack accepts the call.
        """
        attempt = 0
        backoff = self._RECONNECT_BACKOFF_INITIAL_SECONDS
        while True:
            # Best-effort refresh of the bot user id. Self-message
            # filtering depends on this; if the initial lookup at
            # connect-time 503'd, this re-runs every retry until it
            # succeeds.
            await self._refresh_bot_user_id()
            try:
                assert self._handler is not None
                await self._handler.start_async()
                # Clean exit (e.g. operator-initiated close_async).
                log.info(
                    "SlackBridge supervisor: handler.start_async() "
                    "returned cleanly; exiting"
                )
                return
            except asyncio.CancelledError:
                raise
            except SlackApiError as exc:
                err_code = ""
                resp = getattr(exc, "response", None)
                if resp is not None:
                    try:
                        err_code = (
                            resp.get("error") if isinstance(resp, dict)
                            else getattr(resp, "data", {}).get("error", "")
                        ) or ""
                    except Exception:  # noqa: BLE001
                        err_code = ""
                # Operator-actionable failure modes: don't retry.
                fatal_codes = {
                    "invalid_auth", "token_revoked", "account_inactive",
                    "missing_scope", "not_authed",
                }
                if err_code in fatal_codes:
                    log.error(
                        "SlackBridge: terminal auth failure (%s); supervisor exiting",
                        err_code,
                    )
                    event_kind = (
                        "slack_bridge_scope_failure"
                        if err_code == "missing_scope"
                        else "slack_bridge_auth_failure"
                    )
                    asyncio.create_task(_safe_log_event(
                        event_kind,
                        slack_error=err_code,
                        error=str(exc)[:300],
                    ))
                    raise
                attempt += 1
                log.warning(
                    "SlackBridge: transient SlackApiError (%s) on attempt %d; "
                    "retrying in %.1fs",
                    err_code or exc, attempt, backoff,
                )
                if _should_emit_retry_algedonic(attempt):
                    asyncio.create_task(_safe_log_event(
                        "slack_bridge_retry",
                        attempt=attempt,
                        backoff_seconds=round(backoff, 1),
                        slack_error=err_code,
                        error=f"SlackApiError: {str(exc)[:200]}",
                    ))
            except Exception as exc:  # noqa: BLE001
                # Unexpected exception (network errors from aiohttp,
                # WebSocket disconnect storms, slack_sdk internals).
                # Treat as transient — same posture as Discord supervisor.
                attempt += 1
                log.warning(
                    "SlackBridge: unexpected exception (%s) on attempt %d; "
                    "retrying in %.1fs",
                    exc, attempt, backoff,
                )
                if _should_emit_retry_algedonic(attempt):
                    asyncio.create_task(_safe_log_event(
                        "slack_bridge_retry",
                        attempt=attempt,
                        backoff_seconds=round(backoff, 1),
                        error=f"{type(exc).__name__}: {str(exc)[:200]}",
                    ))

            await asyncio.sleep(backoff)
            backoff = min(
                backoff * 2.0, self._RECONNECT_BACKOFF_CAP_SECONDS,
            )

            # Build a fresh handler. The previous one may be in a
            # half-open state after a failed handshake.
            try:
                if self._handler is not None:
                    await self._handler.close_async()
            except Exception:  # noqa: BLE001
                pass
            assert self._app is not None
            self._handler = AsyncSocketModeHandler(self._app, self.app_token)

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
        *,
        final: bool = True,
    ) -> SendResult:
        # chainlink #5: Slack has no public typing-indicator API for
        # bots (chat.assistant.threads.setStatus is App Assistant-only),
        # so ``final`` is informational. Streaming plan + result land
        # as two separate Slack messages naturally.
        del final
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

    async def send_typing_indicator(self, channel_id: str) -> None:
        """No-op. Slack has no public typing API for bots — the
        ``chat.assistant.threads.setStatus`` call is App Assistant-only,
        not general chat. If we want a typing stand-in later, the
        cleanest path is auto-react with 👀 on the user's message and
        remove it when the response goes out, but that fires reaction
        events back to the agent and adds its own UX wrinkles."""
        return None

    async def fetch_history(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> list[ChannelMessage]:
        """Fetch recent messages from a Slack channel via
        ``conversations.history``. Returns oldest-first. Slack's API
        accepts up to 1000 per call but recommends ≤200; we cap at
        100 to match Discord's behavior and keep the call cheap.

        Requires the bot's OAuth token to have ``channels:history``
        / ``groups:history`` / ``im:history`` / ``mpim:history``
        depending on the channel type. Missing scope raises
        ``SlackApiError`` (``not_in_channel`` / ``missing_scope``)
        which propagates to the caller.
        """
        if self._app is None:
            return []
        slack_channel = _channel_id_to_slack(channel_id)
        if slack_channel is None:
            return []
        limit = max(1, min(int(limit), 100))

        params: dict[str, Any] = {"channel": slack_channel, "limit": limit}
        if before:
            # Slack's ``latest`` cursor: messages with ts <= latest. We
            # subtract a microsecond's worth of ts so the cursor message
            # itself isn't re-included on the next page.
            params["latest"] = before
            params["inclusive"] = False

        try:
            resp = await self._app.client.conversations_history(**params)
        except SlackApiError as exc:
            log.warning(
                "SlackBridge.fetch_history failed for %s: %s",
                slack_channel, exc,
            )
            raise

        messages = resp.get("messages") or []
        out: list[ChannelMessage] = []
        # Slack returns newest-first; reverse for oldest-first output.
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            user_id = m.get("user") or m.get("bot_id")
            ts = m.get("ts") or ""
            # Slack ts is "<unix-seconds>.<microseconds>" — convert to
            # ISO-8601 for the uniform shape.
            iso_ts = ""
            if ts:
                try:
                    iso_ts = datetime.fromtimestamp(
                        float(ts), tz=timezone.utc,
                    ).isoformat()
                except (ValueError, OSError):
                    iso_ts = ts
            attachments = m.get("files") or []
            attachment_urls = tuple(
                str(a.get("url_private") or a.get("url_private_download") or "")
                for a in attachments
                if isinstance(a, dict)
                and (a.get("url_private") or a.get("url_private_download"))
            )
            # Resolve a friendly name for guild-channel messages. DMs
            # have user ids only; users.info hits a 1-call cache.
            author_display: str | None = user_id
            if user_id and not user_id.startswith("B"):  # not a bot id
                info = await self._user_info_cached(user_id)
                if info:
                    author_display = (
                        info.get("real_name")
                        or info.get("display_name")
                        or user_id
                    )
            out.append(ChannelMessage(
                id=str(ts),
                ts=iso_ts,
                author_id=user_id,
                author_display=author_display,
                is_bot=bool(m.get("bot_id")),
                content=str(m.get("text", "") or ""),
                attachment_urls=attachment_urls,
                extra={"thread_ts": m.get("thread_ts")} if m.get("thread_ts") else {},
            ))
        return out

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

    async def _user_info_cached(self, user_id: str) -> dict[str, str | None] | None:
        """Fetch (or return cached) Slack user profile.

        Returns ``{real_name, display_name, email}`` on success, ``None`` on
        API failure. Successful lookups are cached for the process lifetime
        so we hit ``users.info`` at most once per distinct user.

        Failures aren't cached — if the operator adds the ``users:read`` /
        ``users:read.email`` scope post-deploy, the next message from that
        user picks up the new permissions automatically. Slack's
        ``users.info`` is Tier 4 (100 req/min); per-user caching keeps us
        well under that even on chatty channels.
        """
        cached = self._user_cache.get(user_id)
        if cached is not None:
            return cached
        if self._app is None:
            return None
        try:
            resp = await self._app.client.users_info(user=user_id)
        except SlackApiError as exc:
            log.warning("slack users.info failed for %s: %s", user_id, exc)
            return None
        user = resp.get("user") or {}
        profile = user.get("profile") or {}
        info: dict[str, str | None] = {
            "real_name": user.get("real_name") or profile.get("real_name"),
            "display_name": profile.get("display_name") or user.get("name"),
            "email": profile.get("email"),
        }
        self._user_cache[user_id] = info
        return info

    def _register_handlers(self, app: Any) -> None:
        async def on_message(event: dict[str, Any]) -> None:
            await self._on_message(event)

        async def on_app_mention(event: dict[str, Any]) -> None:
            # app_mention events overlap with regular message events for
            # mention-bearing messages. Letting both paths fire would
            # double-enqueue. We rely on the message handler for content
            # and just log the mention as observability.
            log.debug("slack app_mention from %s in %s", event.get("user"), event.get("channel"))

        async def on_reaction_added(event: dict[str, Any]) -> None:
            await self._on_reaction(event)

        # The slack-bolt decorator registers under the underlying app.
        app.message(on_message)
        app.event("app_mention")(on_app_mention)
        app.event("reaction_added")(on_reaction_added)

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

        # chainlink #232: dedup before any work — Slack Socket Mode
        # redelivers events on ACK loss; without the cache we'd burn a
        # turn (plus an attachment download) on every redelivery.
        source_id = event.get("ts")
        if source_id and not self._seen_ids.add_if_new(source_id):
            log.debug(
                "SlackBridge: duplicate inbound message dropped "
                "(source_id=%s) — Socket-Mode redelivery",
                source_id,
            )
            return

        slack_channel = event.get("channel") or ""
        channel_type = event.get("channel_type")
        channel_id = _slack_channel_to_id(slack_channel, channel_type)
        is_dm = _is_dm_channel(slack_channel, channel_type)

        text = (event.get("text") or "").strip()
        if not text:
            text = "User sent a message with no text."

        # Platform-prefixed stable id is the matching key for cross-channel
        # / cross-platform pull (FUTURE_WORK §6.1).
        author_key = f"slack-{user_id}"

        # Enrich author_display + capture email via users.info if available.
        # Falls back to user_id when the lookup fails (e.g., users:read
        # scope missing). Email lands in event.extra for any future use
        # (identity proposal flow, EmailBridge cross-reference, etc.).
        author_display = user_id
        slack_email: str | None = None
        info = await self._user_info_cached(user_id)
        if info:
            author_display = (
                info.get("real_name") or info.get("display_name") or user_id
            )
            slack_email = info.get("email")

        # Inbound file_share attachments — Slack's ``files`` array on
        # the event has url_private (Bot-token-auth) which we can stream
        # to disk. Skipped when attachments_dir isn't configured.
        attachment_paths: list[str] = []
        attachment_urls: list[str] = []
        for f in (event.get("files") or []):
            if not isinstance(f, dict):
                continue
            url = f.get("url_private") or f.get("url_private_download")
            name = f.get("name") or f.get("id") or "attachment"
            size = f.get("size")
            if url:
                attachment_urls.append(str(url))
            if not (self.attachments_dir and url):
                continue
            if (
                self.attachments_max_bytes is not None
                and isinstance(size, int)
                and size > self.attachments_max_bytes
            ):
                log.warning(
                    "SlackBridge: attachment %s (%d bytes) exceeds "
                    "max_bytes=%s; skipping download",
                    name, size, self.attachments_max_bytes,
                )
                continue
            target = build_inbound_path(
                self.attachments_dir,
                channel="slack",
                chat_id=str(slack_channel or ""),
                filename=str(name),
            )
            # Slack's url_private requires the bot token in the
            # Authorization header. Use the shared download_to_path
            # helper so the streaming-size cap is enforced (no unbounded
            # disk write if the endpoint streams more than advertised).
            ok = await download_to_path(
                str(url),
                target,
                max_bytes=self.attachments_max_bytes,
                headers={"Authorization": f"Bearer {self.bot_token}"},
            )
            if ok:
                attachment_paths.append(str(target))

        agent_event = AgentEvent(
            trigger="user_message",
            channel_id=channel_id,
            content=text,
            author=author_key,
            author_display=author_display,
            author_id=user_id,
            source_id=event.get("ts"),
            source="slack",
            attachment_names=attachment_paths,
            extra={
                "channel_conversation_type": "dm" if is_dm else "multi_user",
                "channel_visibility": "private" if is_dm else "public",
                "thread_ts": event.get("thread_ts"),
                "slack_email": slack_email,
                **(
                    {"inbound_attachment_urls": attachment_urls}
                    if attachment_urls else {}
                ),
            },
        )
        await self.enqueue(agent_event)

    # VSM: algedonic (in) — see DiscordBridge._on_reaction; identical
    #                       semantics, different protocol.
    # loop_id: 2.6
    async def _on_reaction(self, event: dict[str, Any]) -> None:
        """Surface inbound reactions on the bot's messages as
        ``react_received`` events. mimir.feedback's algedonic block
        (24h window) picks them up and shows them in the next turn's
        prompt under "Recent feedback signals."

        Slack delivers ``reaction_added`` events for every reaction in
        every channel the app is in — we filter to:
          - reactions the bot itself didn't add (not own-tool feedback)
          - reactions on the bot's own messages (signal about us)
        """
        from ..event_logger import log_event
        from ..reactions import classify_reaction, normalize_emoji

        user_id = event.get("user")
        if user_id is None or (self._bot_user_id and user_id == self._bot_user_id):
            return

        item = event.get("item") or {}
        target_user = (item.get("item_user")
                       or event.get("item_user"))  # older payloads
        if target_user is None or target_user != self._bot_user_id:
            return

        slack_channel = item.get("channel") or ""
        # We don't always know the channel_type from the reaction
        # payload; fall back to the public-channel encoding. Bench
        # bridges never get reaction events anyway.
        channel_id = _slack_channel_to_id(slack_channel, None)

        emoji_raw = event.get("reaction") or ""
        emoji_glyph = normalize_emoji(emoji_raw)
        polarity = classify_reaction(emoji_glyph)

        target_age_minutes: float | None = None
        target_ts = item.get("ts")
        if target_ts:
            try:
                from datetime import datetime, timezone
                target_age_minutes = (
                    datetime.now(tz=timezone.utc).timestamp() - float(target_ts)
                ) / 60.0
            except (ValueError, TypeError):
                pass

        try:
            await log_event(
                "react_received",
                bridge=self.name,
                channel_id=channel_id,
                emoji=emoji_glyph,
                polarity=polarity,
                action="add",
                author=f"slack-{user_id}",
                target_message_id=target_ts,
                target_age_minutes=target_age_minutes,
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "SlackBridge",
    "SLACK_MESSAGE_CHAR_LIMIT",
    "_chunk_message",
    "_slack_channel_to_id",
    "_channel_id_to_slack",
    "_is_dm_channel",
    "_normalize_emoji",
]
