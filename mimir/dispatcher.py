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
from typing import Awaitable, Callable

from .config import Config
from .event_logger import log_event
from .models import AgentEvent

log = logging.getLogger(__name__)

TurnRunner = Callable[[AgentEvent], Awaitable[object]]


class Dispatcher:
    def __init__(self, config: Config, run_turn: TurnRunner | None = None) -> None:
        self._config = config
        self._run_turn = run_turn
        self._queues: dict[str, asyncio.Queue[AgentEvent]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_turns)
        self._closed = False
        self._high_water_logged: dict[str, bool] = {}

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
                    return

                async with self._semaphore:
                    if self._run_turn is None:
                        await log_event(
                            "error",
                            where="dispatcher.worker",
                            channel_id=channel_id,
                            message="run_turn not bound; event dropped",
                        )
                        queue.task_done()
                        continue
                    try:
                        await self._run_turn(event)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("run_turn raised for %s", channel_id)
                        await log_event(
                            "error",
                            where="dispatcher.worker",
                            channel_id=channel_id,
                            message=f"{type(exc).__name__}: {exc}",
                        )
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
