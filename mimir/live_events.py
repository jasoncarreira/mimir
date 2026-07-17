"""Shared live-event normalization for React dashboards.

The stream is intentionally derived from existing append-only logs. That keeps
the live substrate additive: turn records in ``turns.jsonl`` are converted into
stable event envelopes that clients can backfill, order, and deduplicate by
cursor without changing the turn runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ._jsonl_tail import tail_jsonl_records


FRESH_BACKFILL_MAX_RECORDS = 100
CURSOR_BACKFILL_MAX_RECORDS = 5000


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


def _turn_sort_key(turn: dict[str, Any]) -> tuple[str, str]:
    return (_turn_sort_ts(turn), str(turn.get("turn_id") or ""))


def _cursor_ts(cursor: str | None) -> str | None:
    if not cursor:
        return None
    parts = cursor.rsplit(":", 2)
    return parts[0] if len(parts) == 3 else None


def _live_cursor(ts: str | None, turn_id: str, index: int) -> str:
    """Return a cursor whose lexical order matches delivery order."""
    # ISO-8601 timestamps sort lexically in chronological order for the turn
    # records we write. Keep missing timestamps deterministic and older than
    # real records, then append turn_id/index as tie-breakers inside a timestamp.
    return f"{ts or ''}:{turn_id}:{index:06d}"


def turn_record_to_live_items(turn: dict[str, Any]) -> list[LiveEventItem]:
    turn_id = str(turn.get("turn_id") or "")
    if not turn_id:
        return []

    ts = _turn_sort_ts(turn) or None
    phase = "failed" if turn.get("error") else "finished"
    # Carry the turn's seq, channel, and trigger on every live item: seq lets
    # consumers show the running total as max(seq) (no double-count on backfill);
    # channel_id/trigger let them scope (e.g. the chat Field Log shows only
    # web-chat turns, not background poller/heartbeat turns).
    seq = turn.get("seq")
    channel_id = turn.get("channel_id")
    trigger = turn.get("trigger")
    items = [
        LiveEventItem(
            id=f"turn:{turn_id}:lifecycle:{phase}",
            cursor=_live_cursor(ts, turn_id, 0),
            ts=ts,
            event={
                "kind": "turn.lifecycle",
                "turn_id": turn_id,
                "phase": phase,
                "ts": ts,
                "error": turn.get("error"),
                "seq": seq if isinstance(seq, int) else None,
                "channel_id": channel_id,
                "trigger": trigger,
            },
        )
    ]

    events = turn.get("events") or []
    if isinstance(events, list):
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                continue
            cursor = _live_cursor(ts, turn_id, index)
            items.append(
                LiveEventItem(
                    id=f"turn:{turn_id}:event:{index}",
                    cursor=cursor,
                    ts=ts,
                    event={
                        "kind": "turn.event",
                        "turn_id": turn_id,
                        "channel_id": channel_id,
                        "trigger": trigger,
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

    Cursors are lexical and monotonic for delivery order:
    ``<turn-ts>:<turn-id>:000000`` is the lifecycle item and later indexes
    are selected-turn event details. Clients persist the last delivered cursor
    and reconnect with ``?since=<cursor>``; the strict comparison below avoids
    replaying the last acknowledged item.
    """
    seen: set[str] = set()
    out: list[LiveEventItem] = []
    for turn in sorted(turns, key=_turn_sort_key):
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



def read_live_event_items_since(
    turns_log: Path,
    *,
    since: str | None = None,
    limit: int | None = None,
    max_records: int | None = None,
    tail_reader: Callable[[Path], Iterable[dict[str, Any]]] = tail_jsonl_records,
) -> list[LiveEventItem]:
    """Read the newest turn-log tail and return live events after ``since``.

    The SSE loop calls this from a worker thread. Reading newest-first lets us
    stop once the tail has crossed the acknowledged monotonic cursor instead
    of reparsing/sorting the full retained turn window every second.
    """
    if not turns_log.is_file():
        return []

    records: list[dict[str, Any]] = []
    record_limit = max_records
    if record_limit is None:
        record_limit = CURSOR_BACKFILL_MAX_RECORDS if since else FRESH_BACKFILL_MAX_RECORDS
    since_ts = _cursor_ts(since)
    try:
        for record in tail_reader(turns_log):
            if not isinstance(record, dict):
                continue
            record_ts = _turn_sort_ts(record)
            if since and since_ts is not None and record_ts < since_ts:
                break
            if since and since_ts is None:
                items = turn_record_to_live_items(record)
                if items and items[-1].cursor <= since:
                    break
            records.append(record)
            if len(records) >= record_limit:
                break
    except OSError:
        return []

    records.reverse()
    return build_live_event_backfill(records, since=since, limit=limit)
