"""Tests for JsonlSnapshot — mtime+TTL-cached JSONL tail (CR#10)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mimir.jsonl_snapshot import (
    JsonlSnapshot,
    iter_snapshot_or_tail,
    iter_window_records,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _bump_mtime(path: Path, *, seconds: float = 1.0) -> None:
    """Force a different mtime than the current cached value.

    On fast filesystems (or with sub-second granularity) two writes
    inside the same second can have identical mtime — that's a real
    bug class for the snapshot, but pinning it requires a deliberate
    bump. Use ``os.utime`` to force a difference."""
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + seconds))


# ─── Basic read ──────────────────────────────────────────────────────


def test_records_returns_newest_first(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}, {"i": 3}])

    snap = JsonlSnapshot(path)
    out = snap.records()

    # Tail-first iteration → newest record first.
    assert [r["i"] for r in out] == [3, 2, 1]


def test_records_returns_empty_for_missing_file(tmp_path: Path):
    snap = JsonlSnapshot(tmp_path / "missing.jsonl")
    assert snap.records() == []


def test_records_picks_up_file_creation_after_first_call(tmp_path: Path):
    """A snapshot constructed before the file exists should still work
    once the file appears. Pinned because new mimir homes start with
    no events.jsonl until the first log_event call."""
    path = tmp_path / "events.jsonl"
    snap = JsonlSnapshot(path, ttl_s=0.0)  # zero TTL → re-stat every call
    assert snap.records() == []

    _write_jsonl(path, [{"i": 1}])
    assert [r["i"] for r in snap.records()] == [1]


# ─── Caching ─────────────────────────────────────────────────────────


def test_repeat_calls_within_ttl_return_cached_list(tmp_path: Path):
    """Within the TTL window, ``records()`` returns the same list
    instance — no re-read, no re-parse."""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}])
    snap = JsonlSnapshot(path, ttl_s=60.0)

    first = snap.records()
    second = snap.records()
    assert first is second  # exact same list — cached, not re-read


def test_writes_within_ttl_are_not_seen_until_invalidate(tmp_path: Path):
    """The TTL window prefers throughput over freshness. Writes are
    visible only on TTL expiry, mtime change after TTL, OR explicit
    invalidate(). This test pins the explicit-invalidate path; the
    mtime-after-TTL path is covered separately."""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}])
    snap = JsonlSnapshot(path, ttl_s=60.0)

    assert [r["i"] for r in snap.records()] == [1]

    _append_jsonl(path, [{"i": 2}])
    # Within TTL: cache wins, no stat, new record not visible.
    assert [r["i"] for r in snap.records()] == [1]

    snap.invalidate()
    assert [r["i"] for r in snap.records()] == [2, 1]


def test_records_re_reads_after_ttl_expiry_when_mtime_changes(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}])
    snap = JsonlSnapshot(path, ttl_s=0.05)

    assert [r["i"] for r in snap.records()] == [1]

    _append_jsonl(path, [{"i": 2}])
    _bump_mtime(path)
    time.sleep(0.06)  # past the TTL

    # Past TTL + mtime advanced → re-read.
    assert [r["i"] for r in snap.records()] == [2, 1]


def test_records_skips_re_read_when_mtime_unchanged(tmp_path: Path):
    """After TTL expiry the snapshot stat()s the file. If mtime is
    unchanged, the cache is reused — only the TTL window is reset.
    Pin by patching tail_jsonl_records to count calls."""
    from unittest.mock import patch

    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}])
    snap = JsonlSnapshot(path, ttl_s=0.05)

    # Prime the cache.
    snap.records()

    time.sleep(0.06)  # past TTL but no mtime change

    # Patch the underlying reader to fail loudly if called.
    call_count = {"n": 0}

    def _counting_tail(p):
        call_count["n"] += 1
        for line in p.read_text().splitlines():
            yield json.loads(line)

    with patch("mimir.jsonl_snapshot.tail_jsonl_records", side_effect=_counting_tail):
        snap.records()
        assert call_count["n"] == 0, (
            "mtime unchanged after TTL expiry must not trigger a re-read"
        )


# ─── max_records bound ───────────────────────────────────────────────


def test_records_caps_at_max_records(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": i} for i in range(20)])

    snap = JsonlSnapshot(path, max_records=5)
    out = snap.records()

    # Newest 5 only — newest-first iteration with the cap applied.
    assert [r["i"] for r in out] == [19, 18, 17, 16, 15]


# ─── invalidate ──────────────────────────────────────────────────────


def test_invalidate_forces_re_read_on_next_records_call(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}])
    snap = JsonlSnapshot(path, ttl_s=60.0)

    snap.records()  # populate
    _append_jsonl(path, [{"i": 2}])
    snap.invalidate()

    # Even within TTL, post-invalidate read sees the new record.
    assert [r["i"] for r in snap.records()] == [2, 1]


# ─── iter_snapshot_or_tail ───────────────────────────────────────────


def test_iter_snapshot_or_tail_uses_snapshot_when_provided(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}])
    snap = JsonlSnapshot(path)

    out = list(iter_snapshot_or_tail(snap, path))
    assert [r["i"] for r in out] == [2, 1]


def test_iter_snapshot_or_tail_falls_back_to_direct_tail_when_none(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}])

    out = list(iter_snapshot_or_tail(None, path))
    assert [r["i"] for r in out] == [2, 1]


def test_iter_snapshot_or_tail_handles_missing_file(tmp_path: Path):
    """Missing-file case must work for both snapshot and direct paths."""
    missing = tmp_path / "missing.jsonl"
    snap = JsonlSnapshot(missing)
    assert list(iter_snapshot_or_tail(snap, missing)) == []
    assert list(iter_snapshot_or_tail(None, missing)) == []


# ─── iter_window_records (#498) ──────────────────────────────────────


def test_iter_window_records_falls_back_to_file_when_saturated(tmp_path: Path):
    """#498: a saturated snapshot would truncate a time-windowed scan at the
    cap (undercounting the window). iter_window_records streams the full file
    tail instead, so the scan reaches its cutoff."""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": i} for i in range(20)])

    snap = JsonlSnapshot(path, max_records=5)
    assert snap.records() and snap.saturated  # cap hit → saturated

    out = list(iter_window_records(snap, path))
    # All 20 (newest-first) via the file fallback — not just the capped 5.
    assert [r["i"] for r in out] == list(range(19, -1, -1))


def test_iter_window_records_uses_snapshot_when_not_saturated(tmp_path: Path):
    """Unsaturated → use the cheap cached snapshot (no file re-read needed)."""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}])
    snap = JsonlSnapshot(path, max_records=100)
    assert not snap.saturated or snap.records()  # not saturated

    out = list(iter_window_records(snap, path))
    assert [r["i"] for r in out] == [2, 1]


def test_iter_window_records_falls_back_when_no_snapshot(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": 1}, {"i": 2}])
    out = list(iter_window_records(None, path))
    assert [r["i"] for r in out] == [2, 1]


# ─── Concurrency ─────────────────────────────────────────────────────


def test_records_thread_safe(tmp_path: Path):
    """Multiple threads calling records() concurrently must not crash
    or double-read on first population. Pinned by spinning N threads
    that race for the first records() call and asserting consistent
    output."""
    import threading

    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": i} for i in range(100)])

    snap = JsonlSnapshot(path, ttl_s=60.0)

    results: list[list[dict]] = []
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        results.append(snap.records())

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All workers see the same data — same list contents, same length.
    assert all(len(r) == 100 for r in results)
    assert all([r["i"] for r in res] == [r["i"] for r in results[0]] for res in results)


# ─── Saturation observability (chainlink #259 item 15) ───────────────


def test_not_saturated_when_under_cap(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": n} for n in range(5)])
    snap = JsonlSnapshot(path, max_records=10)
    out = snap.records()
    assert len(out) == 5
    assert snap.saturated is False


def test_saturated_when_cap_hit(tmp_path: Path):
    """A file with more records than the cap drains to exactly the cap
    and reports saturated — the signal a time-windowed scan can be
    silently truncated."""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": n} for n in range(25)])
    snap = JsonlSnapshot(path, max_records=10)
    out = snap.records()
    assert len(out) == 10
    # Newest-first, capped at the 10 most recent.
    assert [r["i"] for r in out] == list(range(24, 14, -1))
    assert snap.saturated is True


def test_saturation_warns_once(tmp_path: Path, caplog):
    """Saturation logs a warning, but only once per process/path even
    across re-reads, so a steady firehose doesn't spam the log."""
    import logging
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": n} for n in range(15)])
    snap = JsonlSnapshot(path, ttl_s=0.0, max_records=10)
    with caplog.at_level(logging.WARNING, logger="mimir.jsonl_snapshot"):
        snap.records()
        _append_jsonl(path, [{"i": 99}])
        _bump_mtime(path)
        snap.records()  # second drain, still saturated
    warnings = [r for r in caplog.records if "saturated" in r.getMessage()]
    assert len(warnings) == 1


