"""v0.4 §3: local mirror of SAGA session boundary atoms.

SAGA's ``/v1/sessions/recent`` is the source of truth, but if SAGA is
briefly down at prompt-assembly time we still want the agent to see
recent session summaries. This module owns an append-only JSONL at
``<home>/.mimir/session_boundaries.jsonl`` populated by the
``saga_end_session`` tool wrapper after a successful SAGA call. The
local mirror is best-effort: failures don't crash the tool turn; the
prompt assembly degrades gracefully when neither source is available.

Storage path is under ``.mimir/`` (alongside the indexer's SQLite db),
NOT under ``state/`` — the indexer doesn't walk ``.mimir/`` so the
mirror won't get embedded as "knowledge."

chainlink #63: session-summary Unfinished lists are point-in-time
snapshots that go stale fast. The renderer now annotates each summary
header with relative-age + turn-count markers, suffixes the Unfinished
sub-bullet with ``[verify before quoting]`` past either staleness
threshold, and applies ``closed_since`` corrective overrides written
by later boundaries (drops resolved items via case-insensitive
substring match against the closed_since refs).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ._jsonl_tail import tail_jsonl_records

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# Minimum length for a closed_since entry to participate in substring
# matching. Single-character refs would over-match (e.g. "#" appearing
# in any prose); two-character is the natural floor for things like
# "#1" or short PR refs while still rejecting empty/single-char noise.
_MIN_CLOSED_SINCE_REF_LEN = 2


@dataclass
class SessionBoundaryLog:
    """Append-only mirror at ``<home>/.mimir/session_boundaries.jsonl``.

    Records mirror the wire shape of SAGA's session boundary atoms so
    the prompt-render path doesn't care which source it got data from
    (modulo the ``ts`` field — local mirror uses the append-time UTC
    timestamp; SAGA's ``ts`` is the boundary atom's creation time on
    the SAGA side).
    """

    path: Path

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def append(self, record: dict[str, Any]) -> None:
        """Append one record. Best-effort: caller catches/logs any
        OSError so a failed mirror write doesn't fail the tool turn."""
        record = {"ts": _utc_now_iso(), **record}
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")

    def recent(
        self,
        *,
        channel_id: str | None = None,
        count: int = 3,
    ) -> list[dict[str, Any]]:
        """Return up to ``count`` most-recent records, optionally
        filtered by channel. Reverse-chronological. Empty list when
        the file is missing or unreadable.

        Tail-streamed: typical bound (count=3, occasional 20) resolves
        in one chunk read regardless of how large the file has grown."""
        out: list[dict[str, Any]] = []
        for rec in tail_jsonl_records(self.path):
            if channel_id is not None and rec.get("channel_id") != channel_id:
                continue
            out.append(rec)
            if len(out) >= count:
                break
        return out


