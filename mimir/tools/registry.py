"""All remaining tool ports — completes migration coverage.

Translates the 12 tools across mimir/{channeltools,scheduletools,
committools,spawn}.py to LangChain @tool. Patterns are identical to
extra_tools.py — same translation rule applies (decorator + type
hints + docstring → schema).

Tools ported (12 total):
  channeltools.py:    send_message, react, fetch_channel_history
  scheduletools.py:   list_schedules, add_schedule, remove_schedule,
                       reload_pollers
  committools.py:     commitment_complete, commitment_snooze,
                       commitment_dismiss, commitment_list
  spawn.py:           spawn_claude_code

Plus combined with extra_tools.py (file_search, mimir_get_turn,
shell_exec) and existing memory_tool.py (memory_query) + store_tool.py
(memory_store), that's **17 tools** ported total — complete coverage
of mimir's existing agent-facing surface.

Each tool's dependencies (channel registry, scheduler, commitments
store, spawn config) are injected via module-state setters parallel
to memory_tool.py's set_memory_client pattern.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import asyncio
import logging
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Optional

log = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig

from ..bridges._directives import parse_directives, ReactDirective, resolve_react_target
from ..billing import PRIORITY_LEVELS
from ..poller_budget import aggregate_poller_turn_usage
from ..scheduler import SchedulerJob

# Per-task ContextVar for channel_id — isolated across concurrent asyncio
# Tasks so concurrent turns on different channels don't race (S2-1 fix).
_current_channel_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mimir_current_channel_id", default=None
)

# Per-task ContextVar: is the active turn an interactive (user-facing) one?
# Set by the dispatcher at turn start, paired with the channel id. Retained
# for callers that still thread turn interactivity through the tool registry;
# send_message itself now requires an explicit deliverable channel_id.
_current_turn_interactive_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mimir_current_turn_interactive", default=True
)


def _channel_from_config_or_state(
    channel_id: str | None, config: RunnableConfig | None
) -> str:
    """Resolve the effective channel_id for a tool call.

    Precedence (highest first):
      1. Explicit ``channel_id`` argument from the model
      2. LangGraph ``configurable["channel_id"]`` (set by run_turn)
      3. The per-task ``_current_channel_id_var`` ContextVar (set by the
         dispatcher via ``set_current_channel_id``; isolated across concurrent
         turns — the S2-1 fix replaced the old process-global ``_STATE`` write).

    Returns ``""`` if no source supplies a channel.
    """
    cid = (channel_id or "").strip()
    if cid:
        return cid
    if config is not None:
        configurable = config.get("configurable") or {}
        from_config = (configurable.get("channel_id") or "").strip()
        if from_config:
            return from_config
    return (_current_channel_id_var.get() or "").strip()

from langchain_core.tools import InjectedToolArg, ToolException, tool


_NON_DELIVERABLE_CHANNEL_PREFIXES = ("poller:", "scheduler:")
_NON_DELIVERABLE_CHANNEL_LITERALS = {"system"}


async def _reject_send_message(channel_id: str | None, reason: str) -> None:
    from ..event_logger import safe_log_event

    await safe_log_event(
        "send_message_blocked",
        tool="send_message",
        channel_id=channel_id,
        reason=reason,
    )
    if reason == "empty_message":
        raise ToolException(
            "send_message rejected: empty message. The message text is empty "
            "or whitespace-only; end the turn or send non-empty text."
        )
    raise ToolException(
        "send_message rejected: not a deliverable channel. Provide a real "
        "bridge channel_id such as discord-<id>, dm-discord-<id>, or web-*; "
        f"got {channel_id!r}."
    )


def _is_non_deliverable_channel(channel_id: str) -> bool:
    lowered = channel_id.lower()
    return (
        lowered in _NON_DELIVERABLE_CHANNEL_LITERALS
        or lowered.startswith(_NON_DELIVERABLE_CHANNEL_PREFIXES)
    )


# ────────────────────────────────────────────────────────────────────
# Module-state dependency injection (parallel to memory_tool.py)
# ────────────────────────────────────────────────────────────────────

_STATE: dict[str, Any] = {
    "channel_registry": None,
    "identity_resolver": None,
    "dispatcher": None,
    "scheduler": None,
    "commitments_store": None,
    "spawn_config": None,
    # Slice-3 Worklink autonomy (#444): the HomeostaticArbiter so the in-turn
    # ``worklink_run`` tool can shed autonomous dispatch under TIGHT. Injected by
    # server.py from ``agent._arbiter``; the operator CLI path never sets it, so
    # ``mimir worklink run`` is uncapped/un-gated by design.
    "arbiter": None,
    # chainlink #392: the old "current_channel_id" key was dead — the per-turn
    # channel lives in the _current_channel_id_var ContextVar (set via
    # set_current_channel_id). Removed so nothing reads a never-written key.
}


def set_channel_registry(registry: Any) -> None:
    _STATE["channel_registry"] = registry


def set_identity_resolver(resolver: Any) -> None:
    """Inject the IdentityResolver so ``list_channels`` can surface
    operator-curated channels + captured DM channels. Set by server.py."""
    _STATE["identity_resolver"] = resolver


def set_dispatcher(dispatcher: Any) -> None:
    _STATE["dispatcher"] = dispatcher


def set_scheduler(scheduler: Any) -> None:
    _STATE["scheduler"] = scheduler


def set_commitments_store(store: Any) -> None:
    _STATE["commitments_store"] = store


def set_spawn_config(config: Any) -> None:
    _STATE["spawn_config"] = config


def set_arbiter(arbiter: Any) -> None:
    """Inject the HomeostaticArbiter so ``worklink_run`` can gate autonomous
    dispatch (#444). Set by server.py from the agent's arbiter."""
    _STATE["arbiter"] = arbiter


def set_current_channel_id(channel_id: str | None) -> contextvars.Token:
    """Set the per-task channel_id. Returns a Token; call
    reset_current_channel_id(token) in a finally block to restore.

    ContextVar is isolated per asyncio.Task — concurrent turns on
    different channels don't race. Replaces the old process-global
    _STATE["current_channel_id"] write (S2-1 fix).
    """
    return _current_channel_id_var.set(channel_id)


def reset_current_channel_id(token: contextvars.Token) -> None:
    """Restore the prior channel_id using the Token from set_current_channel_id."""
    _current_channel_id_var.reset(token)


def set_current_turn_interactive(interactive: bool) -> contextvars.Token:
    """Set whether the active turn is interactive (user-facing). Paired with
    ``set_current_channel_id`` at turn start; reset in a finally block."""
    return _current_turn_interactive_var.set(interactive)


def reset_current_turn_interactive(token: contextvars.Token) -> None:
    """Restore the prior interactivity flag using the Token from
    set_current_turn_interactive."""
    _current_turn_interactive_var.reset(token)


# ────────────────────────────────────────────────────────────────────
# Channel tools (mimir/channeltools.py)
# ────────────────────────────────────────────────────────────────────

@tool
async def send_message(
    text: str,
    channel_id: Optional[str] = None,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Emit a message to a channel.

    Requires an explicit deliverable bridge channel_id. Subject to a
    per-turn loop-detection circuit breaker — repeated near-duplicates
    first warn, then refuse.

    Args:
        text: The message body to send.
        channel_id: Target deliverable bridge channel ID.
    """
    if not text or not text.strip():
        await _reject_send_message(channel_id, "empty_message")
    explicit_channel = bool((channel_id or "").strip())
    if not explicit_channel:
        await _reject_send_message(channel_id, "not_deliverable_channel")
    if _is_non_deliverable_channel((channel_id or "").strip()):
        await _reject_send_message(channel_id, "not_deliverable_channel")
    channels = _STATE["channel_registry"]
    if channels is None:
        return "send_message failed: no channel registry configured"
    cid = _channel_from_config_or_state(channel_id, config)
    if not cid:
        return "send_message failed: no channel_id and no current channel"

    # Loop-detection circuit breaker (SPEC §7.2.4). The per-turn
    # LoopDetector lives on the active TurnContext; agent.run_turn
    # attaches it at the start of every turn. Pre-181-J this hook
    # was missing — repeated near-duplicate sends would ship
    # indefinitely. Now: HARD_STOP refuses the send with a recovery
    # hint; SOFT_WARN allows but logs a one-time per-turn warning
    # event so operator dashboards can flag the near-loop.
    from .._context import get_current_turn
    from ..event_logger import log_event as _log_event
    from ..loop_detector import BreakerVerdict

    detector = None
    detector_state = None
    decision = None
    undelivered_decision = None
    ctx = get_current_turn()
    if ctx is not None:
        detector = getattr(ctx, "loop_detector", None)

    bridge = channels.find(cid)
    if bridge is None:
        return f"send_message failed: no bridge for channel {cid!r}"

    if detector is not None:
        undelivered_decision = detector.check_undelivered_backstop(text)
        if undelivered_decision is not None:
            await _log_event(
                "send_message_loop_hard_stop",
                channel_id=cid,
                streak=undelivered_decision.streak,
                similarity=round(undelivered_decision.similarity, 4),
                undelivered=True,
            )
            return (
                "send_message hard stop: repeated near-duplicate "
                "undelivered-send loop. This send is refused before another "
                "delivery attempt. Reflect on the delivery failure before "
                "trying again."
            )
        detector_state = detector.snapshot()
        decision = detector.check(text)
        if decision.verdict == BreakerVerdict.HARD_STOP:
            await _log_event(
                "send_message_loop_hard_stop",
                channel_id=cid,
                streak=decision.streak,
                similarity=round(decision.similarity, 4),
            )
            return (
                "send_message hard stop: repeated near-duplicate loop. "
                "This send is refused. Reflect on what's wrong with the "
                "approach before sending again — try a completely "
                "different tactic or finish the turn."
            )

    # Strip <actions>...</actions> directive blocks from the outbound
    # text and dispatch parsed directives (react, send-file) after send.
    parsed = parse_directives(text)
    clean_text = parsed.clean_text

    result = None
    delivered_by_text = False
    delivered_by_directive = False
    if clean_text:
        try:
            # final=False keeps the typing indicator held — it is released
            # once, at turn end (run_turn's finally), so it persists across
            # multiple send_message calls in a turn. final=True (the default)
            # would cancel typing on the FIRST send (see DiscordBridge.send).
            result = await bridge.send(cid, clean_text, final=False)
        except Exception as exc:
            if detector is not None and detector_state is not None:
                detector.restore(detector_state)
                undelivered_decision = detector.record_undelivered_attempt(text)
                if undelivered_decision.verdict == BreakerVerdict.HARD_STOP:
                    await _log_event(
                        "send_message_loop_hard_stop",
                        channel_id=cid,
                        streak=undelivered_decision.streak,
                        similarity=round(undelivered_decision.similarity, 4),
                        undelivered=True,
                    )
                    return (
                        "send_message hard stop: repeated near-duplicate "
                        "undelivered-send loop. This send failed before "
                        "delivery and further identical retries are refused. "
                        "Reflect on the delivery failure before trying again."
                    )
            return f"send_message failed: {exc}"

        # Soft delivery failure: bridges return SendResult(sent=False, error=…)
        # for recoverable failures (disconnected client, bad channel) instead
        # of raising. With auto-dispatch gone this is the sole reply path, so a
        # soft failure must NOT look delivered — don't log send_message_sent,
        # don't append to history, and surface the failure to the model.
        if not getattr(result, "sent", True):
            if detector is not None and detector_state is not None:
                detector.restore(detector_state)
                undelivered_decision = detector.record_undelivered_attempt(text)
                if undelivered_decision.verdict == BreakerVerdict.HARD_STOP:
                    await _log_event(
                        "send_message_loop_hard_stop",
                        channel_id=cid,
                        streak=undelivered_decision.streak,
                        similarity=round(undelivered_decision.similarity, 4),
                        undelivered=True,
                    )
                    return (
                        "send_message hard stop: repeated near-duplicate "
                        "undelivered-send loop. This send failed before "
                        "delivery and further identical retries are refused. "
                        "Reflect on the delivery failure before trying again."
                    )
            _err = getattr(result, "error", None)
            try:
                await _log_event(
                    "send_message_failed",
                    channel_id=cid,
                    error=(str(_err)[:200] if _err else None),
                )
            except Exception:  # noqa: BLE001
                pass
            return (
                "send_message failed: bridge reported the message was not "
                f"delivered (channel={cid}" + (f"; {_err}" if _err else "") + ")"
            )

        delivered_by_text = True

        # Successful delivery — record it on the turn context so the
        # forgot-to-send guard knows a reply actually went out. An
        # attempted-but-refused / soft-failed send must NOT suppress the
        # no-reply signal, so only a confirmed send increments this.
        if ctx is not None:
            try:
                ctx.send_message_count += 1
                ctx.delivered_channel_ids.add(cid)
            except Exception:  # noqa: BLE001
                pass
        if detector is not None:
            detector.clear_undelivered_attempts()

        # S2-2: log a send_message_sent event with a normalized content hash
        # so FeedbackLog._detect_cross_turn_send_loops can detect 24h floods
        # (same message to same channel across multiple turns / heartbeats).
        # Normalize: lowercase + collapse whitespace; truncate at 500 chars
        # before hashing so two slightly-trimmed variants of a long message
        # compare as equal.
        _norm = re.sub(r"\s+", " ", clean_text.strip()).lower()[:500]
        _content_hash = hashlib.md5(_norm.encode()).hexdigest()[:16]
        try:
            await _log_event("send_message_sent", channel_id=cid, content_hash=_content_hash)
        except Exception:
            pass  # best-effort; don't fail the send if event logging hiccups

        # Append outbound to chat-history buffer so the agent's next
        # turn sees its own reply in Recent activity. Dropped in PR
        # #181's deepagents migration; restoring here closes the
        # regression for the send_message-tool path (the most common
        # outbound path in production). No-op when no buffer is
        # registered (test paths that bypass ``server.serve``).
        #
        # Use ``bridge.name`` (e.g. "discord", "slack") as the source
        # so this message passes the recent_sources allowlist filter
        # in recent_for_channel. Without a source the message is
        # silently excluded from ## Recent activity regardless of
        # channel — the allowlist treats None the same as an
        # unrecognised source. This matters especially for cross-
        # channel sends from poller / heartbeat turns: a poller turn
        # sending to discord needs source="discord" so the operator
        # sees mimir's own messages interleaved with their own in
        # the next discord turn's prompt. (chainlink #270)
        from ..history import get_global_buffer
        _buf = get_global_buffer()
        if _buf is not None and result is not None:
            try:
                msg = _buf.make_message(
                    channel_id=cid,
                    kind="assistant_message",
                    content=clean_text,
                    msg_id=getattr(result, "message_id", None),
                    source=bridge.name,
                )
                await _buf.append(msg)
            except Exception:  # noqa: BLE001
                # Best-effort — don't fail the tool call if the
                # buffer hiccups. Log a warning rather than swallowing
                # silently so disk-full / permission-denied issues
                # are visible in events.jsonl downstream.
                log.warning(
                    "send_message: chat_history append failed", exc_info=True,
                )

    for _directive in parsed.directives:
        if isinstance(_directive, ReactDirective):
            # chainlink #394: resolve the target via the shared helper and
            # skip on None rather than calling bridge.react(cid, None, emoji).
            # The just-sent message is the natural target; a directives-only
            # send_message (empty clean_text → result None) has no target.
            _target = resolve_react_target(
                _directive.message_id,
                result.message_id if result else None,
            )
            if _target is None:
                log.debug(
                    "send_message react directive skipped: no target message "
                    "for emoji %r", _directive.emoji,
                )
                continue
            # chainlink #408: a directive react is a real delivery — it
            # feeds the same accounting as the standalone ``react`` tool
            # (0.3.2's react_count fix covered only the tool; an
            # actions-only body increments NO send count, so a delivered
            # ack still tripped the forgot-to-send guard), and its
            # failures surface algedonically (the prompt promises
            # per-directive failures show up in the feedback block —
            # false while this was a bare except-pass).
            _ok: object = False
            try:
                _ok = await bridge.react(cid, _target, _directive.emoji)
            except Exception as _exc:  # noqa: BLE001 — non-fatal; don't abort the send
                await _log_event(
                    "send_message_directive_failed",
                    channel_id=cid,
                    directive="react",
                    emoji=_directive.emoji,
                    message_id=_target,
                    error=f"{type(_exc).__name__}: {_exc}"[:200],
                )
            else:
                if _ok is False:
                    await _log_event(
                        "send_message_directive_failed",
                        channel_id=cid,
                        directive="react",
                        emoji=_directive.emoji,
                        message_id=_target,
                        error="bridge declined",
                    )
            # Confirmed-delivery gate mirrors the react tool: only a
            # non-False return counts toward the turn's reply accounting.
            if _ok is not False:
                delivered_by_directive = True
                if ctx is not None:
                    try:
                        ctx.react_count += 1
                        ctx.delivered_channel_ids.add(cid)
                    except Exception:  # noqa: BLE001
                        pass
                if detector is not None:
                    detector.clear_undelivered_attempts()
        # SendFileDirective: not yet implemented via this path

    if (
        detector is not None
        and detector_state is not None
        and not delivered_by_text
        and not delivered_by_directive
    ):
        detector.restore(detector_state)
        undelivered_decision = detector.record_undelivered_attempt(text)
        if undelivered_decision.verdict == BreakerVerdict.HARD_STOP:
            await _log_event(
                "send_message_loop_hard_stop",
                channel_id=cid,
                streak=undelivered_decision.streak,
                similarity=round(undelivered_decision.similarity, 4),
                undelivered=True,
            )
            return (
                "send_message hard stop: repeated near-duplicate "
                "undelivered-send loop. This send failed before delivery and "
                "further identical retries are refused. Reflect on the "
                "delivery failure before trying again."
            )
    elif (
        detector is not None
        and decision is not None
        and decision.verdict == BreakerVerdict.SOFT_WARN
        and detector.mark_warning_emitted()
    ):
        await _log_event(
            "send_message_loop_warning",
            channel_id=cid,
            streak=decision.streak,
            similarity=round(decision.similarity, 4),
        )

    # chainlink #259: surface the bare message_id, not the SendResult repr,
    # so downstream parses/greps (e.g. a later react(message_id=...)) work.
    _mid = getattr(result, "message_id", None) if result else None
    return f"send_message ok: channel={cid} message_id={_mid}"


send_message.handle_tool_error = True


def _resolve_recent_message_id(channel_id: str) -> Optional[str]:
    """Most recent buffered message on ``channel_id`` that carries an id.

    Backs ``react``'s default target. Returns None when no buffer is
    registered (test paths that bypass ``server.serve``) or the channel
    has no id-bearing message in the recent window. ``recent_for_channel``
    pools across public channels, so we filter to the requested channel
    and walk newest-first for the first message with a usable id.
    """
    from ..history import get_global_buffer
    buf = get_global_buffer()
    if buf is None:
        return None
    recent = buf.recent_for_channel(channel_id, limit=50)
    for msg in reversed(recent):
        if msg.channel_id == channel_id and msg.msg_id:
            return msg.msg_id
    return None


@tool
async def react(
    emoji: str,
    message_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """React to a message with an emoji.

    When ``message_id`` is omitted, defaults to the most recent message
    on the channel that carries an id — usually the message being
    acknowledged — resolved from the shared history buffer. Bridges that
    don't support native reactions (e.g. Bluesky) log a no-op.

    Args:
        emoji: Reaction emoji (e.g. "👍").
        message_id: Specific message to react to. Defaults to the most
            recent id-bearing message on the channel.
        channel_id: Channel scope. Defaults to current turn's.
    """
    channels = _STATE["channel_registry"]
    if channels is None:
        return "react failed: no channel registry configured"
    cid = _channel_from_config_or_state(channel_id, config)
    if not cid:
        return "react failed: no channel_id and no current channel"
    bridge = channels.find(cid)
    if bridge is None:
        return f"react failed: no bridge for channel {cid!r}"
    # chainlink #259 item 11: the str-typed bridge.react raises / no-ops
    # on a None message_id, and the documented "most recent" default was
    # never actually implemented (None was forwarded verbatim). Resolve
    # it here from the shared history buffer instead.
    if message_id is None:
        message_id = _resolve_recent_message_id(cid)
        if message_id is None:
            return (
                f"react failed: no message_id given and no recent "
                f"id-bearing message on channel {cid} to default to — "
                f"pass message_id explicitly"
            )
    try:
        ok = await bridge.react(cid, message_id, emoji)
    except Exception as exc:
        return f"react failed: {exc}"
    # Bridges signal a declined reaction (bad id, missing scope, …) with
    # a False return rather than an exception. Don't report "ok" on it.
    if ok is False:
        return (
            f"react failed: bridge declined "
            f"(channel={cid}, message_id={message_id}, emoji={emoji})"
        )
    # A successful react is a valid interactive response (an acknowledgment) —
    # record it on the turn so the forgot-to-send guard doesn't flag a
    # react-only reply as "no reply" (0.3.2).
    from .._context import get_current_turn
    _ctx = get_current_turn()
    if _ctx is not None:
        try:
            _ctx.react_count += 1
            _ctx.delivered_channel_ids.add(cid)
        except Exception:  # noqa: BLE001
            pass
    return f"react ok: channel={cid} emoji={emoji} message_id={message_id}"


@tool
async def fetch_channel_history(
    channel_id: Optional[str] = None,
    limit: int = 20,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Fetch recent messages from a channel.

    Args:
        channel_id: Channel to read. Defaults to current turn's.
        limit: Max messages to return (1-100, default 20).
    """
    channels = _STATE["channel_registry"]
    if channels is None:
        return "fetch_channel_history failed: no channel registry"
    cid = _channel_from_config_or_state(channel_id, config)
    if not cid:
        return "fetch_channel_history failed: no channel_id and no current"
    try:
        k = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        k = 20
    bridge = channels.find(cid)
    if bridge is None or not hasattr(bridge, "fetch_history"):
        return f"fetch_channel_history failed: bridge {cid!r} doesn't support history"
    try:
        history = await bridge.fetch_history(cid, limit=k)
    except Exception as exc:
        return f"fetch_channel_history failed: {exc}"
    return json.dumps(history, indent=2, ensure_ascii=False, default=str)


@tool
async def list_channels(platform: Optional[str] = None) -> str:
    """List the channels you know about — use this to find where to send,
    especially to DM a person by name. Read-only.

    Args:
        platform: Optional bridge filter — e.g. ``"slack"`` or ``"discord"``.
            When set, returns only that bridge's channels/DMs/prefixes
            (matching ids ``<platform>-…`` and ``dm-<platform>-…``). Omit
            for everything.

    Returns JSON with:
      - ``channels``: operator-curated channels (channel_id, display_name, kind, notes).
      - ``dms``: per-person DM channels (person, display_name, platform, channel_id),
        auto-captured the first time each person messaged you on a bridge.
      - ``live_prefixes``: the channel-id prefixes the connected bridges serve
        (e.g. ``discord-`` / ``dm-slack-``). A ``channel_id`` must carry one of
        these or ``send_message`` raises UnknownChannelError.

    To DM a person, send to their ``dms[].channel_id`` (a prefix-qualified id
    like ``dm-slack-D…``) — never their user id.
    """
    resolver = _STATE["identity_resolver"]
    channels = _STATE["channel_registry"]
    plat = (platform or "").strip().lower() or None

    def _belongs(channel_id: str) -> bool:
        if plat is None:
            return True
        return channel_id.startswith(f"{plat}-") or channel_id.startswith(f"dm-{plat}-")

    out: dict[str, Any] = {"channels": [], "dms": [], "live_prefixes": []}
    if plat is not None:
        out["platform"] = plat
    if resolver is not None:
        for ch in resolver.all_channels():
            if not _belongs(ch.canonical):
                continue
            out["channels"].append(
                {
                    "channel_id": ch.canonical,
                    "display_name": ch.display_name,
                    "kind": ch.kind,
                    "notes": ch.notes,
                }
            )
        for ident in resolver.all_identities():
            for dm_platform, cid in (ident.dm_channels or {}).items():
                if plat is not None and dm_platform.strip().lower() != plat:
                    continue
                out["dms"].append(
                    {
                        "person": ident.canonical,
                        "display_name": ident.display_name,
                        "platform": dm_platform,
                        "channel_id": cid,
                    }
                )
    if channels is not None and hasattr(channels, "prefixes"):
        out["live_prefixes"] = [p for p in channels.prefixes() if _belongs(p)]
    return json.dumps(out, indent=2, ensure_ascii=False, default=str)


@tool
def defer_injected_message(
    message_id: str,
    reason: str,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Defer a mid-turn-injected user message to its own later turn (chainlink #384).

    Mid-turn injection folds a user's follow-up into the turn you're already
    working on. Usually that's right — clarifications, corrections, "also do X",
    cancels, or priority changes should fold. But sometimes an injected message
    is a TRUE topic switch or substantial unrelated new work — common in a
    multi-person channel where several people ask independent things while you're
    mid-turn. Folding those into the current answer makes a worse, mixed response
    and blurs audit / commitment boundaries.

    Call this to hand such a message its OWN response boundary: it's re-queued as
    a fresh turn right after this one, with its original author/content preserved,
    and will not be folded again.

    CONTRACT (not runtime-enforced): if you defer a message, do NOT also answer it
    substantively in your current final response — it's already in your context,
    but answering it defeats the purpose. A brief "I'll take your other question
    as its own turn" is fine.

    Use for: a true topic switch, substantial unrelated work, a message that needs
    its own auditable response/tool-use boundary, or an explicit "separate
    question" / "new thread".
    Do NOT use for: clarifications, corrections, "also..." additions, cancels, or
    priority changes to what you're already doing — those should fold.

    Args:
        message_id: The msg_id from the injected message's header
            "[mid-turn message from <author>, msg_id: <id>]".
        reason: Short why (e.g. "topic switch", "unrelated new work").
    """
    cid = _channel_from_config_or_state(None, config)
    if not cid:
        return "defer_injected_message failed: no current channel context"
    mid = (message_id or "").strip()
    if not mid:
        return "defer_injected_message failed: message_id is required"
    from .. import mid_turn_injection
    result = mid_turn_injection.defer_message(cid, mid, (reason or "").strip())
    if result == "deferred":
        return (
            f"Deferred message {mid} to its own next turn. Do NOT answer it "
            "substantively in this response."
        )
    if result == "already_deferred":
        return f"Message {mid} is already deferred for this turn (no-op)."
    if result == "not_found":
        return (
            f"defer_injected_message failed: no injected message with msg_id {mid} "
            "in the current turn. Only messages folded into THIS turn can be deferred."
        )
    return (
        "defer_injected_message failed: no active injectable turn — nothing to defer."
    )


# ────────────────────────────────────────────────────────────────────
# Scheduler tools (mimir/scheduletools.py)
# ────────────────────────────────────────────────────────────────────

@tool
async def list_schedules() -> str:
    """List all scheduled work: yaml-config jobs AND skill pollers.

    Each entry has a ``type`` (``"job"`` or ``"poller"``) so the two
    registries are distinguishable. Jobs carry name, cron, ``priority``,
    channel, and prompt-source (one of ``prompt`` / ``prompt_file`` /
    ``callable``); pollers carry name, cron, ``priority``. ``priority``
    (low|normal|high) is the arbiter's suppression band. Runtime fields
    (``last_run`` / ``next_fire``) live on apscheduler ``Job`` objects rather
    than the config view and are still not joined here.
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "list_schedules failed: no scheduler configured"
    try:
        jobs = await scheduler.list_jobs()
    except Exception as exc:
        return f"list_schedules failed: {exc}"
    out: list[dict[str, Any]] = []
    for j in jobs:
        entry: dict[str, Any] = {
            # ``type`` distinguishes yaml-config scheduler jobs from skill
            # pollers (appended below) — they're separate registries and used to
            # be invisible here (chainlink #522).
            "type": "job",
            "name": j.name,
            "cron": j.cron,
            # Priority-banded arbiter suppression (low|normal|high). Surfaced so
            # an operator can see/verify what a job is set to (chainlink #523).
            "priority": getattr(j, "priority", "normal"),
            "channel_id": j.channel_id,
        }
        # Surface whichever prompt-source field is populated (mutually
        # exclusive per SchedulerJob's contract). Inline prompts are
        # truncated to keep this tool's output skim-friendly.
        if getattr(j, "prompt_file", None):
            entry["prompt_file"] = j.prompt_file
        elif getattr(j, "callable_name", None):
            entry["callable"] = j.callable_name
        elif j.prompt:
            entry["prompt"] = (
                j.prompt if len(j.prompt) <= 200 else j.prompt[:200] + "..."
            )
        # ``time_of_day`` is an alternative to ``cron`` — surface it
        # when the operator picked that style instead.
        time_of_day = getattr(j, "time_of_day", None)
        if time_of_day:
            entry["time_of_day"] = time_of_day
        out.append(entry)
    # Skill pollers (chainlink #522): a separate registry from the yaml jobs.
    # Surface them in the same view so the schedule isn't misleadingly empty.
    poller_usage = {}
    scheduler_home = getattr(scheduler, "_home", None)
    if scheduler_home is not None:
        try:
            poller_usage = await asyncio.to_thread(
                partial(
                    aggregate_poller_turn_usage,
                    Path(scheduler_home) / "logs" / "turns.jsonl",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive; listing must not fail
            log.warning("list_schedules poller usage aggregation failed: %s", exc)
            poller_usage = {}

    poller_details = getattr(scheduler, "registered_poller_details", None)
    if callable(poller_details):
        for p in poller_details():
            name = p.get("name")
            entry = {
                "type": "poller",
                "name": name,
                "cron": p.get("cron"),
                "priority": p.get("priority"),
            }
            if name in poller_usage:
                entry["usage"] = poller_usage[name].to_dict()
            out.append(entry)
    # Empty only when there is NO scheduled work of either kind — checked after
    # pollers are appended so a poller-only deployment isn't reported as empty
    # (the #522 visibility gap; mimir-carreira review on PR #728).
    if not out:
        return "(no scheduled jobs)"
    return json.dumps(out, indent=2, ensure_ascii=False, default=str)


@tool
async def add_schedule(
    name: str,
    cron: str,
    prompt: str = "",
    channel_id: Optional[str] = None,
    priority: Optional[str] = None,
    prompt_file: Optional[str] = None,
) -> str:
    """Add a new scheduled tick (add-or-replace by name).

    Provide exactly one of ``prompt`` or ``prompt_file``. Prefer ``prompt_file``
    for anything beyond a one-liner — it's the canonical shape used by the
    bundled ``scheduler_template.yaml`` (e.g. the memory-hygiene tick), so the
    prompt body can grow without bloating ``scheduler.yaml``.

    Args:
        name: Unique job identifier.
        cron: 5-field cron expression (e.g. ``"0 9 * * *"`` for 9am daily).
        prompt: Inline prompt to fire on the cron tick. Mutually exclusive with
            ``prompt_file``.
        channel_id: Channel to dispatch the tick on. Defaults to
            ``scheduler:<name>`` synthetic.
        priority: Arbiter suppression band — ``low``, ``normal`` (default), or
            ``high``. Higher priority rides through more resource pressure before
            the arbiter sheds the tick (``high`` is shed only at the most extreme
            severity). To change an existing job's priority without rewriting its
            prompt, use ``set_schedule_priority``.
        prompt_file: Basename of a prompt file under ``<home>/prompts/`` (e.g.
            ``"memory-hygiene.md"``) to fire on the tick. Mutually exclusive
            with ``prompt``; persisted as ``prompt_file:`` in scheduler.yaml.
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "add_schedule failed: no scheduler configured"
    inline = (prompt or "").strip()
    pfile = (prompt_file or "").strip()
    if bool(inline) == bool(pfile):
        return (
            "add_schedule failed: provide exactly one of prompt / prompt_file "
            "(got "
            + ("both" if inline else "neither")
            + ")"
        )
    resolved_priority = "normal"
    if priority is not None:
        resolved_priority = priority.strip().lower()
        if resolved_priority not in PRIORITY_LEVELS:
            return (
                f"add_schedule failed: invalid priority {priority!r} "
                f"(expected one of {sorted(PRIORITY_LEVELS)})"
            )
    if pfile:
        # Validate prompt_file with the SAME resolver the scheduler uses at fire
        # time (_resolve_prompt_file), so a value can't pass here but get rejected
        # when it fires — which would fall back to the empty inline prompt, i.e.
        # the "silently empty tick" this check exists to prevent. The resolver
        # rejects path traversal, absolute-path escapes, and symlinks; require a
        # real regular file on top. Best-effort: skip when the home isn't known.
        home = getattr(scheduler, "_home", None)
        if home is not None:
            from ..scheduler import _resolve_prompt_file
            resolved = _resolve_prompt_file(Path(home), pfile)
            if resolved is None or not resolved.is_file():
                return (
                    f"add_schedule failed: prompt_file {pfile!r} must be a regular "
                    f"file under {Path(home) / 'prompts'} (basename only — no "
                    f"'..', absolute paths, or symlinks; create the file first)"
                )
    try:
        if pfile:
            job = SchedulerJob(
                name=name, cron=cron, prompt_file=pfile, channel_id=channel_id,
                priority=resolved_priority,
            )
        else:
            job = SchedulerJob(
                name=name, cron=cron, prompt=inline, channel_id=channel_id,
                priority=resolved_priority,
            )
        job = await scheduler.add_job(job)
    except Exception as exc:
        return f"add_schedule failed: {exc}"
    source = f"prompt_file={job.prompt_file}" if job.prompt_file else "prompt=inline"
    return (
        f"add_schedule ok: name={job.name} cron={job.cron} "
        f"priority={job.priority} {source}"
    )


@tool
async def set_schedule_priority(name: str, priority: str) -> str:
    """Set the arbiter priority of an existing scheduled job.

    ``priority`` is ``low``, ``normal``, or ``high`` — higher rides through more
    resource pressure before the arbiter sheds the tick. Only the priority is
    changed; the job's prompt / prompt_file / cron / channel are preserved (so
    this is the safe way to bump a ``prompt_file`` job like a daily briefing).
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "set_schedule_priority failed: no scheduler configured"
    norm = priority.strip().lower() if isinstance(priority, str) else ""
    if norm not in PRIORITY_LEVELS:
        return (
            f"set_schedule_priority failed: invalid priority {priority!r} "
            f"(expected one of {sorted(PRIORITY_LEVELS)})"
        )
    try:
        jobs = await scheduler.list_jobs()
        match = next((j for j in jobs if j.name == name), None)
        if match is None:
            return f"set_schedule_priority failed: no job named {name!r}"
        if getattr(match, "callable_name", None):
            # Callable entries bypass the arbiter gate entirely, so priority is a
            # no-op for them (and isn't even persisted). Refuse rather than
            # silently appearing to set it.
            return (
                f"set_schedule_priority: {name!r} is a callable job; priority "
                f"does not apply (callables bypass the arbiter gate)"
            )
        await scheduler.add_job(replace(match, priority=norm))
    except Exception as exc:
        return f"set_schedule_priority failed: {exc}"
    return f"set_schedule_priority ok: name={name} priority={norm}"


@tool
async def remove_schedule(name: str) -> str:
    """Remove a scheduled tick by name."""
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "remove_schedule failed: no scheduler configured"
    try:
        removed = await scheduler.remove_job(name)
    except Exception as exc:
        return f"remove_schedule failed: {exc}"
    if not removed:
        return f"remove_schedule: no job named {name!r}"
    return f"remove_schedule ok: name={name}"


@tool
async def reload_pollers() -> str:
    """Re-read pollers.yaml and re-register all pollers.

    Use after editing the file to apply changes without restarting
    the agent. Returns counts of registered / replaced / removed.
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "reload_pollers failed: no scheduler configured"
    try:
        stats = await scheduler.reload_pollers()
    except Exception as exc:
        return f"reload_pollers failed: {exc}"
    return (
        f"reload_pollers ok: total={stats.get('total', 0)} "
        f"(fresh={stats.get('registered', 0)})"
    )


# ────────────────────────────────────────────────────────────────────
# Commitments tools (mimir/committools.py)
# ────────────────────────────────────────────────────────────────────

@tool
async def commitment_complete(commitment_id: str, message_id: Optional[str] = None) -> str:
    """Mark a tracked commitment as completed.

    Args:
        commitment_id: The commitment to close out.
        message_id: Optional message ID that triggered the completion (for audit).
    """
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_complete failed: no commitments store"
    try:
        result = await store.complete(commitment_id, message_id=message_id)
    except Exception as exc:
        return f"commitment_complete failed: {exc}"
    if not result:
        return f"commitment_complete failed: {commitment_id} not found or already terminal"
    return f"commitment_complete ok: id={commitment_id}"


@tool
async def commitment_snooze(
    commitment_id: str,
    until_iso: str,
    reason: Optional[str] = None,
) -> str:
    """Snooze a commitment until a future ISO datetime.

    Args:
        commitment_id: The commitment to snooze.
        until_iso: ISO-8601 datetime when the commitment reactivates (e.g. "2026-05-20T10:00:00Z").
        reason: Optional snooze reason recorded in the log.
    """
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_snooze failed: no commitments store"
    try:
        dt = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Naive ISO (no Z/offset — the tool's arg spec doesn't forbid it)
            # is UTC, matching the commitments CLI _parse_iso + the extractor.
            # Without this, dt.timestamp() reads the SERVER's local tz and the
            # snooze lands hours off on a non-UTC host (#503).
            dt = dt.replace(tzinfo=timezone.utc)
        until_unix = dt.timestamp()
        result = await store.snooze(commitment_id, until_unix=until_unix, reason=reason)
    except Exception as exc:
        return f"commitment_snooze failed: {exc}"
    if not result:
        return f"commitment_snooze failed: {commitment_id} not found or already terminal"
    return f"commitment_snooze ok: id={commitment_id} until={until_iso}"


@tool
async def commitment_dismiss(commitment_id: str, reason: Optional[str] = None) -> str:
    """Dismiss a commitment without completing it.

    Args:
        commitment_id: The commitment to dismiss.
        reason: Optional dismissal reason recorded in the log.
    """
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_dismiss failed: no commitments store"
    try:
        result = await store.dismiss(commitment_id, reason=reason)
    except Exception as exc:
        return f"commitment_dismiss failed: {exc}"
    if not result:
        return f"commitment_dismiss failed: {commitment_id} not found or already terminal"
    return f"commitment_dismiss ok: id={commitment_id}"


from ..commitments.models import CommitmentStatus as _CommitmentStatus

# Non-terminal statuses worth surfacing to ``commitment_list``. Sourced
# from the ``CommitmentStatus`` enum so a future rename can't silently
# drift this set out of sync with the real state machine.
_ACTIVE_STATUSES = frozenset({
    _CommitmentStatus.PENDING.value,
    _CommitmentStatus.DELIVERED.value,
    _CommitmentStatus.SNOOZED.value,
})


@tool
async def commitment_list(due_within_days: int = 7) -> str:
    """List active (non-terminal) commitments, optionally filtered by due window.

    Args:
        due_within_days: Only include commitments whose due window ends within
            this many days from now. Pass 0 to list all active commitments
            regardless of due date (default 7 days).
    """
    import time as _time
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_list failed: no commitments store"
    try:
        # store.list() is synchronous
        all_items = store.list()
    except Exception as exc:
        return f"commitment_list failed: {exc}"
    now = _time.time()
    cutoff = now + due_within_days * 86400 if due_within_days > 0 else None
    items = [
        c for c in all_items
        if c.status in _ACTIVE_STATUSES
        and (
            cutoff is None
            or c.due_window_end_unix is None  # unbound — always include
            or c.due_window_end_unix <= cutoff
        )
    ]
    if not items:
        label = "all active" if due_within_days == 0 else f"due within {due_within_days} days"
        return f"(no active commitments — {label})"
    return json.dumps(
        [
            {
                "id": c.id,
                "text": c.text,
                "status": c.status,
                "channel_id": c.channel_id,
                "due_window_hint": c.due_window_hint,
                "due_window_end_unix": c.due_window_end_unix,
            }
            for c in items
        ],
        indent=2, ensure_ascii=False, default=str,
    )


# ────────────────────────────────────────────────────────────────────
# Spawn (mimir/spawn.py)
# ────────────────────────────────────────────────────────────────────


# Pre-OSS hardening (review item #5). ``spawn_claude_code`` previously
# had no concurrency cap, no per-hour rate cap, and no recursion-depth
# limit. A misbehaving agent could fan out an unbounded number of
# parallel ``claude`` subprocesses or recursively spawn itself into a
# fork bomb. The defaults below are conservative and operator-overridable.
_SPAWN_MAX_CONCURRENT_DEFAULT = 3
_SPAWN_MAX_PER_HOUR_DEFAULT = 20
_SPAWN_MAX_DEPTH_DEFAULT = 2

# Env var the *child* claude subprocess sees so a nested
# ``spawn_claude_code`` inside the subprocess increments before
# checking against the depth cap. The harness sets this to
# ``parent_depth + 1`` when spawning.
_SPAWN_DEPTH_ENV = "MIMIR_SPAWN_DEPTH"

# Minimal-env allowlist for spawned CLI subprocesses (#494). The child
# runs an independently-prompted agent with shell + web; handing it the
# parent's full ``os.environ`` would expose every unrelated secret
# (Discord/Slack tokens, DB URLs, TAVILY/GitHub keys) to a second model
# that can read its own env and exfiltrate. So we build the child env
# from an allowlist: infrastructure vars the CLI needs to find binaries,
# config, and a home, plus only the provider credentials that specific
# CLI uses. New secrets added to the parent env are excluded by default.
_CHILD_ENV_INFRA = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM",
    "TMPDIR", "TMP", "TEMP", "TZ", "PWD", "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)
# Prefixes for benign locale/desktop vars passed through wholesale.
_CHILD_ENV_INFRA_PREFIXES = ("LC_", "XDG_")


def _minimal_child_env(*, depth: int, cred_prefixes: tuple[str, ...]) -> dict[str, str]:
    """Build a spawned-CLI env from an allowlist (#494): infra vars +
    ``cred_prefixes``-matching credentials + ``MIMIR_SPAWN_DEPTH``.

    Everything else in the parent ``os.environ`` (unrelated secrets) is
    dropped. ``cred_prefixes`` is the set of provider-credential prefixes
    the target CLI legitimately needs (e.g. ``("ANTHROPIC_", "CLAUDE_")``
    for claude, ``("OPENAI_", "CODEX_")`` for codex)."""
    env: dict[str, str] = {}
    for key in _CHILD_ENV_INFRA:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    for key, val in os.environ.items():
        if key.startswith(_CHILD_ENV_INFRA_PREFIXES) or key.startswith(cred_prefixes):
            env[key] = val
    env[_SPAWN_DEPTH_ENV] = str(depth)
    return env


def _env_int_floor1(name: str, default: int) -> int:
    """Read an int env var, defaulting if missing/invalid. Floors at 1
    so an operator who sets ``=0`` doesn't accidentally disable all
    spawns (the depth cap is the right tool for "no spawns")."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass
class _SpawnGuard:
    """Process-wide state for spawn caps. Lazy-initialized on first
    spawn so module import doesn't depend on an asyncio loop being
    running."""

    sem: asyncio.Semaphore | None = None
    recent: deque[float] = field(default_factory=deque)
    rate_lock: asyncio.Lock | None = None
    max_concurrent: int = _SPAWN_MAX_CONCURRENT_DEFAULT
    max_per_hour: int = _SPAWN_MAX_PER_HOUR_DEFAULT
    max_depth: int = _SPAWN_MAX_DEPTH_DEFAULT


_SPAWN_GUARD = _SpawnGuard()


def _spawn_guard_init() -> _SpawnGuard:
    """Re-read env vars and (re)initialize the semaphore + lock on first
    spawn. Env vars are read each invocation so tests can change them
    per-case without restarting the process; the semaphore / lock are
    created exactly once per ``max_concurrent`` value so the
    concurrency cap is real (every fresh ``Semaphore`` would defeat
    the gate by handing every caller a full set of slots)."""
    g = _SPAWN_GUARD
    new_max_concurrent = _env_int_floor1(
        "MIMIR_SPAWN_MAX_CONCURRENT", _SPAWN_MAX_CONCURRENT_DEFAULT,
    )
    g.max_per_hour = _env_int_floor1(
        "MIMIR_SPAWN_MAX_PER_HOUR", _SPAWN_MAX_PER_HOUR_DEFAULT,
    )
    g.max_depth = _env_int_floor1(
        "MIMIR_SPAWN_MAX_DEPTH", _SPAWN_MAX_DEPTH_DEFAULT,
    )
    # (Re)create only when the semaphore is missing or the cap
    # changed — keeps the existing pending waiters in the same FIFO
    # the loop scheduled them in. asyncio.Lock / Semaphore are
    # loop-bound; the ``sem is None`` arm covers the cross-loop case
    # (tests swap event loops between cases).
    if g.sem is None or g.max_concurrent != new_max_concurrent:
        g.sem = asyncio.Semaphore(new_max_concurrent)
        g.max_concurrent = new_max_concurrent
    if g.rate_lock is None:
        g.rate_lock = asyncio.Lock()
    return g


def _spawn_reset_for_tests() -> None:
    """Drop the existing semaphore + lock + rate window so a fresh
    test gets clean state. Called only from tests."""
    _SPAWN_GUARD.sem = None
    _SPAWN_GUARD.rate_lock = None
    _SPAWN_GUARD.recent.clear()


async def _spawn_acquire_rate_slot(guard: _SpawnGuard) -> tuple[float | None, str | None]:
    """Check + reserve a per-hour spawn slot.

    Returns ``(token, None)`` if a slot was reserved, where ``token`` is
    the exact timestamp appended to the sliding window, or
    ``(None, error)`` if the cap is exhausted. The token lets abort
    paths remove their own reservation rather than blindly popping the
    newest entry, which can belong to a concurrent spawn.

    The window is a sliding 3600-second wall-clock window over
    ``time.monotonic()`` timestamps. The check + append are inside
    the rate_lock so two concurrent spawns can't both squeeze under
    the cap on the same tick.
    """
    assert guard.rate_lock is not None  # initialized by _spawn_guard_init
    async with guard.rate_lock:
        now = time.monotonic()
        cutoff = now - 3600
        while guard.recent and guard.recent[0] < cutoff:
            guard.recent.popleft()
        if len(guard.recent) >= guard.max_per_hour:
            oldest_age = int(now - guard.recent[0])
            return (
                None,
                f"spawn_claude_code refused: per-hour cap "
                f"({guard.max_per_hour}/h) reached — oldest entry "
                f"{oldest_age}s ago. Raise MIMIR_SPAWN_MAX_PER_HOUR "
                f"or wait for the window to roll forward.",
            )
        guard.recent.append(now)
    return now, None


async def _spawn_release_rate_slot(guard: _SpawnGuard, token: float | None) -> None:
    """Release this spawn's reserved rate slot on abort.

    The rate window is shared by concurrent spawn calls, so abort cleanup
    must remove the exact token returned by ``_spawn_acquire_rate_slot``
    under the same lock. A blind ``pop()`` can remove a later concurrent
    spawn's reservation instead.
    """
    if token is None:
        return
    assert guard.rate_lock is not None
    async with guard.rate_lock:
        try:
            guard.recent.remove(token)
        except ValueError:
            # The token may already have aged out during an unusually long
            # wait; either way, there is no live reservation to release.
            pass


def _run_claude_subprocess(
    argv: list[str],
    cwd: str | None,
    timeout_s: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Sync subprocess.run wrapper — called from a thread via to_thread.

    Keeping the blocking I/O in a helper that's invoked through
    ``asyncio.to_thread`` keeps spawn_claude_code from freezing the
    dispatcher's event loop for the duration of the subprocess (up to
    ``timeout_s=1800`` by default). Returns (returncode, stdout, stderr)
    or raises subprocess.TimeoutExpired / FileNotFoundError unchanged.

    ``env`` (if set) replaces the inherited environment. The spawn
    path always sets it so ``MIMIR_SPAWN_DEPTH`` is incremented for
    the child, enforcing the recursion cap defense-in-depth.
    """
    proc = subprocess.run(  # noqa: S603 — argv is constructed, not shell
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
        # Headless spawn: never inherit the parent's stdin. ``codex exec``
        # reads stdin (appends it to the prompt) and would block until the
        # timeout if stdin is an open pipe/TTY; DEVNULL EOFs immediately so
        # it uses the prompt arg only. Harmless for ``claude -p`` too.
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, proc.stdout, proc.stderr


@tool
async def spawn_claude_code(
    prompt: str,
    cwd: Optional[str] = None,
    timeout_s: int = 1800,
    name: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Spawn a Claude Code subprocess to execute a complex task.

    Use for work that needs deep context isolation, long-running
    multi-step plans, or independent execution from the parent agent.
    The subprocess runs ``claude -p <prompt>`` and captures its
    output, final cost, and modelUsage metrics.

    Pre-fix this was a sync function that called ``subprocess.run``
    directly. deepagents awaited the sync callable, freezing the
    dispatcher's event loop for up to ``timeout_s=1800`` seconds —
    every other channel's worker blocked until the spawn finished.
    Now async, with the blocking subprocess call wrapped in
    ``asyncio.to_thread``.

    **Model selection heuristic** (chainlink #158): pick based on
    cognitive *depth* required, not output *shape* (destination path).
    Passing ``model`` explicitly avoids the output-path-as-proxy
    mis-tier (e.g. a wiki-page destination triggering a lighter model
    for analytical work that needs deeper reasoning).

    - ``model="opus"`` — analytical / evaluative work: VSM analysis,
      gap inventories, skeptical code review, design decisions,
      adversarial synthesis. Use when the task is "think critically"
      even if the output lands at a doc/wiki path.
    - ``model="sonnet"`` — default for most spawn work: implementation
      tasks, benchmark runs, doc writing, spec drafts, mechanical
      multi-step work where the path is well-defined.
    - ``model="haiku"`` — high-throughput mechanical tasks that are
      genuinely simple (e.g. format conversion, short summaries).
      Rarely the right choice for spawn work; prefer sonnet as the
      safe default.
    - Omit ``model`` to let the claude CLI use its configured default
      (currently sonnet-tier).

    Args:
        prompt: The task to hand to the spawned Claude Code instance.
        cwd: Working directory for the subprocess. Defaults to home.
        timeout_s: Subprocess timeout (default 30 min).
        name: Optional label recorded in the spawn log.
        model: Claude model alias or full name (e.g. ``"opus"``,
            ``"sonnet"``, ``"claude-opus-4-7"``). Passed as
            ``--model`` to the claude CLI. Omit to use the CLI
            default. Use ``"opus"`` for analytical/evaluative work
            even when the output destination is doc-shaped.
    """
    cfg = _STATE["spawn_config"]
    if cfg is None:
        return "spawn_claude_code failed: no spawn config"
    if not prompt or not prompt.strip():
        return "spawn_claude_code failed: prompt is required"

    # ── Pre-OSS hardening (review item #5): concurrency / rate / depth caps.
    guard = _spawn_guard_init()

    # Depth check first — cheapest, no resource reservation needed.
    # ``MIMIR_SPAWN_DEPTH`` is set by the harness on every nested
    # spawn (``=parent_depth+1``); the root agent has it unset / 0.
    current_depth = _env_int_floor1(_SPAWN_DEPTH_ENV, 0) if os.environ.get(
        _SPAWN_DEPTH_ENV
    ) else 0
    if current_depth >= guard.max_depth:
        return (
            f"spawn_claude_code refused: recursion depth cap reached "
            f"({current_depth} >= MIMIR_SPAWN_MAX_DEPTH={guard.max_depth}). "
            f"The parent agent is already at this nesting level — "
            f"deeper spawns would risk a fork-bomb / unbounded budget."
        )

    # Per-hour rate cap. Reserves a slot inside the rate_lock so two
    # concurrent spawns can't both pass the check.
    rate_token, rate_err = await _spawn_acquire_rate_slot(guard)
    if rate_err is not None:
        return rate_err

    # Concurrency cap. The semaphore reserves a slot for the duration
    # of the subprocess; pending spawns wait their turn rather than
    # piling up unbounded.
    cwd_path = Path(cwd).expanduser() if cwd else cfg.get("default_cwd")
    # ``--`` separator before the prompt: the prompt is arbitrary
    # operator/agent text, and a prompt starting with ``--<flag>``
    # would otherwise be parsed by the claude CLI as another flag.
    # (Review item — fast-follow in the §"Notable code bugs" section.)
    argv = ["claude", "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv += ["--", prompt]

    # Minimal env (#494): infra + Anthropic/Claude creds + the
    # incremented spawn depth. Unrelated secrets are NOT inherited.
    child_env = _minimal_child_env(
        depth=current_depth + 1, cred_prefixes=("ANTHROPIC_", "CLAUDE_"),
    )

    assert guard.sem is not None
    try:
        async with guard.sem:
            try:
                returncode, stdout, stderr = await asyncio.to_thread(
                    _run_claude_subprocess,
                    argv,
                    str(cwd_path) if cwd_path else None,
                    timeout_s,
                    child_env,
                )
            except subprocess.TimeoutExpired:
                return f"spawn_claude_code timed out after {timeout_s}s"
            except FileNotFoundError:
                return "spawn_claude_code failed: 'claude' CLI not on PATH"
    except BaseException:
        # The subprocess never started OR exited abnormally; the
        # rate-slot we reserved doesn't reflect real work that
        # consumed budget. Release it so the per-hour window is
        # accurate for the operator.
        await _spawn_release_rate_slot(guard, rate_token)
        raise
    if returncode != 0:
        return (
            f"spawn_claude_code failed: exit={returncode} "
            f"stderr={stderr[:500]}"
        )
    try:
        result = json.loads(stdout)
        return json.dumps(
            {"result": result.get("result", "")[:2000],
             "cost_usd": result.get("total_cost_usd"),
             "num_turns": result.get("num_turns"),
             "name": name},
            indent=2,
        )
    except json.JSONDecodeError:
        return f"spawn_claude_code: raw output: {stdout[:2000]}"


@tool
async def spawn_codex(
    prompt: str,
    cwd: Optional[str] = None,
    timeout_s: int = 1800,
    name: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Spawn a Codex CLI subprocess to execute a complex task.

    The Codex analogue of ``spawn_claude_code``: runs ``codex exec
    <prompt>`` once, non-interactively, and returns its output. Use for
    work you'd rather hand to Codex (OpenAI) than the parent agent —
    context isolation, independent execution, or a second model's take.
    Registered only when the ``codex`` CLI is on PATH (the tool-list gate
    consults ``providers.codex_available``), so it never appears on a
    deployment that can't run it.

    Shares the spawn caps with ``spawn_claude_code`` — the same
    per-hour / concurrency / recursion-depth budget (a spawn is a spawn
    regardless of which CLI), so an agent can't fork-bomb via either path.

    Codex CLI invocation note: ``codex exec`` is the non-interactive
    subcommand, but exact flags vary by codex version + setup. Extra
    flags (sandbox / approval mode, ``--json``, ...) are injected
    verbatim from ``MIMIR_CODEX_SPAWN_ARGS`` (shlex-split), so the
    operator tunes the invocation without a code change.

    Args:
        prompt: The task to hand to the spawned Codex instance.
        cwd: Working directory for the subprocess. Defaults to home.
        timeout_s: Subprocess timeout (default 30 min).
        name: Optional label recorded in the spawn log.
        model: Codex model name, passed as ``--model``. Omit for the
            CLI default.
    """
    cfg = _STATE["spawn_config"]
    if cfg is None:
        return "spawn_codex failed: no spawn config"
    if not prompt or not prompt.strip():
        return "spawn_codex failed: prompt is required"

    # Shared spawn caps (same guard as spawn_claude_code — one budget
    # across both spawn paths). Depth check first (cheapest), then the
    # per-hour rate slot, then the concurrency semaphore.
    guard = _spawn_guard_init()
    current_depth = _env_int_floor1(_SPAWN_DEPTH_ENV, 0) if os.environ.get(
        _SPAWN_DEPTH_ENV
    ) else 0
    if current_depth >= guard.max_depth:
        return (
            f"spawn_codex refused: recursion depth cap reached "
            f"({current_depth} >= MIMIR_SPAWN_MAX_DEPTH={guard.max_depth}). "
            f"The parent agent is already at this nesting level — "
            f"deeper spawns would risk a fork-bomb / unbounded budget."
        )
    rate_token, rate_err = await _spawn_acquire_rate_slot(guard)
    if rate_err is not None:
        # The shared helper names spawn_claude_code; retarget for this tool.
        return rate_err.replace("spawn_claude_code", "spawn_codex")

    cwd_path = Path(cwd).expanduser() if cwd else cfg.get("default_cwd")
    import shlex
    argv = ["codex", "exec"]
    # Operator-tunable extra flags (sandbox/approval mode, --json, etc.) —
    # the codex CLI surface varies by version, so don't hardcode beyond
    # the subcommand. e.g. MIMIR_CODEX_SPAWN_ARGS="--full-auto".
    argv += shlex.split(os.environ.get("MIMIR_CODEX_SPAWN_ARGS", ""))
    if model:
        argv += ["--model", model]
    # ``--`` separator so a prompt starting with ``-`` isn't parsed as a flag.
    argv += ["--", prompt]

    # Minimal env (#494): infra + OpenAI/Codex creds + the incremented
    # spawn depth. codex also reads ~/.codex/auth.json via HOME (passed
    # through). Unrelated secrets are NOT inherited.
    child_env = _minimal_child_env(
        depth=current_depth + 1, cred_prefixes=("OPENAI_", "CODEX_"),
    )

    assert guard.sem is not None
    try:
        async with guard.sem:
            try:
                returncode, stdout, stderr = await asyncio.to_thread(
                    # Generic argv subprocess runner (shared with
                    # spawn_claude_code despite the historical name).
                    _run_claude_subprocess,
                    argv,
                    str(cwd_path) if cwd_path else None,
                    timeout_s,
                    child_env,
                )
            except subprocess.TimeoutExpired:
                return f"spawn_codex timed out after {timeout_s}s"
            except FileNotFoundError:
                return "spawn_codex failed: 'codex' CLI not on PATH"
    except BaseException:
        await _spawn_release_rate_slot(guard, rate_token)
        raise
    if returncode != 0:
        return f"spawn_codex failed: exit={returncode} stderr={stderr[:500]}"
    # codex exec writes its result to stdout (text, or JSONL with --json).
    # Return it verbatim (truncated); unlike claude -p's structured JSON the
    # codex output shape varies by flags, so don't assume a parse.
    return json.dumps({"result": stdout.strip()[:2000], "name": name}, indent=2)


# ────────────────────────────────────────────────────────────────────
# Pending mimir-package update (mimir/update_on_start.py)
# ────────────────────────────────────────────────────────────────────


@tool
async def request_mimir_update(
    target_version: Optional[str] = None,
    include_prereleases: bool = False,
) -> str:
    """Approve a mimir package update — writes the pending-update flag.

    **Escalate-first action** (per memory/core/06-action-boundaries.md):
    invoke ONLY after the operator has explicitly approved the update
    in the current conversation. Writing this flag without operator
    consent is a self-modification of the running binary — the same
    kind of compounding-cost / silent-drift concern that gates
    ``memory/core/`` edits. The flag IS the operator's consent
    signal; do not fabricate it.

    Trigger conditions (all must hold):
      1. A ``mimir_update_available`` event has fired (visible in the
         per-turn feedback block).
      2. You raised the available update with the operator in this
         conversation.
      3. The operator replied with explicit approval ("yes", "do the
         update", "approve", etc.) — NOT a non-committal acknowledgment.

    What happens after this tool succeeds:
      - A flag file is written to ``<MIMIR_HOME>/.mimir/pending-update.flag``.
      - On the NEXT process restart (operator runs ``docker compose
        restart`` or the equivalent), the startup pre-flight in
        ``server.main`` runs ``pip install --upgrade``, deletes the
        flag, and re-execs onto the new code. ``mimir_update_applied``
        (positive) or ``mimir_update_failed`` (negative) lands in
        the algedonic block on the first turn after restart.
      - The running agent (this process) keeps running on the OLD
        version until the operator restarts.

    Args:
        target_version: Pin to a specific version (e.g. ``"0.2.0"``).
            Default empty → pip resolves the latest matching the
            ``include_prereleases`` setting at install time. Pinning
            is what you want when the operator explicitly reviewed a
            specific release; leaving it empty is appropriate when
            they said "update to whatever's latest."
        include_prereleases: True → pip ``--pre`` flag passed at
            install time (alpha / beta / rc accepted). Default False.

    Returns: a confirmation string with the path written and what
    will happen on next restart, plus the operator-facing reminder.
    """
    from pathlib import Path
    from ..update_on_start import write_flag

    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        return (
            "request_mimir_update failed: MIMIR_HOME env not set — "
            "can't resolve the flag location. (This shouldn't happen "
            "in a normal deployment; surface to the operator.)"
        )
    home = Path(home_env)

    cleaned_target = (target_version or "").strip()
    path = write_flag(
        home,
        target_version=cleaned_target,
        include_prereleases=bool(include_prereleases),
    )

    pin_desc = f"pinned to {cleaned_target}" if cleaned_target else "latest at install time"
    pre_desc = " (pre-releases allowed)" if include_prereleases else ""
    return (
        f"Pending-update flag written to {path}.\n"
        f"Target: {pin_desc}{pre_desc}.\n"
        f"On next restart, mimir will run pip install --upgrade and "
        f"re-exec onto the new code. The running process keeps the "
        f"OLD version until the operator restarts.\n"
        f"Operator: run `docker compose restart` (or the equivalent "
        f"for your deployment) when ready."
    )


# ────────────────────────────────────────────────────────────────────
# Convenience: assemble all tools for the deepagent factory
# ────────────────────────────────────────────────────────────────────

@tool
async def worklink_run(
    issue_id: int,
    backend: Optional[str] = None,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Dispatch a Worklink job for a ready Chainlink leaf issue, from this turn.

    Claims the issue, runs the configured coding backend in an isolated git
    worktree, validates observed evidence (diff + tests), and transitions the
    issue label — all via the deterministic core executor (the same path as
    ``mimir worklink run``; this tool never re-implements claim/evidence).

    Autonomous-dispatch guards apply here but NOT to the operator CLI:
      * Sheds under resource pressure — if the HomeostaticArbiter says the
        worklink priority can't fire (e.g. severity TIGHT), the dispatch is
        refused and you should try later. Use ``mimir worklink run`` to force.
      * Respects the concurrent-claim cap (``defaults.max_concurrent`` in
        worklink.yaml, default 2) across all in-flight Worklink workers.

    Note: a leaf run is synchronous and can take minutes — pick small,
    well-scoped ready leaves. Per-issue exclusivity is guaranteed by the
    Chainlink lock, so this never collides with another worker on the same issue.

    Args:
        issue_id: Chainlink issue id — must be a worklink-ready leaf.
        backend: Optional backend-name override (else worklink.yaml routing).
    """
    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        return "worklink_run failed: MIMIR_HOME not set"
    home = Path(home_env)

    from ..worklink.autonomy import check_concurrency, worklink_priority, worklink_repo

    # Honor the documented WORKLINK_REPO (MIMIR_WORKLINK_REPO compat); never
    # silently run the executor against the server process cwd.
    try:
        repo = Path(worklink_repo())
    except Exception as exc:
        return f"worklink_run failed: {exc}"

    # 1) Arbiter gate (cheap, in-process): shed autonomous dispatch under
    #    pressure. The CLI path never injects an arbiter, so it bypasses this.
    arbiter = _STATE.get("arbiter")
    if arbiter is not None:
        try:
            priority = worklink_priority(home)
        except Exception:
            priority = "normal"
        try:
            decision = arbiter.should_fire(priority=priority)
        except Exception as exc:  # never let arbiter errors block dispatch silently
            log.warning("worklink_run arbiter check failed: %s", exc)
            decision = None
        if decision is not None and not decision.fire:
            return (
                f"worklink_run shed: resource pressure {decision.severity.name} "
                f"(priority={decision.priority}) — {decision.reason}. "
                "Try again later, or run `mimir worklink run` to force."
            )

    # 2) Concurrency cap (chainlink query): bound total in-flight workers.
    try:
        cc = check_concurrency(home)
    except Exception as exc:
        return f"worklink_run failed: concurrency check error: {exc}"
    if not cc.allowed:
        return f"worklink_run skipped: {cc.reason} — try again when a slot frees."

    # 3) Dispatch via the deterministic core executor. ``run_worklink`` is
    #    synchronous (and opens its own event loop), so run it off the agent's
    #    loop in a worker thread.
    from ..worklink.orchestrator import run_worklink

    try:
        result = await asyncio.to_thread(
            run_worklink,
            home=home,
            repo=repo,
            issue_id=int(issue_id),
            backend=backend,
            autonomous=True,  # in-turn dispatch is autonomous → policy-gated (#460)
        )
    except Exception as exc:
        return f"worklink_run failed: {exc}"

    parts = [f"worklink_run #{result.issue_id}: {result.status}"]
    if result.attempt is not None:
        parts.append(f"attempt={result.attempt}")
    if result.review_ready:
        parts.append("review-ready")
    if result.pr_url:
        parts.append(f"PR {result.pr_url}")
    if result.evidence_path:
        parts.append(f"evidence={result.evidence_path}")
    if result.reason:
        parts.append(f"reason={result.reason}")
    return " ".join(parts)


def all_mimir_tools() -> list:
    """Return the full mimir tool surface for create_deep_agent.

    Combines tools from memory_tool, store_tool, extra_tools, and
    this module. Production cutover would wire the dep-injection
    setters in mimir/server.py:build_app once and let the agent
    discover them all at construction time.

    Web tools (Tavily ``web_search`` + ``fetch_url``) are appended
    only when the active LLM provider is not ``claude_code`` — Claude
    Code subprocesses ship native WebSearch/WebFetch and stacking
    Tavily on top would duplicate the surface. See
    ``mimir.tools.web.web_tools_enabled`` for the gating predicate.
    """
    from .memory import memory_get, memory_query
    from .store import memory_store
    from .proposals import (
        abandon_proposal,
        open_proposal,
        submit_proposal,
    )
    from .extra import file_search, get_turn, mimir_get_turn, rebuild_index, shell_exec
    from .web import web_tools_enabled
    from .shell_async import bash_async, bash_job_output, bash_jobs_list
    from .saga_ops import (
        saga_end_session,
        saga_feedback,
        saga_forget,
        saga_mark_contributions,
        saga_record_skill_learning,
    )
    tools = [
        # Memory (read + write)
        memory_query, memory_get, memory_store,
        # Change proposals for protected files (PR-gated; never writes live).
        # The sanctioned path for the agent to change memory/core/* or
        # prompts/*: open a worktree sandbox under scratch/, edit it natively,
        # then submit -> one PR for operator approval (chainlink #337/#339/#344).
        open_proposal,
        submit_proposal,
        abandon_proposal,
        # SAGA ops (outcome marker, manual credit, session boundary, forget,
        # per-skill learning capture)
        saga_feedback, saga_mark_contributions, saga_end_session, saga_forget,
        saga_record_skill_learning,
        # Indexer (file search + mid-turn index rebuild)
        file_search,
        rebuild_index,
        # Turn-history lookup (mimir_get_turn is canonical; get_turn
        # is a back-compat alias for skill prompts that reference the
        # pre-rename name)
        mimir_get_turn, get_turn,
        # Shell exec (allowlist-scoped, sync — fine for sub-second cmds)
        shell_exec,
        # Async shell — long-running jobs that wake the agent via
        # ``shell_job_complete`` on exit. The companion list/output
        # tools query the per-process ShellJobRegistry.
        bash_async, bash_jobs_list, bash_job_output,
        # Channel ops
        send_message, react, fetch_channel_history, list_channels,
        # Mid-turn injection escape hatch (chainlink #384): punt a folded
        # follow-up to its own turn instead of answering it in this one.
        defer_injected_message,
        # Scheduler
        list_schedules, add_schedule, set_schedule_priority, remove_schedule, reload_pollers,
        # Commitments
        commitment_complete, commitment_snooze,
        commitment_dismiss, commitment_list,
        # Worklink in-turn dispatch (#444). Core tool (no skill mechanism);
        # arbiter- + cap-gated autonomous dispatch to the deterministic executor.
        worklink_run,
        # Mimir-package self-update (operator-approved, applied on
        # next restart). See mimir/update_on_start.py.
        request_mimir_update,
    ]
    web_search_on, fetch_url_on = web_tools_enabled()
    if web_search_on or fetch_url_on:
        from .web import fetch_url, web_search
        if web_search_on:
            tools.append(web_search)
        if fetch_url_on:
            tools.append(fetch_url)
    # Spawn — register only when the ``claude`` CLI it shells out to is on
    # PATH (chainlink #292). A deployment routed to a non-Claude provider
    # (e.g. Minimax) typically has no claude CLI, so registering it would
    # only offer the agent a tool that fails with "'claude' CLI not on
    # PATH". Gates on CLI presence, not auth — see claude_code_available.
    from ..providers import claude_code_available, codex_available
    if claude_code_available():
        tools.append(spawn_claude_code)
    # Same gate for spawn_codex on the ``codex`` CLI (chainlink #293).
    if codex_available():
        tools.append(spawn_codex)
    # MCP-bridged tools (populated by server.py:_on_startup after the
    # MCP servers come up; empty when MCP is unconfigured).
    from .mcp import get_mcp_tools
    tools.extend(get_mcp_tools())
    # Per-turn tool-call budget gating moved to ``BudgetGateMiddleware``
    # (mimir/tools/budget_gate.py) and wired via ``create_deep_agent
    # (middleware=...)`` in agent.py. The middleware intercepts every
    # tool call — registered AND deepagents built-ins — so the
    # previous per-tool wrapping pattern (apply_budget_gate) was
    # removed to avoid double-counting.
    return tools
