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

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

#: Recidivism window for the "N times in the last 24h" tally.
WINDOW = timedelta(hours=24)


def nudge_enabled(channel_id: str | None, prefixes: Sequence[str]) -> bool:
    """True iff ``channel_id`` opts into resend-nudge recovery.

    Mirrors the mid-turn-injection allow-list shape: a prefix list, with
    ``"*"`` enabling all channels. Empty list (the default) disables it.
    """
    if not channel_id or not prefixes:
        return False
    if "*" in prefixes:
        return True
    return any(channel_id.startswith(p) for p in prefixes)


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


def record_and_count(home: Path | str, channel_id: str, now: datetime) -> int:
    """Append ``now`` to the per-channel no-send log, prune entries older than
    :data:`WINDOW`, and return the count within the window (including this one).

    Backed by a small JSON file under ``<home>/.mimir/`` so the tally survives
    restarts without scanning the (potentially huge) events log. Best-effort:
    a corrupt/unreadable file resets to just this occurrence rather than raising.
    """
    path = Path(home) / ".mimir" / "resend_nudge_log.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    cutoff = now - WINDOW
    kept: list[datetime] = []
    raw = data.get(channel_id)
    if isinstance(raw, list):
        for s in raw:
            try:
                ts = datetime.fromisoformat(s)
            except Exception:
                continue
            if ts >= cutoff:
                kept.append(ts)
    kept.append(now)

    # Prune other channels' stale entries too, so the file stays bounded.
    pruned: dict[str, list[str]] = {channel_id: [ts.isoformat() for ts in kept]}
    for chan, stamps in data.items():
        if chan == channel_id or not isinstance(stamps, list):
            continue
        fresh = []
        for s in stamps:
            try:
                ts = datetime.fromisoformat(s)
            except Exception:
                continue
            if ts >= cutoff:
                fresh.append(s)
        if fresh:
            pruned[chan] = fresh

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pruned), encoding="utf-8")
    tmp.replace(path)
    return len(kept)
