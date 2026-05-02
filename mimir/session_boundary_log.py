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
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._jsonl_tail import tail_jsonl_records

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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
) -> str | None:
    """Markdown body for the ``## Recent session summaries`` block.

    Each entry: ``YYYY-MM-DD HH:MM (channel) — <summary>`` plus a
    one-line ``Unfinished:`` bullet when the boundary's
    ``unfinished`` list is non-empty. Stored-but-not-rendered fields
    (topics_discussed, decisions_made, emotional_state) are reachable
    via SAGA semantic retrieval; they'd add noise here.

    Returns ``None`` when the input is empty so the caller can skip
    rendering an empty section."""
    if not boundaries:
        return None
    lines: list[str] = []
    for b in boundaries:
        ts = _short_ts(str(b.get("ts") or ""))
        ch = b.get("channel_id") or "-"
        summary = (b.get("summary") or "").strip() or "(no summary)"
        # Single-line summary; collapse internal newlines so the bullet
        # stays compact and readable.
        summary = " ".join(summary.split())
        if ts:
            lines.append(f"- {ts} ({ch}) — {summary}")
        else:
            lines.append(f"- ({ch}) — {summary}")
        unfinished = b.get("unfinished") or []
        if unfinished:
            joined = "; ".join(str(x).strip() for x in unfinished if str(x).strip())
            if joined:
                lines.append(f"  Unfinished: {joined}")
    return "\n".join(lines)


def _short_ts(ts: str) -> str:
    cleaned = ts.replace("T", " ")
    return cleaned[:16] if len(cleaned) >= 16 else cleaned


