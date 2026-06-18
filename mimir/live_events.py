"""Shared live-event normalization for React dashboards.

The stream is intentionally derived from existing append-only logs. That keeps
the live substrate additive: turn records in ``turns.jsonl`` are converted into
stable event envelopes that clients can backfill, order, and deduplicate by
cursor without changing the turn runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class LiveEventItem:
    id: str
    cursor: str
    ts: str | None
    event: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cursor": self.cursor,
            "ts": self.ts,
            "event": self.event,
        }


def _turn_sort_ts(turn: dict[str, Any]) -> str:
    value = turn.get("ts")
    return str(value) if value is not None else ""


def turn_record_to_live_items(turn: dict[str, Any]) -> list[LiveEventItem]:
    turn_id = str(turn.get("turn_id") or "")
    if not turn_id:
        return []

    ts = _turn_sort_ts(turn) or None
    phase = "failed" if turn.get("error") else "finished"
    items = [
        LiveEventItem(
            id=f"turn:{turn_id}:lifecycle:{phase}",
            cursor=f"turn:{turn_id}:000000",
            ts=ts,
            event={
                "kind": "turn.lifecycle",
                "turn_id": turn_id,
                "phase": phase,
                "ts": ts,
                "error": turn.get("error"),
            },
        )
    ]

    events = turn.get("events") or []
    if isinstance(events, list):
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                continue
            cursor = f"turn:{turn_id}:{index:06d}"
            items.append(
                LiveEventItem(
                    id=f"turn:{turn_id}:event:{index}",
                    cursor=cursor,
                    ts=ts,
                    event={
                        "kind": "turn.event",
                        "turn_id": turn_id,
                        "event": event,
                    },
                )
            )
    return items


def build_live_event_backfill(
    turns: Iterable[dict[str, Any]],
    *,
    since: str | None = None,
    limit: int | None = None,
) -> list[LiveEventItem]:
    """Return ordered, deduplicated live events after ``since``.

    Cursors are lexical and monotonic for the current turn-log ordering:
    ``turn:<turn_id>:000000`` is the lifecycle item and later indexes are
    selected-turn event details. Clients persist the last delivered cursor and
    reconnect with ``?since=<cursor>``; the strict comparison below avoids
    replaying the last acknowledged item.
    """
    seen: set[str] = set()
    out: list[LiveEventItem] = []
    for turn in sorted(turns, key=_turn_sort_ts):
        for item in turn_record_to_live_items(turn):
            if since and item.cursor <= since:
                continue
            if item.id in seen:
                continue
            seen.add(item.id)
            out.append(item)
    if limit is not None and limit > 0:
        return out[-limit:]
    return out