def render_session_summaries(
    boundaries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    turn_counts: dict[str, int] | None = None,
    stale_age_hours: int = 2,
    stale_turns: int = 5,
) -> str | None:
    """Markdown body for the ``## Recent session summaries`` block.

    Each entry: ``YYYY-MM-DD HH:MM (~Xh ago, N turns this channel)
    (channel) — <summary>`` plus a one-line ``Unfinished:`` bullet
    when non-empty (suffixed ``[verify before quoting]`` once a
    staleness threshold trips). Stored-but-not-rendered fields
    (topics_discussed, decisions_made, emotional_state) are reachable
    via SAGA semantic retrieval; they'd add noise here.

    chainlink #63 staleness markers:
    - ``now`` is the wall-clock reference for the age suffix. ``None``
      skips age rendering (callers can opt out for tests / niche
      uses).
    - ``turn_counts[boundary_key]`` is the number of turns on the
      same channel since the boundary's ``ts``. Each boundary's key
      is its ``ts`` string (or empty string when ``ts`` is missing).
      Missing keys render as zero turns.
    - ``stale_age_hours`` / ``stale_turns`` thresholds: when *either*
      signal exceeds its threshold, the Unfinished bullet's header
      gets a ``[verify before quoting]`` suffix.
    - Each boundary's ``closed_since`` list (refs of items resolved
      since this boundary) is collected and used to drop stale items
      from *earlier* boundaries' Unfinished lists. Drop is by
      case-insensitive substring match — closed_since refs ≥
      ``_MIN_CLOSED_SINCE_REF_LEN`` characters are treated as
      substrings, and any Unfinished item containing one of them
      is dropped from the rendering.

    Returns ``None`` when the input is empty (or every Unfinished item
    got dropped + every summary is otherwise empty) so the caller can
    skip rendering an empty section. Always renders header lines for
    non-empty input — an empty Unfinished is itself a useful signal.
    """
    if not boundaries:
        return None
    # Aggregate every boundary's closed_since for cross-boundary drop.
    # Note: we collect refs across ALL passed boundaries (caller is
    # responsible for already filtering by channel via
    # ``recent_session_boundaries``). Aggregating here means
    # boundaries[0]'s Unfinished can be filtered by closed_since on
    # boundaries[1+] — i.e. later closures resolve earlier unfinished.
    all_closed_since: list[str] = []
    for b in boundaries:
        for ref in b.get("closed_since") or []:
            ref_str = str(ref).strip()
            if len(ref_str) >= _MIN_CLOSED_SINCE_REF_LEN:
                all_closed_since.append(ref_str)
    lines: list[str] = []
    for b in boundaries:
        # Both shapes accepted: local mirror writes ``ts`` /
        # ``channel_id``; SAGA's get_last_sessions returns
        # ``timestamp`` / ``channel`` (chainlink #63 latent fix). The
        # local-mirror naming wins when both are present.
        ts_raw = str(b.get("ts") or b.get("timestamp") or "")
        ts = _short_ts(ts_raw)
        ch = b.get("channel_id") or b.get("channel") or "-"
        summary = (b.get("summary") or "").strip() or "(no summary)"
        # Single-line summary; collapse internal newlines so the bullet
        # stays compact and readable.
        summary = " ".join(summary.split())

        age_str = _format_relative_age(ts_raw, now)
        # Per-boundary turn count: only render the marker when the
        # caller explicitly supplied a counts mapping. Tests + niche
        # call sites that don't care can omit ``turn_counts`` entirely
        # and get the lean rendering. The agent's prompt builder
        # always passes one (chainlink #63).
        turn_count: int | None
        if turn_counts is None:
            turn_count = None
        else:
            turn_count = turn_counts.get(ts_raw, 0)
        markers: list[str] = []
        if age_str:
            markers.append(age_str)
        if turn_count is not None:
            markers.append(_format_turn_count(turn_count))
        if markers:
            marker_str = ", ".join(markers)
            header_meta = f"({marker_str}) ({ch})"
        else:
            header_meta = f"({ch})"
        if ts:
            lines.append(f"- {ts} {header_meta} — {summary}")
        else:
            lines.append(f"- {header_meta} — {summary}")

        # Apply closed_since drops to this boundary's unfinished.
        unfinished = b.get("unfinished") or []
        kept = _apply_closed_since(unfinished, all_closed_since)
        if kept:
            joined = "; ".join(str(x).strip() for x in kept if str(x).strip())
            if joined:
                # Threshold for the verify-before-quoting suffix:
                # either signal alone trips it. Skip evaluation when
                # neither signal was supplied (tests / lean callers).
                age_hours = _age_hours(ts_raw, now)
                trips_age = (
                    age_hours is not None and age_hours >= stale_age_hours
                )
                trips_turns = (
                    turn_count is not None and turn_count >= stale_turns
                )
                suffix = (
                    " [verify before quoting]"
                    if (trips_age or trips_turns) else ""
                )
                lines.append(f"  Unfinished{suffix}: {joined}")
    return "\n".join(lines)


