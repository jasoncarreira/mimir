"""Mtime+TTL-cached snapshot of a JSONL file's tail (CR#10).

The hot path of a turn reads ``events.jsonl`` and ``turns.jsonl`` from
6+ call sites — feedback assembly, usage block, self-state block,
session summaries, subagent aggregate, budget partition. Each one used
to call ``tail_jsonl_records(path)`` directly, walking the file from
the tail in 8-KiB chunks. With WAL'd writers happening concurrently,
that's a real I/O storm at high turn rates (~50–200 ms per turn just on
JSONL parsing on a cold cache).

``JsonlSnapshot`` caches the parsed tail records (newest-first, up to a
bound) for ``ttl_s`` seconds OR until the file's mtime changes. Within
the TTL window callers iterate the cached list in O(1) instead of
re-streaming.

Owned by Agent: ``Agent._events_snapshot`` and ``Agent._turns_snapshot``
are constructed in ``__init__`` and threaded through to the call sites
that need them. Module-level helpers (``aggregate_usage``,
``_partition_turns``, ``FeedbackLog.recent``, etc.) accept the snapshot
as an optional kwarg and fall back to ``tail_jsonl_records(path)`` when
None — preserves test-side back-compat where call sites construct
without an Agent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Iterator

from ._jsonl_tail import _tail_lines, tail_jsonl_records

log = logging.getLogger(__name__)


# 5 seconds. Long enough to amortize the per-turn read storm (a turn
# fires the readers ~6 times in <1 second), short enough that the
# operator's tail -f sees fresh data within human-perceptible latency.
# The mtime check ensures correctness even within the TTL — if a writer
# touches the file between two reads, the next read reloads regardless
# of TTL.
_DEFAULT_TTL_S = 5.0

# 10000 records is enough to cover the v0.4 retention caps (5k turns /
# 75k events) for turns.jsonl, and to cover ~30 days of events.jsonl
# under typical mimir traffic (~hundred events/turn × tens of turns/day).
# Records older than the cached window are read by very few call sites
# (the homeostat 7-day window is the deepest); those sites tolerate
# missing-old-records since the data they care about is typically in
# the last day.
#
# Failure mode under a firehose (chainlink #259 item 15): time-windowed
# scans (e.g. cross-turn dedup / arousal counts over the last 24h) walk
# the newest-first records and early-break at ``ts < cutoff``. If a day
# produces >``_DEFAULT_MAX_RECORDS`` events, the cap is exhausted before
# the cutoff is reached, so the scan silently sees only the newest 10k
# and never reaches its window edge. We can't make the cap window-aware
# without giving up the memory bound (an unbounded firehose would load
# unbounded records), so instead we make the truncation OBSERVABLE: a
# saturated snapshot exposes ``saturated`` and logs a one-time warning,
# so a too-tight cap shows up in logs rather than as a silently short
# window.
_DEFAULT_MAX_RECORDS = 10000

# Byte budget for the cached tail. A record-count cap alone doesn't bound
# memory: turn records have unbounded-ish sizes (observed 600 KB+ before
# args/reasoning capping), and this cached tail lives parsed in RAM for
# process lifetime — a busy deployment measured ~600 MB resident from
# turns.jsonl alone. 64 MiB of raw JSONL (~2x that parsed) keeps every
# legitimate reader (stats/feedback/arbiter want recent records) while
# bounding the resident set even over legacy fat logs.
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class JsonlSnapshot:
    """Mtime+TTL-cached newest-first list of decoded JSONL records.

    Thread-safe: production reads happen from the asyncio event loop's
    task AND from ``asyncio.to_thread`` worker threads (the homeostat
    snapshot runs in a worker). A ``threading.Lock`` guards the cache
    state so concurrent ``records()`` calls don't double-read.

    Empty list when the file is missing or unreadable. Subsequent reads
    after the file is created pick it up via the mtime check.
    """

    def __init__(
        self,
        path: Path,
        *,
        ttl_s: float = _DEFAULT_TTL_S,
        max_records: int = _DEFAULT_MAX_RECORDS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self._path = path
        self._ttl = ttl_s
        self._max = max_records
        self._max_bytes = max_bytes
        self._byte_capped = False
        self._records: list[dict] = []
        self._cached_mtime: float = -1.0
        self._cache_until: float = 0.0
        self._lock = threading.Lock()
        # True when the last tail-drain hit the record cap, so the cached
        # list may not reach back as far as a time-windowed caller wants
        # (chainlink #259 item 15). One-time warning guard so a steady
        # firehose doesn't spam the log every re-read.
        self._saturated: bool = False
        self._saturation_warned: bool = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def saturated(self) -> bool:
        """True if the cached tail hit the ``max_records`` cap on its last
        drain — a time-windowed scan that exhausts the snapshot without
        reaching its cutoff may be silently truncated. Callers doing such
        scans can check this to flag a short window. Reflects the most
        recent drain; reads within the TTL keep the prior value."""
        with self._lock:
            return self._saturated

    def records(self) -> list[dict]:
        """Return the cached newest-first records, refreshing if stale.

        Within the TTL window, returns the cached list directly (no
        stat, no read). After the TTL expires, ``stat()``s the file —
        if mtime is unchanged, refreshes the TTL deadline and returns
        the cached list. If mtime has changed, re-reads the tail.

        Callers must NOT mutate the returned list.
        """
        now = time.monotonic()
        with self._lock:
            if now < self._cache_until:
                return self._records

            try:
                stat = self._path.stat()
            except OSError:
                # File is missing (or stat failed). Treat as empty;
                # subsequent calls re-stat after the TTL.
                self._records = []
                self._cached_mtime = -1.0
                self._cache_until = now + self._ttl
                return self._records

            if stat.st_mtime == self._cached_mtime:
                # File unchanged — refresh the TTL window, reuse cache.
                self._cache_until = now + self._ttl
                return self._records

            # File changed (or first read) — drain the tail.
            self._records = list(self._collect_tail())
            self._cached_mtime = stat.st_mtime
            self._cache_until = now + self._ttl
            # Saturated when the drain filled to the cap — the tail may
            # extend past what we cached, so time-windowed scans can be
            # truncated (chainlink #259 item 15). Warn once per process
            # per path so a too-tight cap is visible without log spam.
            self._saturated = len(self._records) >= self._max or self._byte_capped
            if self._saturated and not self._saturation_warned:
                self._saturation_warned = True
                log.warning(
                    "JsonlSnapshot saturated: %s hit its cap (%d records / "
                    "%d bytes); time-windowed scans may see a shorter "
                    "window than requested. Raise the caps or tighten "
                    "retention.",
                    self._path, self._max, self._max_bytes,
                )
            return self._records

    def invalidate(self) -> None:
        """Force a re-read on the next ``records()`` call.

        Useful when the snapshot owner just wrote to the file and wants
        to see its own write reflected immediately rather than waiting
        for the TTL. Mimir's writers call this after appending to
        events.jsonl / turns.jsonl so the next reader picks up the new
        line.
        """
        with self._lock:
            self._cached_mtime = -1.0
            self._cache_until = 0.0

    def _collect_tail(self) -> Iterator[dict]:
        # Reads raw lines (not pre-decoded records) so the byte budget is
        # counted on the wire size, newest-first; the count cap and the
        # byte cap both stop the drain, and either sets saturation.
        count = 0
        consumed = 0
        self._byte_capped = False
        try:
            for line in _tail_lines(self._path):
                stripped = line.strip()
                if not stripped:
                    continue
                consumed += len(stripped)
                if count and consumed > self._max_bytes:
                    self._byte_capped = True
                    return
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                yield rec
                count += 1
                if count >= self._max:
                    return
        except OSError:
            return


def iter_snapshot_or_tail(
    snapshot: "JsonlSnapshot | None", path: Path,
) -> Iterator[dict]:
    """Iterate newest-first records — from the snapshot if provided,
    otherwise streaming from the file directly.

    The fallback to ``tail_jsonl_records(path)`` keeps module-level
    helpers (``aggregate_usage``, ``_partition_turns``, etc.) usable in
    tests / direct-call paths that don't construct an Agent.
    """
    if snapshot is not None:
        # Iterate a copy of the list so concurrent invalidate/refresh
        # doesn't change the iteration target mid-walk. The cached list
        # is at most _DEFAULT_MAX_RECORDS so the copy is cheap.
        for rec in list(snapshot.records()):
            yield rec
        return
    for rec in tail_jsonl_records(path):
        yield rec


def iter_window_records(
    snapshot: "JsonlSnapshot | None", path: Path,
) -> Iterator[dict]:
    """Iterate newest-first records for a TIME-WINDOWED scan (one that breaks
    at a cutoff older than the newest record).

    Like :func:`iter_snapshot_or_tail`, but when the snapshot is **saturated**
    — its cached tail hit ``max_records`` and may not reach the scan's cutoff —
    stream directly from the file instead (#498). Otherwise a windowed
    count / dedup pass silently truncates at the cap: kind counts undercount
    (a threshold-crossing kind never escalates) and prior-escalation dedup
    misses entries beyond the tail (escalation re-fires). The direct stream
    still stops at the caller's cutoff, so it reads only back to the window
    edge, not the whole file.

    Do NOT use this for non-windowed scans that only need the newest records
    (those are unaffected by saturation — the cap keeps the newest)."""
    if snapshot is not None:
        records = snapshot.records()  # refreshes the cache + .saturated
        if not snapshot.saturated:
            for rec in list(records):
                yield rec
            return
    # No snapshot, or a saturated one — stream the full file tail so the
    # windowed scan reaches its cutoff.
    for rec in tail_jsonl_records(path):
        yield rec
