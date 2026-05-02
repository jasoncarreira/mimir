"""Tests for the §12.1 Upcoming feedforward prompt section."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from mimir.upcoming import (
    UpcomingItem,
    _humanize_delta,
    collect_plan_window_resets,
    collect_scheduler_jobs,
    render_upcoming_block,
)


def test_humanize_delta_buckets():
    assert _humanize_delta(timedelta(seconds=-1)) == "now"
    assert _humanize_delta(timedelta(seconds=0)) == "now"
    assert _humanize_delta(timedelta(seconds=42)) == "42s"
    assert _humanize_delta(timedelta(seconds=300)) == "5m"
    assert _humanize_delta(timedelta(minutes=3, seconds=20)) == "3m 20s"
    assert _humanize_delta(timedelta(hours=2, minutes=15)) == "2h 15m"
    assert _humanize_delta(timedelta(hours=4)) == "4h"
    assert _humanize_delta(timedelta(days=2, hours=3)) == "2d 3h"
    assert _humanize_delta(timedelta(days=2)) == "2d"


def test_render_upcoming_returns_none_when_empty():
    """Both sources empty → no Upcoming section rendered (caller can
    suppress the header rather than print an empty block)."""
    out = render_upcoming_block(scheduler=None, rate_limit_store=None)
    assert out is None


def test_collect_scheduler_jobs_skips_jobs_without_next_run():
    """APScheduler can return jobs with next_run_time=None when
    they're paused or hit a final state. Skip those."""
    job1 = SimpleNamespace(id="scheduler:heartbeat", next_run_time=None)
    fake_apsched = SimpleNamespace(get_jobs=lambda: [job1])
    fake_wrapper = SimpleNamespace(_scheduler=fake_apsched)
    items = collect_scheduler_jobs(fake_wrapper)
    assert items == []


def test_collect_scheduler_jobs_orders_by_when_and_strips_prefix():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    j1 = SimpleNamespace(id="scheduler:heartbeat",
                         next_run_time=base + timedelta(minutes=30))
    j2 = SimpleNamespace(id="scheduler:reflection",
                         next_run_time=base + timedelta(days=2))
    j3 = SimpleNamespace(id="saga-consolidate",  # not prefixed
                         next_run_time=base + timedelta(hours=1))
    fake_wrapper = SimpleNamespace(
        _scheduler=SimpleNamespace(get_jobs=lambda: [j2, j1, j3])
    )
    items = collect_scheduler_jobs(fake_wrapper, limit=10)
    assert [it.label for it in items] == [
        "heartbeat",         # +30m, scheduler: prefix stripped
        "saga-consolidate",  # +1h, no prefix to strip
        "reflection",        # +2d
    ]


def test_collect_scheduler_jobs_respects_limit():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    jobs = [
        SimpleNamespace(id=f"scheduler:job{i}", next_run_time=base + timedelta(minutes=i))
        for i in range(10)
    ]
    fake_wrapper = SimpleNamespace(_scheduler=SimpleNamespace(get_jobs=lambda: jobs))
    items = collect_scheduler_jobs(fake_wrapper, limit=3)
    assert len(items) == 3


def test_collect_plan_window_resets():
    """Plan-window snapshots come from rate_limits.RateLimitStore.current()."""

    @dataclass
    class _Snap:
        resets_at: int | None
        utilization: float | None = None

    base_ts = int(datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc).timestamp())
    snapshots = {
        "five_hour": _Snap(resets_at=base_ts + 3600, utilization=0.45),
        "seven_day_opus": _Snap(resets_at=base_ts + 86400 * 3, utilization=0.78),
        # one with no resets_at — should be skipped, not crash
        "no_reset": _Snap(resets_at=None, utilization=0.1),
    }
    fake_store = SimpleNamespace(current=lambda: snapshots)
    items = collect_plan_window_resets(fake_store)
    assert len(items) == 2
    # Sorted by reset time ascending → five_hour first
    assert "five_hour" in items[0].label
    assert "currently 45% used" in items[0].detail
    assert "seven_day_opus" in items[1].label
    assert "currently 78% used" in items[1].detail


def test_render_upcoming_includes_both_groups():
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    fake_sched = SimpleNamespace(_scheduler=SimpleNamespace(get_jobs=lambda: [
        SimpleNamespace(id="scheduler:heartbeat",
                        next_run_time=base + timedelta(minutes=30)),
    ]))

    @dataclass
    class _Snap:
        resets_at: int
        utilization: float
    fake_store = SimpleNamespace(current=lambda: {
        "five_hour": _Snap(
            resets_at=int((base + timedelta(hours=2)).timestamp()),
            utilization=0.6,
        ),
    })
    out = render_upcoming_block(
        scheduler=fake_sched, rate_limit_store=fake_store, now=base,
    )
    assert out is not None
    assert "**Scheduled work**" in out
    assert "heartbeat" in out
    assert "**Plan-window resets**" in out
    assert "five_hour" in out
    # Both should render their "in Xm" / "in Xh" relative time.
    assert "in 30m" in out
    assert "in 2h" in out


def test_render_upcoming_handles_one_group_only():
    """If only the scheduler has items (or only plan-window), render
    just that group — no empty headers."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    fake_sched = SimpleNamespace(_scheduler=SimpleNamespace(get_jobs=lambda: [
        SimpleNamespace(id="scheduler:heartbeat",
                        next_run_time=base + timedelta(minutes=15)),
    ]))
    out = render_upcoming_block(
        scheduler=fake_sched, rate_limit_store=None, now=base,
    )
    assert out is not None
    assert "Scheduled work" in out
    assert "Plan-window resets" not in out
