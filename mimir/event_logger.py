"""events.jsonl firehose writer (SPEC §10.1).

Append-only, lock-serialized. Writers all over the process call into the
module-level singleton via ``log_event(event_type, **payload)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._jsonl_tail import _tail_lines, count_lines_chunked

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class EventLogger:
    def __init__(
        self,
        path: Path,
        session_id: str,
        max_events: int | None = None,
        *,
        agent_id: str | None = None,
    ) -> None:
        self._path = path
        self._session_id = session_id
        # Stamped on every event record below. ``None`` keeps the
        # pre-existing record shape (the key is omitted), so the
        # turn-viewer's existing schema-tolerance covers operators
        # who haven't set MIMIR_AGENT_ID.
        self._agent_id = agent_id
        self._max_events = max_events
        self._lock: asyncio.Lock | None = None
        self._line_count = 0

        path.parent.mkdir(parents=True, exist_ok=True)
        # CR2-#6: pre-2026-05-10 this read the entire file via
        # ``read_text()`` then ``splitlines()``. events.jsonl is bounded
        # at ~300 MB (750k events × ~400 B); every process start paid
        # multi-hundred-MB memory + GC churn for a single integer.
        # ``count_lines_chunked`` reads in 64 KB chunks and counts ``\n``
        # bytes — O(1) memory.
        self._line_count = count_lines_chunked(path)

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _ensure_dir(self) -> None:
        """Recreate the parent dir if it was removed out-of-band (e.g. a
        sloppy benchmark cleanup deleted logs/ while we were running)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("events.jsonl mkdir failed: %s", exc)

    async def log(self, event_type: str, **payload: Any) -> None:
        record = self._record(event_type, payload)
        async with self._ensure_lock():
            try:
                self._ensure_dir()
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
                self._line_count += 1
                # Hysteresis: trim only when over cap by ≥10%. Without the
                # buffer, every event past the cap triggers an O(file)
                # rewrite — a high-throughput agent under a small cap pays
                # that cost on every event. The 10% margin means a
                # 1000-cap log rewrites once per ~100 events instead.
                if (
                    self._max_events
                    and self._line_count > self._max_events + max(self._max_events // 10, 1)
                ):
                    await self._trim()
            except OSError as exc:
                log.warning("events.jsonl write failed: %s", exc)

    def _record(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "type": event_type,
            "session_id": self._session_id,
        }
        if self._agent_id is not None:
            rec["agent_id"] = self._agent_id
        rec.update(payload)
        return rec

    async def _trim(self) -> None:
        # CR2-#6: tail-stream up to ``max_events`` records and rewrite,
        # rather than reading the whole file into memory. Memory bound
        # is O(max_events × avg_record_size); previously the read was
        # unbounded relative to file size. Wrapped in ``to_thread``
        # because the rewrite still does sync file IO and trim runs
        # from inside ``EventLogger.log`` which is on the event loop.
        if not self._max_events:
            return
        try:
            await asyncio.to_thread(self._trim_sync)
        except OSError as exc:
            log.warning("events.jsonl trim failed: %s", exc)

    def _trim_sync(self) -> None:
        kept_reversed: list[str] = []
        try:
            for line in _tail_lines(self._path):
                stripped = line.strip()
                if not stripped:
                    continue
                kept_reversed.append(stripped)
                if len(kept_reversed) >= self._max_events:
                    break
        except OSError as exc:
            log.warning("events.jsonl trim tail-read failed: %s", exc)
            return
        if not kept_reversed:
            return
        # tail yields newest-first; reverse for chronological rewrite.
        kept = list(reversed(kept_reversed))
        tmp = self._path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.rename(self._path)
        self._line_count = len(kept)


_logger: EventLogger | None = None


def init_logger(
    path: Path,
    session_id: str,
    max_events: int | None = None,
    *,
    agent_id: str | None = None,
) -> EventLogger:
    global _logger
    _logger = EventLogger(
        path, session_id, max_events=max_events, agent_id=agent_id,
    )
    return _logger


def get_logger() -> EventLogger:
    if _logger is None:
        raise RuntimeError("event_logger not initialized — call init_logger() at startup")
    return _logger


async def log_event(event_type: str, **payload: Any) -> None:
    await get_logger().log(event_type, **payload)


async def safe_log_event(event_type: str, **payload: Any) -> None:
    """Best-effort wrapper around ``log_event`` that swallows logger-side
    failures so a misbehaving event sink never crashes the primary work path.

    Use at monitoring call sites (algedonic gap fills, bridge supervisors)
    where the ``log_event`` result is informational only.  Logs at DEBUG
    level on failure.

    Public (no leading underscore) — intended for cross-module use.
    Bridge modules keep their own private ``_safe_log_event`` helpers
    (``mimir/bridges/{slack,discord}.py``) for isolation reasons that
    are documented in those files.
    """
    try:
        await log_event(event_type, **payload)
    except Exception as exc:  # noqa: BLE001
        log.debug("log_event %r failed: %s", event_type, exc)