def _short_ts(ts: str) -> str:
    cleaned = ts.replace("T", " ")
    return cleaned[:16] if len(cleaned) >= 16 else cleaned


def _parse_iso_ts(ts: str) -> Optional[datetime]:
    """Parse a session-boundary timestamp string. Accepts ISO-8601 with
    or without a ``Z`` suffix; returns None when unparseable. Always
    returns a tz-aware datetime (UTC) so subtraction with ``now``
    doesn't raise."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(ts: str, now: datetime | None) -> Optional[float]:
    """Compute age in fractional hours; None when timestamp is
    unparseable or ``now`` wasn't supplied."""
    if now is None:
        return None
    parsed = _parse_iso_ts(ts)
    if parsed is None:
        return None
    delta = now - parsed
    return delta.total_seconds() / 3600.0


def _format_relative_age(ts: str, now: datetime | None) -> Optional[str]:
    """Render age as a compact string. Buckets match feedback.py's
    target_age formatter:
      <1m  → "<1m ago"
      <1h  → "{N}m ago"
      <1d  → "~{N}h ago"
      else → "~{N}d ago"
    Returns None when ``now`` wasn't supplied or ts is unparseable."""
    hours = _age_hours(ts, now)
    if hours is None:
        return None
    minutes = hours * 60.0
    if minutes < 1:
        return "<1m ago"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    if hours < 24:
        return f"~{int(hours)}h ago"
    days = hours / 24.0
    return f"~{int(days)}d ago"


def _format_turn_count(n: int) -> str:
    if n == 1:
        return "1 turn this channel"
    return f"{int(n)} turns this channel"


def _apply_closed_since(
    unfinished: Iterable[Any], closed_since_refs: list[str],
) -> list[str]:
    """Drop unfinished items containing any closed_since ref as a
    substring (case-insensitive). Refs shorter than
    ``_MIN_CLOSED_SINCE_REF_LEN`` are ignored to avoid over-matching.

    Returns a fresh list — does NOT mutate the input."""
    if not closed_since_refs:
        return [str(u) for u in unfinished if str(u).strip()]
    refs_lower = [r.lower() for r in closed_since_refs]
    kept: list[str] = []
    for item in unfinished:
        text = str(item).strip()
        if not text:
            continue
        text_lower = text.lower()
        if any(ref in text_lower for ref in refs_lower):
            continue  # dropped: a closed_since ref appears in this item
        kept.append(text)
    return kept


def count_turns_since(
    turns_log_path: Path, channel_id: str, since_ts: str,
    *,
    snapshot_records: Optional[Callable[[], Iterable[dict[str, Any]]]] = None,
) -> int:
    """Count turns on ``channel_id`` with ``ts > since_ts``.

    Used by the prompt builder to annotate each session-summary header
    with a "{N} turns this channel" marker so the agent can tell how
    much work has happened since the boundary was written. Comparison
    is on the records' ISO ``ts`` field as strings (lexicographic
    matches chronological for ISO-8601 with consistent timezone).

    ``snapshot_records`` is the in-memory iterator used by callers
    that hold a JsonlSnapshot; falls back to a tail-stream of
    ``turns_log_path`` when not supplied.

    Returns 0 when the path doesn't exist or ``since_ts`` is empty
    (which would otherwise match every record).
    """
    if not since_ts:
        return 0
    records: Iterable[dict[str, Any]]
    if snapshot_records is not None:
        records = snapshot_records()
    else:
        try:
            records = tail_jsonl_records(turns_log_path)
        except FileNotFoundError:
            return 0
    count = 0
    for rec in records:
        rec_ch = rec.get("channel_id")
        if rec_ch != channel_id:
            continue
        rec_ts = rec.get("ts")
        if not rec_ts:
            continue
        if str(rec_ts) > since_ts:
            count += 1
    return count


