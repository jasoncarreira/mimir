"""Loop-stall watchdog (chainlink #587).

The scheduler's loop-lag monitor detects a stall only *after* it clears (it
wakes up late), by which point the blocking callback has finished — so it can
log ``lag_s`` + active turns, but not *what* blocked. This watchdog captures the
event loop thread's stack DURING the stall.

A daemon thread polls a heartbeat that a loop task refreshes. While the loop is
blocked the heartbeat can't advance, so once it goes stale past the threshold
the loop is blocked *right now* — the thread grabs the loop thread's current
stack via ``sys._current_frames()`` and stashes it. The scheduler's loop-lag
monitor then drains the captures and attaches them to the ``scheduler_loop_lag``
event, naming the offending call.

Off-loop by construction (a plain thread), so it keeps working precisely when
the loop is wedged. Capturing another thread's stack is best-effort and
read-only; it never interrupts the loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any

log = logging.getLogger(__name__)


def stack_is_idle(stack: str) -> bool:
    """True when a captured loop-stall stack shows the loop parked in the
    selector (idle, waiting for I/O) rather than stuck in a real callback.

    An idle-parked capture means the loop wasn't running on-loop work when it
    was sampled, so the late wake came from the process being descheduled (e.g.
    a Docker-Desktop / VM scheduling hiccup) rather than a mimir hot path. An
    empty capture is treated as idle too: during a whole-process stall the
    watchdog thread (same process) is frozen as well and only samples after the
    loop resumes, so it has nothing — or a post-resume ``select`` frame — to
    show. Used by the scheduler's loop-lag monitor to route those to an
    informational event instead of the negative algedonic signal.
    """
    if not stack:
        return True
    file_lines = [ln for ln in stack.splitlines() if ln.lstrip().startswith("File ")]
    if not file_lines:
        return True
    leaf = file_lines[-1]
    return "selectors.py" in leaf and " in select" in leaf


def stack_is_apscheduler_logging_flush(stack: str) -> bool:
    """True when APScheduler's coroutine runner is blocked in logging flush.

    APScheduler wraps coroutine jobs with synchronous INFO logs immediately
    before and after ``await job.func(...)``. If stdout/stderr/container log I/O
    stalls, the loop-stack capture points at ``run_coroutine_job`` plus stdlib
    logging/``Handler.flush`` rather than at the job body. This signature is an
    observability hint, not a reason to hide the stall: the work is still
    synchronous on the event loop, but the next fix should target scheduler
    logging/sink behavior instead of the named job body.
    """
    return (
        "apscheduler/executors/base.py" in stack
        and "in run_coroutine_job" in stack
        and "logging/__init__.py" in stack
        and "self.flush()" in stack
    )


class LoopStallWatchdog:
    def __init__(
        self,
        *,
        stall_threshold_s: float = 1.0,
        poll_s: float = 0.25,
        max_captures: int = 32,
        stack_limit: int = 25,
    ) -> None:
        self._threshold = stall_threshold_s
        self._poll = poll_s
        self._stack_limit = stack_limit
        self._beat = time.monotonic()
        self._loop_thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._captures: deque[dict[str, Any]] = deque(maxlen=max_captures)
        self._last_captured_beat: float | None = None

    def beat(self) -> None:
        """Mark the loop alive — called from :meth:`heartbeat_loop` on the loop."""
        self._beat = time.monotonic()

    def start_thread(self, loop_thread_id: int | None = None) -> None:
        """Start the off-loop watchdog thread. Call from the loop thread so the
        default ``loop_thread_id`` (this thread) is the one whose stack we grab."""
        self._loop_thread_id = (
            loop_thread_id if loop_thread_id is not None else threading.get_ident()
        )
        self._beat = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="loop-stall-watchdog", daemon=True
        )
        self._thread.start()

    async def heartbeat_loop(self) -> None:
        """Refresh the heartbeat on the loop until stopped/cancelled."""
        try:
            while not self._stop.is_set():
                self.beat()
                await asyncio.sleep(self._poll)
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        self._stop.set()

    def drain(self) -> list[dict[str, Any]]:
        """Return + clear the captures recorded since the last drain."""
        out = list(self._captures)
        self._captures.clear()
        return out

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self._check_once(time.monotonic())
            except Exception:  # noqa: BLE001 — a watchdog must never crash the thread
                log.debug("loop-stall watchdog check failed", exc_info=True)

    def _check_once(self, now: float) -> None:
        """Capture the loop's stack if the heartbeat is stale past the threshold.

        One capture per stall: keyed on the (frozen-while-blocked) heartbeat
        value, so a single long stall yields one capture, not a burst.
        """
        beat = self._beat
        if now - beat <= self._threshold:
            return
        if self._last_captured_beat == beat:
            return
        self._last_captured_beat = beat
        self._captures.append(
            {"stall_s": round(now - beat, 3), "stack": self._capture_loop_stack()}
        )

    def _capture_loop_stack(self) -> str:
        tid = self._loop_thread_id
        if tid is None:
            return ""
        frame = sys._current_frames().get(tid)
        if frame is None:
            return ""
        return "".join(traceback.format_stack(frame, limit=self._stack_limit))
