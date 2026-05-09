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

import logging
import threading
import time
from pathlib import Path
from typing import Iterator

from ._jsonl_tail import tail_jsonl_records

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
# the last day. A mismatched cap is observable: ``misses`` (the count
# of times the snapshot had to be re-read because an entry beyond the
# tail was needed) shows up in events.jsonl, which CR#10 didn't pin
# but is easy to add later if the cap proves too tight.
_DEFAULT_MAX_RECORDS = 10000


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
    ) -> None:
        self._path = path
        self._ttl = ttl_s
        self._max = max_records
        self._records: list[dict] = []
        self._cached_mtime: float = -1.0
        self._cache_until: float = 0.0
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

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
        count = 0
        for rec in tail_jsonl_records(self._path):
            yield rec
            count += 1
            if count >= self._max:
                break


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
