"""Resend-nudge: recover an interactive turn that produced a reply but never
called ``send_message`` (so the user got nothing).

The forgot-to-send guard (chainlink #423) only *detects* this and emits a
negative feedback signal that surfaces on the NEXT turn — too late for the
reply the user is waiting on. This module backs an opt-in recovery: when an
allow-listed channel's interactive turn ends undelivered, the agent re-prompts
itself ONCE to call ``send_message`` now (the re-prompt itself lives in
``mimir.agent`` since it re-enters the model loop). This is the missing
in-band recovery for tool-shy models (e.g. minimax M3, which tends to answer in
final text instead of calling the tool).

This module holds only the pure, testable pieces: the channel gate, the nudge
text, and a small 24h recidivism counter. All are best-effort and must never
raise into a turn.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .jsonl_snapshot import iter_window_records
from .web_channels import WEB_CHANNEL_PREFIX

#: The under-send signal the 24h tally counts — emitted by the forgot-to-send
#: guard (#423) on every interactive turn that produced text but didn't deliver.
_NO_SEND_EVENT = "interactive_turn_no_send_message"


def channel_prefix_enabled(channel_id: str | None, prefixes: Sequence[str]) -> bool:
    """True iff ``channel_id`` matches a prefix allow-list.

    Shared config shape: ``"*"`` enables every channel, an empty tuple disables
    the feature, and all other entries are literal channel-id prefixes.
    """
    if not channel_id:
        return False
    if not prefixes:
        return False
    if "*" in prefixes:
        return True
    return any(channel_id.startswith(p) for p in prefixes)


def nudge_enabled(channel_id: str | None, prefixes: Sequence[str]) -> bool:
    """True iff ``channel_id`` opts into resend-nudge recovery.

    Web chat (``web-*``) is ALWAYS enabled, independent of
    ``MIMIR_RESEND_NUDGE_CHANNELS``: it's single-user and interactive, so a
    tool-shy model (e.g. minimax M3) silently dropping a reply is never
    acceptable there. For all other channels it mirrors the mid-turn-injection
    allow-list shape: a prefix list, with ``"*"`` enabling all channels; an
    empty list (the default) disables it.
    """
    if not channel_id:
        return False
    if channel_id.startswith(WEB_CHANNEL_PREFIX):
        return True
    return channel_prefix_enabled(channel_id, prefixes)


def build_nudge_text(channel_id: str, count: int) -> str:
    """The corrective re-prompt. ``count`` is the no-send tally in the last 24h
    (including this occurrence); the running tally is included once it's a
    repeat, as behavioral pressure on a chronically tool-shy model."""
    tally = f" — that's {count} times in the last 24 hours" if count and count > 1 else ""
    return (
        "You produced a reply but never called send_message, so the user "
        f"received nothing{tally}. Your final text is treated as reasoning and "
        "is NOT auto-delivered. Call send_message now "
        f"(channel_id={channel_id!r}) to deliver the response you just wrote — "
        "do only that, no other tools or work."
    )


def count_recent_no_sends(events_path: Path | str, channel_id: str, cutoff_iso: str) -> int:
    """Count prior ``interactive_turn_no_send_message`` events for ``channel_id``
    in the window ``[cutoff_iso, now]``, read from ``events.jsonl`` the same way
    the algedonic block counts (a windowed tail scan via
    :func:`iter_window_records`) — one source of truth, not a parallel counter.

    Returns the count of PRIOR occurrences (the caller adds 1 for the current,
    not-yet-emitted one). Best-effort: 0 on a missing/unreadable log.
    """
    try:
        count = 0
        for ev in iter_window_records(None, Path(events_path)):  # newest-first
            ts = ev.get("timestamp")
            if not isinstance(ts, str) or ts < cutoff_iso:
                if isinstance(ts, str):
                    break  # walked past the window edge (log is chronological)
                continue
            if ev.get("type") == _NO_SEND_EVENT and ev.get("channel_id") == channel_id:
                count += 1
        return count
    except Exception:
        return 0
