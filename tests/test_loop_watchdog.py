"""Tests for the loop-stall watchdog (chainlink #587)."""

from __future__ import annotations

import signal
import threading

import pytest

from mimir import loop_watchdog
from mimir.loop_watchdog import (
    LoopStallWatchdog,
    stack_is_apscheduler_logging_flush,
    stack_is_idle,
)


def _wd() -> LoopStallWatchdog:
    wd = LoopStallWatchdog(stall_threshold_s=1.0)
    # Capture THIS thread's stack so the test exercises the real path.
    wd._loop_thread_id = threading.get_ident()
    return wd


# ─── stack_is_idle (chainlink #682) ──────────────────────────────────


def test_stack_is_idle_true_for_selector_parked_loop():
    stack = (
        '  File "/app/mimir/server.py", line 1873, in main\n'
        '  File "/usr/local/lib/python3.11/asyncio/base_events.py", line 1898, in _run_once\n'
        '  File "/usr/local/lib/python3.11/selectors.py", line 468, in select\n'
    )
    assert stack_is_idle(stack) is True


def test_stack_is_idle_false_for_real_on_loop_frame():
    stack = (
        '  File "/app/mimir/health_probe.py", line 351, in probe_pwd\n'
        '  File "/usr/local/lib/python3.11/subprocess.py", line 1885, in _execute_child\n'
    )
    assert stack_is_idle(stack) is False


def test_stack_is_idle_true_for_empty_capture():
    # The watchdog (same process) is frozen through a whole-process stall and
    # samples nothing — treated as idle/host, not a real on-loop block.
    assert stack_is_idle("") is True


def test_stack_is_apscheduler_logging_flush_matches_live_signature():
    stack = (
        '  File "/workspace/mimir/.venv/lib/python3.11/site-packages/'
        'apscheduler/executors/base.py", line 181, in run_coroutine_job\n'
        '    retval = await job.func(*job.args, **job.kwargs)\n'
        '  File "/usr/local/lib/python3.11/logging/__init__.py", line 1489, in info\n'
        '    self._log(INFO, msg, args, **kwargs)\n'
        '  File "/usr/local/lib/python3.11/logging/__init__.py", line 1114, in emit\n'
        '    self.flush()\n'
    )

    assert stack_is_apscheduler_logging_flush(stack) is True
    assert stack_is_apscheduler_logging_flush(
        '  File "/app/mimir/health_probe.py", line 351, in probe_pwd\n'
    ) is False


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


def test_sustained_stall_fires_structured_alert_once_off_loop(monkeypatch):
    events = []
    alerts = []
    main_thread_id = threading.get_ident()
    monkeypatch.setattr(loop_watchdog, "log_event_sync",
                        lambda event_type, **payload: events.append((event_type, payload)))
    wd = LoopStallWatchdog(
        stall_threshold_s=0.01, poll_s=0.005, alert_threshold_s=0.03,
        notify=lambda stall_s, stack: alerts.append(
            (stall_s, threading.get_ident())),
    )
    wd.start_thread(loop_thread_id=main_thread_id)
    wd._beat = 100.0
    try:
        threading.Event().wait(0.15)
    finally:
        wd.stop()
    assert len(alerts) == 1
    assert alerts[0][0] >= 0.03
    assert alerts[0][1] != main_thread_id
    assert len(events) == 1
    assert events[0][0] == "loop_stall_watchdog_fired"


def test_sustained_stall_self_terminate_is_opt_in(monkeypatch):
    restarts = []
    monkeypatch.setattr(loop_watchdog, "log_event_sync", lambda *a, **kw: None)
    wd = LoopStallWatchdog(
        stall_threshold_s=1.0, alert_threshold_s=5.0,
        terminate_on_stall=True, notify=lambda stall_s, stack: None,
        terminate=lambda pid, sig: restarts.append((pid, sig)),
    )
    wd._beat = 100.0
    wd._check_once(now=106.0)
    assert restarts == [(1, signal.SIGTERM)]


def test_sustained_stall_does_not_terminate_by_default(monkeypatch):
    monkeypatch.setattr(loop_watchdog, "log_event_sync", lambda *a, **kw: None)
    wd = LoopStallWatchdog(
        stall_threshold_s=1.0, alert_threshold_s=5.0,
        notify=lambda stall_s, stack: None,
        terminate=lambda pid, sig: pytest.fail("termination must remain opt-in"),
    )
    wd._beat = 100.0
    wd._check_once(now=106.0)
