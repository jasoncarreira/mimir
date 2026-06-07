"""events.jsonl firehose writer (SPEC §10.1).

Append-only, lock-serialized. Writers all over the process call into the
module-level singleton via ``log_event(event_type, **payload)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._jsonl_tail import _tail_lines, count_lines_chunked
from .redaction import redact_payload

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
        # chainlink #393: serialize the SYNC file mutators that can run on
        # different threads — log_sync (loop thread) and _trim_sync (worker
        # thread, via to_thread). Without it, a log_sync append could land on
        # the old inode in the window between _trim_sync's tail-read and its
        # tmp.rename(), losing that record, and _line_count drifts (loop +=1 vs
        # worker =len(kept)). A threading.Lock, NOT the asyncio lock, because
        # _trim_sync runs off-loop. (The async log() path is already serialized
        # by the asyncio lock and awaits its own _trim, so it never races.)
        self._io_lock = threading.Lock()
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

    def log_sync(self, event_type: str, **payload: Any) -> None:
        """Synchronous append — see ``log_event_sync`` for callsite
        rationale. No async lock acquisition; relies on POSIX
        ``O_APPEND`` atomicity. Errors are swallowed at WARN (same as
        the async path) so a misbehaving log sink never crashes the
        primary work path."""
        record = self._record(event_type, payload)
        try:
            # chainlink #393: hold _io_lock so this append can't interleave with
            # _trim_sync's tail-read + rename on the worker thread.
            with self._io_lock:
                self._ensure_dir()
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
                self._line_count += 1
            # Trim deferred to the async path — see comment in log().
        except OSError as exc:
            log.warning("events.jsonl sync write failed: %s", exc)

    def _record(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "type": event_type,
            "session_id": self._session_id,
        }
        if self._agent_id is not None:
            rec["agent_id"] = self._agent_id
        rec.update(redact_payload(payload))
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
        # chainlink #393: hold _io_lock across the whole tail-read → rename so a
        # concurrent log_sync append (loop thread) can't be lost in the rename
        # window, and _line_count is written under the same lock as log_sync's.
        with self._io_lock:
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


def get_events_path() -> Path | None:
    """Path the singleton EventLogger writes events.jsonl to, or ``None``
    if the logger isn't initialized yet. Read-side consumers (poller
    failed-turn recovery, chainlink #262) use this to tail turn-outcome
    events without threading the path through every call site."""
    return _logger._path if _logger is not None else None


async def log_event(event_type: str, **payload: Any) -> None:
    await get_logger().log(event_type, **payload)


def log_event_sync(event_type: str, **payload: Any) -> None:
    """Sync variant for callsites that can't await — e.g. langchain
    ``rate_limit_callback`` invoked inline on the chat model's response
    path. POSIX ``O_APPEND`` keeps writes atomic at the OS level for
    records well under ``PIPE_BUF`` (4 KB on Linux), so the sync path
    doesn't interleave with the async writer even when both fire at the
    same time. Trimming is intentionally deferred — the next async
    ``log_event`` will catch up on the line-count check."""
    get_logger().log_sync(event_type, **payload)


def _reset_logger_for_tests() -> None:
    """Reset the module-level logger singleton.

    Only for use in unit tests — clears the ``_logger`` global so a test
    that called ``init_logger`` doesn't pollute the next test's logger state.
    Never call from production code.
    """
    global _logger
    _logger = None


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
