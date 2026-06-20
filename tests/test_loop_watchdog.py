"""Tests for the loop-stall watchdog (chainlink #587)."""

from __future__ import annotations

import threading

from mimir.loop_watchdog import LoopStallWatchdog


def _wd() -> LoopStallWatchdog:
    wd = LoopStallWatchdog(stall_threshold_s=1.0)
    # Capture THIS thread's stack so the test exercises the real path.
    wd._loop_thread_id = threading.get_ident()
    return wd


def test_no_capture_under_threshold():
    wd = _wd()
    wd._beat = 100.0
    wd._check_once(now=100.5)  # 0.5s stale, threshold 1.0 → nothing
    assert wd.drain() == []


def test_captures_loop_stack_when_stalled():
    wd = _wd()
    wd._beat = 100.0
    wd._check_once(now=101.6)  # 1.6s stale → capture
    caps = wd.drain()
    assert len(caps) == 1
    assert caps[0]["stall_s"] >= 1.0
    # The captured stack is this test's own call stack (real frame capture).
    assert "test_captures_loop_stack_when_stalled" in caps[0]["stack"]


def test_one_capture_per_stall_then_again_after_recovery():
    wd = _wd()
    wd._beat = 100.0
    wd._check_once(now=102.0)  # stall → capture
    wd._check_once(now=103.0)  # same frozen heartbeat → no duplicate
    assert len(wd.drain()) == 1

    wd._beat = 200.0  # heartbeat advanced = loop recovered
    wd._check_once(now=202.0)  # a new stall → a new capture
    assert len(wd.drain()) == 1


def test_drain_clears():
    wd = _wd()
    wd._beat = 100.0
    wd._check_once(now=102.0)
    assert len(wd.drain()) == 1
    assert wd.drain() == []  # cleared


def test_capture_is_noop_without_loop_thread_id():
    wd = LoopStallWatchdog()  # never started → no loop thread id
    assert wd._capture_loop_stack() == ""


def test_start_thread_then_stop_is_safe():
    wd = LoopStallWatchdog(poll_s=0.01)
    wd.start_thread()
    try:
        assert wd._loop_thread_id == threading.get_ident()
    finally:
        wd.stop()
