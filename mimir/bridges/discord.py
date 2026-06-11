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
import time
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

from ..background_tasks import spawn_background
from ..models import AgentEvent
from ._emoji import resolve_for_discord
from ._history import ChannelMessage
from ._seen_ids import SeenIdCache
from .base import Bridge, SendResult

log = logging.getLogger(__name__)

DISCORD_MESSAGE_CHAR_LIMIT = 2000


# Fatal-exception map: classes that mean "operator must intervene"
# (token rotation, intent enable in dev portal). Built from
# ``getattr`` lookups at module load so a discord-py version that
# doesn't expose one of these classes drops it from the tuple cleanly —
# without the ``except`` clause silently degrading to ``except
# Exception:`` and blocking the retryable handlers below it.
_FATAL_DISCORD_EXCEPTION_INFO: dict[type[BaseException], tuple[str, str]] = {
    cls: (event_kind, log_msg)
    for cls, event_kind, log_msg in (
        (getattr(discord, "LoginFailure", None),
         "discord_bridge_login_failure",
         "token auth permanently rejected"),
        (getattr(discord, "PrivilegedIntentsRequired", None),
         "discord_bridge_intents_failure",
         "privileged intents required — enable members + message_content "
         "in the Discord developer portal"),
    )
    if cls is not None
}
_FATAL_DISCORD_EXCEPTIONS: tuple[type[BaseException], ...] = tuple(
    _FATAL_DISCORD_EXCEPTION_INFO
)


# chainlink #246: throttle + safe-log helpers are shared with SlackBridge
# in mimir/bridges/_supervisor.py. Bind locally with a bridge label so
# any logger-side failure shows up under the right name.
from ._supervisor import should_emit_retry_algedonic as _should_emit_retry_algedonic
from ._supervisor import safe_log_event as _shared_safe_log_event
from ._supervisor import reset_backoff_if_session_was_healthy as _reset_backoff_if_healthy


