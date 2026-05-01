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

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class EventLogger:
    def __init__(self, path: Path, session_id: str, max_events: int | None = None) -> None:
        self._path = path
        self._session_id = session_id
        self._max_events = max_events
        self._lock: asyncio.Lock | None = None
        self._line_count = 0

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                self._line_count = sum(
                    1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                )
            except OSError:
                self._line_count = 0

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
        return {
            "timestamp": _utc_now_iso(),
            "type": event_type,
            "session_id": self._session_id,
            **payload,
        }

    async def _trim(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines() if l.strip()]
            if not self._max_events or len(lines) <= self._max_events:
                return
            kept = lines[-self._max_events:]
            tmp = self._path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.rename(self._path)
            self._line_count = len(kept)
        except OSError as exc:
            log.warning("events.jsonl trim failed: %s", exc)


_logger: EventLogger | None = None


def init_logger(path: Path, session_id: str, max_events: int | None = None) -> EventLogger:
    global _logger
    _logger = EventLogger(path, session_id, max_events=max_events)
    return _logger


def get_logger() -> EventLogger:
    if _logger is None:
        raise RuntimeError("event_logger not initialized — call init_logger() at startup")
    return _logger


async def log_event(event_type: str, **payload: Any) -> None:
    await get_logger().log(event_type, **payload)