def test_saturated_clears_when_file_shrinks_under_cap(tmp_path: Path):
    """Saturation reflects the latest drain — if the tail later fits under
    the cap, the flag clears."""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"i": n} for n in range(15)])
    snap = JsonlSnapshot(path, ttl_s=0.0, max_records=10)
    assert snap.records() and snap.saturated is True
    _write_jsonl(path, [{"i": 1}, {"i": 2}])  # rewrite smaller
    _bump_mtime(path)
    snap.records()
    assert snap.saturated is False


# ── byte budget ──────────────────────────────────────────────────────


def test_byte_budget_caps_tail_and_sets_saturated(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [{"i": i, "pad": "z" * 1000} for i in range(50)])

    snap = JsonlSnapshot(path, max_bytes=5000)
    out = snap.records()

    assert 0 < len(out) < 50
    assert out[0]["i"] == 49  # newest-first: newest kept, oldest dropped
    assert snap.saturated is True


def test_byte_budget_admits_newest_record_even_if_oversized(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(
        path,
        [{"i": 0, "pad": "z" * 10_000}, {"i": 1, "pad": "z" * 10_000}],
    )

    snap = JsonlSnapshot(path, max_bytes=100)
    out = snap.records()

    # The count guard admits the newest record before the budget check,
    # so the snapshot is never empty for a non-empty file.
    assert [r["i"] for r in out] == [1]
    assert snap.saturated is True


def test_default_byte_budget_leaves_small_files_unsaturated(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [{"i": i} for i in range(5)])

    snap = JsonlSnapshot(path)
    assert len(snap.records()) == 5
    assert snap.saturated is False


def test_slim_transform_applied_to_cached_records_only(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [{"i": 0, "big": "z" * 100}])

    def slim(rec: dict) -> dict:
        return {**rec, "big": rec["big"][:10]}

    snap = JsonlSnapshot(path, slim=slim)
    (rec,) = snap.records()
    assert rec["big"] == "z" * 10
    # Disk record untouched — full fidelity preserved for direct readers.
    on_disk = json.loads(path.read_text().splitlines()[0])
    assert on_disk["big"] == "z" * 100


def test_slim_transform_failure_falls_back_to_raw_record(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [{"i": 0}])

    def broken(rec: dict) -> dict:
        raise RuntimeError("boom")

    snap = JsonlSnapshot(path, slim=broken)
    assert snap.records() == [{"i": 0}]