async def _safe_log_event(event_kind: str, **fields: Any) -> None:
    """Discord-flavored wrapper — preserves the prior log-message
    prefix while delegating to the shared implementation."""
    await _shared_safe_log_event("DiscordBridge", event_kind, **fields)


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
        attachments_dir: when set, inbound message attachments are
            downloaded under
            ``<dir>/discord/<channel_id>/<msg_id>/<ts>-<uuid>-<name>``
            and the resulting paths populate ``event.attachment_names``.
            Default None — attachments arrive on the event with their
            URLs in ``extra["inbound_attachment_urls"]`` only.
        attachments_max_bytes: per-file cap; oversized files skip the
            download. None (default) = no cap.
    """

    token: str
    enqueue: Callable[[AgentEvent], Awaitable[bool]]
    respond_to_bots: bool = False
    attachments_dir: Path | None = None
    attachments_max_bytes: int | None = None
    _client: _DiscordClient | None = field(default=None, init=False, repr=False)
    _runner: asyncio.Task | None = field(default=None, init=False, repr=False)
    _background_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set, init=False, repr=False
    )
    # Per-channel "hold typing open" tasks, created on inbound and cancelled
    # on outbound (or when the agent loop calls ``cancel_typing`` after a
    # turn finishes). See ``send_typing_indicator``.
    _typing_tasks: dict[str, asyncio.Task] = field(
        default_factory=dict, init=False, repr=False
    )
    # chainlink #232: inbound message dedup — Discord's gateway resume
    # protocol redelivers around disconnects, which would otherwise cause
    # the agent to run two turns answering the same message. Bounded LRU
    # keyed on the Discord message id (a globally unique snowflake).
    _seen_ids: SeenIdCache = field(
        default_factory=SeenIdCache,
        init=False, repr=False,
    )

    prefixes = ("discord-", "dm-discord-")
    name = "discord"

    # Hard cap on a single typing-hold task. Cancellation normally happens on
    # ``send()`` or ``cancel_typing()``; the cap protects against turns that
    # never call either (errored turns, cross-channel-only sends if the
    # agent loop's ``turn_finished`` cleanup ever regresses). 10 minutes
    # gives longmemeval / long climbs headroom; the cap only matters when
    # cancellation never arrives.
    _TYPING_HOLD_TIMEOUT_SECONDS: float = 600.0

    # Reconnect backoff schedule for the supervisor wrapping
    # ``client.start()``. Discord 5xx incidents typically recover in
    # minutes-to-hours; a 5-minute cap keeps retry cadence reasonable
    # without spamming the bridge during a sustained outage. Initial
    # delay is short so a one-off transient resolves fast; doublings
    # ramp the back-pressure as the outage persists.
    _RECONNECT_BACKOFF_INITIAL_SECONDS: float = 5.0
    _RECONNECT_BACKOFF_CAP_SECONDS: float = 300.0

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = _DiscordClient(self)
        # ``client.start`` is a long-running coroutine. Wrap it in a
        # supervisor that retries on transient failure (Discord 5xx at
        # token-auth, network errors during initial handshake) so a
        # gateway outage at restart-time doesn't permanently kill the
        # bridge for the rest of the container's life. Operator-actionable
        # errors (LoginFailure = bad token, PrivilegedIntentsRequired =
        # missing intent) propagate up — retrying those just spams.
        self._runner = self._spawn_background(
            self._supervised_run(), name="mimir-discord-runner"
        )

    def _spawn_background(
        self, coro: Awaitable[Any], *, name: str | None = None,
    ) -> asyncio.Task[Any]:
        return spawn_background(self._background_tasks, coro, name=name)

    async def _supervised_run(self) -> None:
        """Retry-with-exponential-backoff wrapper around
        ``self._client.start(self.token)``.

        Retryable: 5xx server errors, generic connection / network errors
        from discord.py and aiohttp. Backoff: 5s → 10s → 20s → 40s → 80s,
        capped at ``_RECONNECT_BACKOFF_CAP_SECONDS``. Retries
        indefinitely — Discord's longest documented outage is hours, and
        a wedged-bridge-for-the-rest-of-the-container's-life is worse
        than a noisy retry log.

        Non-retryable (raise out, runner task ends):
        - ``LoginFailure`` (bad token) — operator must rotate
        - ``PrivilegedIntentsRequired`` — config gap, operator must enable
          in Discord developer portal
        - ``CancelledError`` — clean shutdown via ``disconnect()``

        Fires ``discord_bridge_retry`` (algedonic-negative) on attempts
        3-9 inclusive, then every 10th attempt thereafter — see
        ``_should_emit_retry_algedonic``. Surfaces the early "is this
        real?" signal fast, then throttles during sustained outages so
        events.jsonl doesn't fill with retry rows during a 24h
        Discord-side incident.
        """
        attempt = 0
        backoff = self._RECONNECT_BACKOFF_INITIAL_SECONDS
        while True:
            started_at = time.monotonic()
            try:
                # ``client.start`` returns when the gateway disconnects
                # cleanly OR raises on transient/fatal failure. discord.py
                # handles intra-session WebSocket reconnects internally
                # via the resume protocol; this loop only matters when
                # ``start`` itself returns/raises (initial handshake
                # failure or unrecoverable disconnect).
                assert self._client is not None
                await self._client.start(self.token)
                # Clean exit (e.g. operator-initiated disconnect via
                # ``client.close()``). Don't loop — exit the supervisor.
                log.info(
                    "DiscordBridge supervisor: client.start() returned cleanly; exiting"
                )
                return
            except asyncio.CancelledError:
                raise
            except _FATAL_DISCORD_EXCEPTIONS as exc:
                # Operator-actionable: token rotation needed (LoginFailure)
                # or intents must be enabled in the dev portal
                # (PrivilegedIntentsRequired). Map back to the per-class
                # event kind + log message via the module-level info dict.
                event_kind = "discord_bridge_unknown_fatal"
                log_msg = "fatal exception"
                for cls, (kind, msg) in _FATAL_DISCORD_EXCEPTION_INFO.items():
                    if isinstance(exc, cls):
                        event_kind = kind
                        log_msg = msg
                        break
                log.error(
                    "DiscordBridge: %s (%s); supervisor exiting",
                    log_msg, exc,
                )
                # Fire-and-forget so we don't block the supervisor on a
                # logger that might itself be backing off.
                self._spawn_background(
                    _safe_log_event(event_kind, error=str(exc)[:300]),
                    name=f"mimir-discord-log-{event_kind}",
                )
                raise
            except (discord.HTTPException, discord.ConnectionClosed) as exc:
                # ``HTTPException`` is the parent of ``DiscordServerError``
                # so the 5xx-at-token-auth case (the production failure
                # mode that motivated this) lands here. ``ConnectionClosed``
                # covers post-handshake gateway disconnects that
                # discord.py's resume protocol couldn't fix.
                attempt, backoff = _reset_backoff_if_healthy(
                    time.monotonic() - started_at, attempt, backoff,
                    initial_backoff=self._RECONNECT_BACKOFF_INITIAL_SECONDS,
                )
                attempt += 1
                log.warning(
                    "DiscordBridge: transient failure (%s) on attempt %d; "
                    "retrying in %.1fs",
                    exc, attempt, backoff,
                )
                if _should_emit_retry_algedonic(attempt):
                    self._spawn_background(
                        _safe_log_event(
                            "discord_bridge_retry",
                            attempt=attempt,
                            backoff_seconds=round(backoff, 1),
                            error=f"{type(exc).__name__}: {str(exc)[:200]}",
                        ),
                        name="mimir-discord-log-retry",
                    )
            except Exception as exc:  # noqa: BLE001
                # Catch-all for unexpected exception types (network errors
                # from aiohttp surface as multiple class hierarchies). Treat
                # as transient — Discord-down looks the same to the bot
                # whether it's a discord-py exception or a deeper aiohttp
                # one. Surface but retry.
                attempt, backoff = _reset_backoff_if_healthy(
                    time.monotonic() - started_at, attempt, backoff,
                    initial_backoff=self._RECONNECT_BACKOFF_INITIAL_SECONDS,
                )
                attempt += 1
                log.warning(
                    "DiscordBridge: unexpected exception (%s) on attempt %d; "
                    "retrying in %.1fs",
                    exc, attempt, backoff,
                )
                if _should_emit_retry_algedonic(attempt):
                    self._spawn_background(
                        _safe_log_event(
                            "discord_bridge_retry",
                            attempt=attempt,
                            backoff_seconds=round(backoff, 1),
                            error=f"{type(exc).__name__}: {str(exc)[:200]}",
                        ),
                        name="mimir-discord-log-retry",
                    )

            # Sleep before retry. ``CancelledError`` here aborts the loop
            # cleanly via the outer except clause.
            await asyncio.sleep(backoff)
            backoff = min(
                backoff * 2.0, self._RECONNECT_BACKOFF_CAP_SECONDS,
            )

            # The previous client may be in a half-closed state after a
            # failed login. Construct a fresh one for the next attempt.
            try:
                if not self._client.is_closed():
                    await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = _DiscordClient(self)

    async def disconnect(self) -> None:
        # Cancel any dangling typing-hold tasks first so they don't try to
        # POST against a closing client. Each task swallows its own
        # CancelledError on the way out (see ``send_typing_indicator._hold``).
        for task in list(self._typing_tasks.values()):
            if not task.done():
                task.cancel()
        self._typing_tasks.clear()

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
        *,
        final: bool = True,
    ) -> SendResult:
        # Cancel any in-flight typing-hold task on this channel before
        # the actual reply goes out — but only on the FINAL flush.
        # Discord drops the indicator within ~10s of the last refresh,
        # so this gets the dots out of the user's face right around
        # the time their message arrives. Programmatic sends to a
        # channel that never had a typing task (scheduled ticks,
        # alerts) hit the no-op branch — safe.
        #
        # chainlink #5: when ``final=False`` the bridge is delivering a
        # mid-turn "plan" chunk with more work still queued — keep
        # the typing indicator held so the user sees "still working"
        # rather than "done." The next ``send(final=True)`` (the
        # result flush) cancels it normally.
        if final:
            await self.cancel_typing(channel_id)

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

    async def send_typing_indicator(self, channel_id: str) -> None:
        """Hold the Discord typing indicator open until ``send()`` /
        ``cancel_typing()`` is called for this channel (or the hard
        cap, whichever first). Replaces the previous fire-and-forget
        single-POST shape, which dropped the indicator after Discord's
        ~10s TTL even when the bot was still working.

        discord.py 2.x's ``channel.typing()`` is an async context
        manager that auto-refreshes the typing POST every ~9s while
        the caller stays inside the ``async with``. We spawn a
        per-channel asyncio task that enters the context and sleeps
        until cancelled, then exits cleanly. The next 9s tick is
        skipped after cancellation, so Discord drops the indicator
        within ~10s of cancel — i.e. right around when the actual
        reply lands.

        Failures are swallowed; typing is a UX nicety, not
        load-bearing. Repeated calls on the same channel cancel any
        prior task — most-recent-inbound wins."""
        # Replace any prior typing task on this channel.
        prior = self._typing_tasks.pop(channel_id, None)
        if prior is not None and not prior.done():
            prior.cancel()

        if self._client is None or self._client.is_closed():
            return
        cid_int = _channel_id_to_int(channel_id)
        if cid_int is None:
            return

        timeout_seconds = self._TYPING_HOLD_TIMEOUT_SECONDS

        async def _hold() -> None:
            try:
                channel = self._client.get_channel(cid_int) if self._client else None
                if channel is None and self._client is not None:
                    channel = await self._client.fetch_channel(cid_int)
                typing_ctx = getattr(channel, "typing", None) if channel else None
                if typing_ctx is None:
                    return
                async with typing_ctx():
                    # discord.py's ``Typing`` schedules its own auto-refresh
                    # task on aenter; we just need to stay inside the
                    # context until cancelled (or the hard cap fires).
                    await asyncio.sleep(timeout_seconds)
            except asyncio.CancelledError:
                # Standard practice — let the cancel propagate so the
                # ``async with`` exits cleanly (which cancels discord.py's
                # internal refresh task).
                raise
            except Exception:  # noqa: BLE001
                # Typing is best-effort; never raise into the bridge loop.
                pass

        task = asyncio.create_task(_hold(), name=f"typing-hold-{channel_id}")
        self._typing_tasks[channel_id] = task

    async def cancel_typing(self, channel_id: str) -> None:
        """Cancel the in-flight typing-hold task for ``channel_id``,
        if any. Safe to call when no task exists (programmatic sends,
        scheduled ticks). Called from ``send()`` automatically and
        from the agent loop on ``turn_finished`` (so cross-channel
        sends and errored turns don't leave the indicator hanging)."""
        prior = self._typing_tasks.pop(channel_id, None)
        if prior is not None and not prior.done():
            prior.cancel()
            # Don't await — the task's finally/aexit runs on the event
            # loop without us blocking here. Awaiting would slow down
            # send() by a tick on the common path.

    async def fetch_history(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> list[ChannelMessage]:
        """Fetch recent messages from a Discord channel via discord-py's
        ``channel.history`` API. Returns oldest-first so the agent reads
        in conversational order. Discord caps at 100 per call; we cap
        at the same regardless of caller-requested ``limit``.
        """
        if self._client is None or self._client.is_closed():
            return []
        cid_int = _channel_id_to_int(channel_id)
        if cid_int is None:
            return []
        # Clamp to Discord's per-call ceiling. Caller can paginate via
        # ``before`` if they want more than 100.
        limit = max(1, min(int(limit), 100))

        channel = self._client.get_channel(cid_int)
        if channel is None:
            channel = await self._client.fetch_channel(cid_int)
        history_fn = getattr(channel, "history", None)
        if not callable(history_fn):
            return []

        kwargs: dict[str, Any] = {"limit": limit}
        if before:
            try:
                kwargs["before"] = discord.Object(id=int(before))
            except ValueError:
                # Bad cursor — ignore and fetch most-recent.
                pass

        out: list[ChannelMessage] = []
        # discord.py's history returns an async iterator newest-first;
        # we collect then reverse for oldest-first output.
        async for msg in history_fn(**kwargs):
            author = getattr(msg, "author", None)
            author_id = str(getattr(author, "id", "") or "") or None
            author_display = (
                getattr(author, "display_name", None)
                or getattr(author, "global_name", None)
                or (str(author) if author is not None else None)
            )
            attachments = getattr(msg, "attachments", None) or []
            attachment_urls = tuple(
                str(getattr(a, "url", "")) for a in attachments
                if getattr(a, "url", None)
            )
            created = getattr(msg, "created_at", None)
            if created is not None:
                if created.tzinfo is None:
                    from datetime import timezone as _tz
                    created = created.replace(tzinfo=_tz.utc)
                ts = created.isoformat()
            else:
                ts = ""
            out.append(ChannelMessage(
                id=str(getattr(msg, "id", "") or ""),
                ts=ts,
                author_id=author_id,
                author_display=author_display,
                is_bot=bool(getattr(author, "bot", False)),
                content=str(getattr(msg, "content", "") or ""),
                attachment_urls=attachment_urls,
            ))
        out.reverse()
        return out

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
            # Alias → glyph resolution (chainlink #412): the prompt
            # documents alias-name acks (``thumbsup`` / ``:thumbsup:``),
            # but Discord's API only accepts unicode glyphs or custom-
            # emoji literals — a raw alias 400s as Unknown Emoji. The
            # resolver landed in ee0e9b9 but its consuming stage was
            # never wired; Slack has its own ``_normalize_emoji``
            # equivalent on its react path.
            await message.add_reaction(resolve_for_discord(emoji))
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

        # chainlink #232: dedup before any work — Discord's resume
        # protocol can redeliver around disconnects, and we don't want
        # to download attachments or burn an agent turn on a redelivery.
        # Use explicit None-check so a hypothetical id=0 (snowflakes
        # never are, but bridge adapters may copy the pattern) still
        # dedupes rather than bypassing the cache.
        _raw_message_id = getattr(message, "id", None)
        source_id = str(_raw_message_id) if _raw_message_id is not None else None
        # Truthy-check matches the Slack bridge pattern; SeenIdCache also
        # treats empty strings as "no id" so this is doubly safe.
        if source_id and not self._seen_ids.add_if_new(source_id):
            log.debug(
                "DiscordBridge: duplicate inbound message dropped "
                "(source_id=%s) — Discord resume-protocol redelivery",
                source_id,
            )
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

        # Download inbound attachments to disk so the agent can Read them
        # by path. URL-only listing is the fallback when attachments_dir
        # isn't configured (or download fails) — the agent then has the
        # URL in extra and can fetch_url itself.
        attachment_paths: list[str] = []
        attachment_urls: list[str] = []
        for att in (getattr(message, "attachments", None) or []):
            url = getattr(att, "url", None)
            name = getattr(att, "filename", None) or getattr(att, "id", None) or "attachment"
            size = getattr(att, "size", None)
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
                    "DiscordBridge: attachment %s (%d bytes) exceeds "
                    "max_bytes=%s; skipping download",
                    name, size, self.attachments_max_bytes,
                )
                continue
            from ._attachments import (
                _DISCORD_CDN_HOSTS,
                build_inbound_path,
                download_to_path,
            )
            target = build_inbound_path(
                self.attachments_dir,
                channel="discord",
                chat_id=str(getattr(message.channel, "id", "") or ""),
                filename=str(name),
            )
            ok = await download_to_path(
                str(url), target, max_bytes=self.attachments_max_bytes,
                allowed_host_suffixes=_DISCORD_CDN_HOSTS,
            )
            if ok:
                attachment_paths.append(str(target))

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
            source_id=source_id,
            source="discord",
            attachment_names=attachment_paths,
            extra={
                "channel_conversation_type": conv_type,
                "channel_visibility": visibility,
                "channel_name": channel_name,
                **(
                    {"inbound_attachment_urls": attachment_urls}
                    if attachment_urls else {}
                ),
            },
        )
        # Fire-and-forget the typing indicator so the user sees the
        # bot "thinking" while the agent spins up. Discord renders
        # the dots for ~10s on a single trigger; for longer turns
        # it'll just expire naturally — that's better than blocking
        # enqueue on a typing call.
        self._spawn_background(
            self.send_typing_indicator(channel_id),
            name=f"mimir-discord-typing-trigger-{channel_id}",
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
