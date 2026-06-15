"""Cross-turn send-loop detection (S2-2).

Detects repeated send_message to the same channel across multiple turns
and emits cross_turn_send_duplicate algedonic events.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from pathlib import Path

from ._models import FeedbackSignal
from ..jsonl_snapshot import JsonlSnapshot, iter_window_records

log = logging.getLogger(__name__)

# boundaries, complementing the per-turn S2 circuit breaker.
# ---------------------------------------------------------------------------

def _detect_cross_turn_send_loops(
    snapshot: "JsonlSnapshot | None",
    events_path: Path,
    cutoff_iso: str,
    *,
    threshold: int = 3,
) -> list[FeedbackSignal]:
    """S2-2: detect cross-turn send_message loops.

    Scans events.jsonl for ``send_message_sent`` events in the 24h window.
    Groups by (channel_id, content_hash); any pair with >= ``threshold`` sends
    is a loop.  Emits a ``cross_turn_send_duplicate`` event for each new loop
    detected (24h dedup via prior ``cross_turn_send_duplicate`` events).
    Returns synthetic FeedbackSignals for any active loops so they surface in
    the algedonic block on the next turn.
    """
    from ..event_logger import log_event_sync  # lazy — avoids top-level cycle

    send_counts: dict[tuple[str, str], int] = {}
    already_flagged: set[tuple[str, str]] = set()

    for ev in iter_window_records(snapshot, events_path):  # #498: complete window
        ts = ev.get("timestamp")
        if not isinstance(ts, str) or ts < cutoff_iso:
            if isinstance(ts, str):
                break
            continue
        evtype = ev.get("type")
        if evtype == "send_message_sent":
            cid = ev.get("channel_id") or ""
            ch = ev.get("content_hash") or ""
            if cid and ch:
                send_counts[(cid, ch)] = send_counts.get((cid, ch), 0) + 1
        elif evtype == "cross_turn_send_duplicate":
            cid = ev.get("channel_id") or ""
            ch = ev.get("content_hash") or ""
            if cid and ch:
                already_flagged.add((cid, ch))

    signals: list[FeedbackSignal] = []
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    for (cid, ch), count in send_counts.items():
        if count < threshold:
            continue
        if (cid, ch) in already_flagged:
            continue
        # Emit a persistent event so subsequent turns' ``already_flagged``
        # scan deduplicates within the 24h window.  Best-effort — a missing
        # event_logger (test paths that don't call init_logger) is fine.
        try:
            log_event_sync(
                "cross_turn_send_duplicate",
                channel_id=cid,
                content_hash=ch,
                count=count,
            )
        except RuntimeError:
            log.debug(
                "cross_turn_send_duplicate not emitted: event_logger not initialised"
            )
        signals.append(
            FeedbackSignal(
                ts=now_iso,
                polarity="negative",
                kind="cross_turn_loop",
                channel_id=cid,
                content=(
                    f"cross-turn send loop: same message sent {count}× to {cid!r} in 24h — "
                    f"check for repeated heartbeat alerts or autonomous send loops"
                ),
                count=count,
            )
        )
    return signals


# ---------------------------------------------------------------------------
