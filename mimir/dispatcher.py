"""Per-channel queue + worker pool with global concurrency cap (SPEC §4.5).

One worker task per active channel ⇒ within-channel ordering is preserved.
Workers acquire a global ``asyncio.Semaphore`` before invoking ``run_turn``,
so cross-channel concurrency is bounded by ``MIMIR_MAX_CONCURRENT_TURNS``.
Idle workers (no event for ``MIMIR_WORKER_IDLE_TIMEOUT_S``) retire and
respawn on next event.

Workers swallow exceptions to events.jsonl so a single bad turn never wedges
a channel — see SPEC §4.5 "Workers swallow exceptions".
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Awaitable, Callable

from .config import Config
from .event_logger import log_event
from .models import AgentEvent
from .scheduler import SCHEDULER_CHANNEL_PREFIX

log = logging.getLogger(__name__)

TurnRunner = Callable[[AgentEvent], Awaitable[object]]


class Dispatcher:
    def __init__(self, config: Config, run_turn: TurnRunner | None = None) -> None:
        self._config = config
        self._run_turn = run_turn
        self._queues: dict[str, asyncio.Queue[AgentEvent]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_turns)
        # S2-3 fix: cross-job mutex for scheduler-triggered turns. The
        # per-channel queue already serializes within a channel; this
        # extra lock serializes across the ``scheduler:*`` channel family
        # so the weekly reflect (Sun 06:00) and an hourly heartbeat
        # firing at the same minute don't race on shared state files
        # (``state/heartbeat-backlog.md``,
        # ``state/learnings-pending.md``, ``state/proposed-changes.md``).
        # Non-scheduler turns are unaffected — user_message / react /
        # poller events still run concurrently up to
        # ``max_concurrent_turns``.
        self._scheduler_tick_lock = asyncio.Lock()
        self._closed = False
        self._high_water_logged: dict[str, bool] = {}
        # Channels with a turn currently inside run_turn (held the semaphore,
        # past the dispatch into the agent). Used by SessionManager to decide
        # whether the worker is parked on queue.get() — so the synthesis-fire
        # timer can defer when work is in flight (SPEC §5.6).
        self._in_flight: set[str] = set()

    def is_channel_busy(self, channel_id: str) -> bool:
        """True iff a turn is in flight for ``channel_id`` or events are queued
        for it. False means the worker is parked on ``queue.get()`` (or the
        channel has no worker at all)."""
        queue = self._queues.get(channel_id)
        if queue is not None and queue.qsize() > 0:
            return True
        return channel_id in self._in_flight

    def set_run_turn(self, run_turn: TurnRunner) -> None:
        """Late-bind the runner. Used to break the agent ↔ dispatcher
        ↔ scheduler initialization cycle in server.py."""
        self._run_turn = run_turn

    async def enqueue(self, event: AgentEvent) -> bool:
        """Returns True if accepted, False if the per-channel queue is full."""
        if self._closed:
            return False

        channel_id = event.channel_id
        queue = self._queues.get(channel_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._config.max_channel_queue)
            self._queues[channel_id] = queue
            self._high_water_logged[channel_id] = False

        if queue.full():
            await log_event(
                "event_admission_rejected",
                channel_id=channel_id,
                queue_depth=queue.qsize(),
                reason="channel_queue_full",
            )
            return False

        await queue.put(event)
        depth = queue.qsize()
        if depth > 10 and not self._high_water_logged[channel_id]:
            await log_event("event_queue_high_water", channel_id=channel_id, depth=depth)
            self._high_water_logged[channel_id] = True
        elif depth <= 5:
            self._high_water_logged[channel_id] = False

        await log_event("event_queued", channel_id=channel_id, trigger=event.trigger)

        # Ensure a worker is running for this channel.
        worker = self._workers.get(channel_id)
        if worker is None or worker.done():
            self._workers[channel_id] = asyncio.create_task(
                self._worker_loop(channel_id), name=f"mimir-worker-{channel_id}"
            )
        return True

    async def _worker_loop(self, channel_id: str) -> None:
        queue = self._queues[channel_id]
        idle_timeout = self._config.worker_idle_timeout_s
        try:
            # Don't gate the loop on `self._closed` — workers must drain queued
            # items even after dispatcher.drain() has set the flag. The drain
            # path waits on queue.join() (count → 0) and then cancels the
            # worker while it is parked on queue.get().
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    await log_event("worker_retired", channel_id=channel_id, reason="idle")
                    # CR2 (agent runtime) fix: clean up per-channel
                    # bookkeeping when the worker retires. Pre-fix the
                    # queue and high-water entry remained in the dicts
                    # forever — ephemeral channel_ids (transient web-
                    # ad-hoc channels, throwaway per-message channels)
                    # accumulated indefinitely. Only purge if the
                    # queue is actually empty AND not closed (the
                    # ``drain()`` path may be holding a reference to
                    # this queue while waiting on ``queue.join()``).
                    if queue.qsize() == 0 and not self._closed:
                        self._queues.pop(channel_id, None)
                        self._high_water_logged.pop(channel_id, None)
                        # CR2 completion: also pop the worker task entry.
                        # Pre-fix, _workers[channel_id] held the now-done
                        # Task indefinitely — the original CR2 comment called
                        # out exactly this case ("ephemeral channel_ids...
                        # accumulated indefinitely") but the pop was missing
                        # for _workers. The worker is mid-return here, so
                        # popping its own entry is safe.
                        self._workers.pop(channel_id, None)
                    return

                # CR2-#4: ``queue.task_done()`` MUST be called for every
                # ``queue.get()`` regardless of how the dispatch ends —
                # including when ``_run_turn`` raises CancelledError
                # (e.g. on shutdown via ``drain()``). Previously the
                # ``task_done()`` was outside the try/except, so a
                # CancelledError raised inside ``_run_turn`` skipped it
                # — leaving the queue's unfinished count > 0 and
                # blocking ``await queue.join()`` in ``drain()``
                # forever. The outer ``except asyncio.CancelledError``
                # below logs and re-raises; ``task_done()`` lives in a
                # ``finally`` here so neither propagation path skips it.
                try:
                    async with self._semaphore:
                        if self._run_turn is None:
                            await log_event(
                                "error",
                                where="dispatcher.worker",
                                channel_id=channel_id,
                                message="run_turn not bound; event dropped",
                            )
                            continue
                        self._in_flight.add(channel_id)
                        try:
                            # S2-3: serialize scheduler-triggered turns
                            # across the whole ``scheduler:*`` channel
                            # family. Non-scheduler turns skip the lock
                            # and run concurrently up to the global
                            # semaphore's cap.
                            if channel_id.startswith(SCHEDULER_CHANNEL_PREFIX):
                                async with self._scheduler_tick_lock:
                                    await self._run_turn(event)
                            else:
                                await self._run_turn(event)
                        except Exception as exc:  # noqa: BLE001
                            log.exception("run_turn raised for %s", channel_id)
                            await log_event(
                                "error",
                                where="dispatcher.worker",
                                channel_id=channel_id,
                                message=f"{type(exc).__name__}: {exc}",
                                traceback=traceback.format_exc(),
                            )
                        finally:
                            self._in_flight.discard(channel_id)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            await log_event("worker_cancelled", channel_id=channel_id)
            raise

    async def drain(self) -> None:
        """Wait for in-flight turns to finish. Stop accepting new events."""
        self._closed = True
        if not self._workers:
            return
        await log_event("dispatcher_draining", workers=len(self._workers))
        # Wait for queues to drain naturally.
        await asyncio.gather(
            *(q.join() for q in self._queues.values()), return_exceptions=True
        )
        # Cancel any worker still parked on queue.get().
        for task in self._workers.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
