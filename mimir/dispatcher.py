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
import collections
import logging
import traceback
from typing import Awaitable, Callable

from .config import Config
from .event_logger import log_event
from .models import AgentEvent
from .scheduler import SCHEDULER_CHANNEL_PREFIX

log = logging.getLogger(__name__)

TurnRunner = Callable[[AgentEvent], Awaitable[object]]
InjectCallback = Callable[[AgentEvent], Awaitable[None]]
# Best-effort observer fired (fire-and-forget) for each enqueued inbound
# event — used for first-contact DM-channel capture (server.py). Must not
# raise into enqueue and must not block it.
EventObserver = Callable[[AgentEvent], Awaitable[None]]


class _ChannelQueue(asyncio.Queue):
    """A per-channel FIFO queue that also supports front-insertion.

    :meth:`putleft_nowait` re-routes an accepted-but-unfolded mid-turn injection
    (chainlink #376) *ahead* of same-channel events that queued after it while
    the turn ran — preserving the dispatcher's within-channel arrival order
    (mimir's #593 review). Built only on ``asyncio.Queue``'s documented
    subclassing extension points (``_init``/``_get``/``_put`` over a deque, as
    ``LifoQueue``/``PriorityQueue`` do); ``put_nowait`` still owns the
    unfinished-task counter and getter wake-up, so ``join()`` accounting stays
    correct.
    """

    _put_front = False

    def _init(self, maxsize: int) -> None:
        self._queue: collections.deque = collections.deque()

    def _get(self):
        return self._queue.popleft()

    def _put(self, item) -> None:
        if self._put_front:
            self._queue.appendleft(item)
        else:
            self._queue.append(item)

    def putleft_nowait(self, item) -> None:
        """Insert ``item`` at the FRONT; identical to ``put_nowait`` otherwise."""
        self._put_front = True
        try:
            self.put_nowait(item)
        finally:
            self._put_front = False


class Dispatcher:
    def __init__(self, config: Config, run_turn: TurnRunner | None = None) -> None:
        self._config = config
        self._run_turn = run_turn
        # chainlink #376 (PR 4): called with the AgentEvent at the moment a
        # mid-turn message is INJECTED into a running turn, so the agent records
        # it in the chat-history buffer at its true arrival time (correctly
        # interleaved with the turn's mid-flight replies) instead of at turn end.
        # None until wired (server.py); injection still works without it.
        self._on_inject: InjectCallback | None = None
        # Best-effort per-event observer (DM-channel capture). Fire-and-forget;
        # tasks tracked in ``_bg_tasks`` so they aren't GC'd mid-flight
        # (the asyncio strong-ref gotcha, chainlink #118).
        self._on_event: EventObserver | None = None
        self._bg_tasks: set[asyncio.Task] = set()
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

    def set_on_inject(self, on_inject: "InjectCallback") -> None:
        """Late-bind the inject callback (chainlink #376 PR 4). Invoked with the
        event when a mid-turn message is folded into a running turn, so the agent
        records it in chat history at its true arrival time."""
        self._on_inject = on_inject

    def set_on_event(self, on_event: "EventObserver") -> None:
        """Late-bind a best-effort per-event observer (DM-channel capture).
        Fired fire-and-forget for each enqueued ``user_message`` — never
        blocks or fails admission."""
        self._on_event = on_event

    def _injection_enabled(self, channel_id: str) -> bool:
        """True iff ``channel_id`` opts into mid-turn message injection
        (chainlink #376). Prefix allow-list from
        ``MIMIR_MIDTURN_INJECTION_CHANNELS``; ``"*"`` enables all. Empty
        (default) keeps the feature off."""
        prefixes = self._config.midturn_injection_channels
        if not prefixes:
            return False
        if "*" in prefixes:
            return True
        return any(channel_id.startswith(p) for p in prefixes)

    async def enqueue(self, event: AgentEvent) -> bool:
        """Returns True if accepted, False if the per-channel queue is full."""
        if self._closed:
            return False

        # First-contact DM-channel capture (best-effort, fire-and-forget):
        # observe inbound user messages without blocking or risking admission.
        if self._on_event is not None and event.trigger == "user_message":
            task = asyncio.create_task(self._on_event(event))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        channel_id = event.channel_id

        # chainlink #376: fold an in-flight follow-up into the running turn
        # instead of queuing it as the next turn. Eligible only for
        # ``user_message`` events on an opted-in channel that has an active turn
        # AND no queued predecessor — gating on an empty queue preserves
        # ordering (a later message must never overtake an already-queued
        # earlier one; that's stricter than ``is_channel_busy()``).
        # chainlink #384: a ``force_new_turn`` event (a message the agent
        # explicitly deferred) is NEVER folded — it must get its own turn. This
        # is the loop guard: a re-enqueued deferred message can't be re-injected.
        if (
            event.trigger == "user_message"
            and not event.extra.get("force_new_turn")
            and self._injection_enabled(channel_id)
            and channel_id in self._in_flight
        ):
            existing = self._queues.get(channel_id)
            if existing is None or existing.qsize() == 0:
                from .mid_turn_injection import inject_message
                if inject_message(channel_id, event) == "injected":
                    await log_event("mid_turn_injected", channel_id=channel_id)
                    # PR 4: record the message in chat history NOW (true arrival
                    # time), so it threads ahead of the running turn's later
                    # replies instead of being appended at turn end. Idempotent
                    # on the agent side, so an un-folded leftover that re-routes
                    # as its own turn won't double-record.
                    if self._on_inject is not None:
                        await self._on_inject(event)
                    return True
                # "no_active_turn" — the turn ended during the race; fall
                # through and enqueue it as a normal next-turn event.

        queue = self._queues.get(channel_id)
        if queue is None:
            queue = _ChannelQueue(maxsize=self._config.max_channel_queue)
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

    def drain_startup_user_messages(self, channel_id: str | None) -> list[AgentEvent]:
        """Remove startup-queued same-channel user messages for the turn that
        just became injectable (chainlink #383).

        This is the complementary half of mid-turn injection's normal
        ``queue-empty`` ordering guard. If a user sends messages back-to-back
        before the first turn has reached ``_in_flight``/``register_inflight``,
        those follow-ups are already sitting behind the current event in the
        per-channel FIFO when ``run_turn`` starts. They are still older than
        the turn's first model boundary, so fold them into the starting turn
        instead of processing them as separate follow-up turns.

        Only drain the contiguous user_message prefix. A non-user predecessor
        is a hard ordering boundary: anything behind it must not overtake it,
        even if it is a user_message. For every queue.get() consumed here we
        call task_done() immediately; the event is no longer a queued turn, and
        the turn record's ``injected_inputs`` is the durable accounting surface.
        """
        if not channel_id or not self._injection_enabled(channel_id):
            return []
        queue = self._queues.get(channel_id)
        if queue is None or queue.qsize() == 0:
            return []

        drained: list[AgentEvent] = []
        while queue.qsize() > 0:
            try:
                next_event = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            # A non-user event is a hard boundary; so is a ``force_new_turn``
            # event (chainlink #384 — a deferred message must get its own turn,
            # never be folded, even into a starting turn).
            if next_event.trigger != "user_message" or next_event.extra.get("force_new_turn"):
                # We consumed one queue item speculatively. Mark that get() done
                # before re-inserting it as queued work; putleft/put_nowait will
                # create the replacement unfinished-task accounting entry.
                queue.task_done()
                if isinstance(queue, _ChannelQueue):
                    queue.putleft_nowait(next_event)
                else:
                    # Plain-queue fallback for tests: preserve the event and stop
                    # rather than dropping it if a test pre-seeded asyncio.Queue.
                    # Production workers cache their queue reference, so swapping
                    # ``self._queues[channel_id]`` would strand a live worker;
                    # production creates only _ChannelQueue via enqueue().
                    restored = _ChannelQueue(maxsize=self._config.max_channel_queue)
                    restored.putleft_nowait(next_event)
                    while True:
                        try:
                            tail_event = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        queue.task_done()
                        restored.put_nowait(tail_event)
                    self._queues[channel_id] = restored
                break
            queue.task_done()
            drained.append(next_event)
        return drained

    def requeue_front(self, events: list[AgentEvent]) -> int:
        """Re-route accepted-but-unfolded mid-turn injections to the FRONT of
        their channel's queue (chainlink #376; mimir's #593 ordering finding).

        ``run_turn`` calls this from its ``finally`` for leftover injections —
        events :func:`mid_turn_injection.inject_message` accepted but the turn
        ended before the next ``before_model`` boundary could fold them. They
        arrived *before* any same-channel event that queued while the turn ran,
        so appending them via :meth:`enqueue` would place them behind those
        later events and break within-channel FIFO. Front-insertion restores
        arrival order; ``events`` keep their relative order. Synchronous (queue
        ops only) — call without ``await``. Returns the count requeued.
        """
        if not events or self._closed:
            return 0
        channel_id = events[0].channel_id
        queue = self._queues.get(channel_id)
        if queue is None or not isinstance(queue, _ChannelQueue):
            # No live queue (worker retired), or a plain queue pre-seeded in a
            # test: (re)create a front-insertable one. A retired worker is
            # respawned below; a test-seeded plain queue is replaced wholesale,
            # which is safe because that path has no concurrent worker.
            queue = _ChannelQueue(maxsize=self._config.max_channel_queue)
            self._queues[channel_id] = queue
            self._high_water_logged.setdefault(channel_id, False)
        requeued = 0
        # appendleft reverses insertion order, so push the events in reverse to
        # land them front-to-back in their original order, ahead of the tail.
        for event in reversed(events):
            try:
                queue.putleft_nowait(event)
            except asyncio.QueueFull:
                # Channel saturated: match enqueue()'s drop-on-full rather than
                # raising inside run_turn's finally. The caller logs the leftover
                # batch; record the drop here too.
                log.warning(
                    "requeue_front dropped a leftover injection on %s (queue full)",
                    channel_id,
                )
                continue
            requeued += 1
        # The in-flight turn's worker is mid-return when this runs (it hasn't
        # looped back to queue.get() yet), so it will pick these up; respawn only
        # if it already retired.
        worker = self._workers.get(channel_id)
        if worker is None or worker.done():
            self._workers[channel_id] = asyncio.create_task(
                self._worker_loop(channel_id), name=f"mimir-worker-{channel_id}"
            )
        return requeued

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
                    # chainlink #302: re-check the queue with NO intervening
                    # await before retiring. A concurrent enqueue() that lands
                    # an event during the ``worker_retired`` log (a yield point)
                    # sees ``worker.done() is False`` and so spawns no
                    # replacement; without these guards the worker would retire
                    # anyway and strand that event — a silent drop, and the
                    # put-but-never-got item keeps ``unfinished_tasks > 0`` so
                    # ``drain()``'s ``queue.join()`` hangs shutdown forever.
                    if queue.qsize() > 0:
                        continue
                    await log_event("worker_retired", channel_id=channel_id, reason="idle")
                    # Re-check after the await (the yield point above): an
                    # enqueue may have raced in during the log. If so, keep
                    # serving rather than retire — that enqueue did not spawn a
                    # replacement (we were not yet done), so this worker owns it.
                    if queue.qsize() > 0:
                        continue
                    # CR2 (agent runtime) fix: clean up per-channel
                    # bookkeeping when the worker retires. Pre-fix the
                    # queue and high-water entry remained in the dicts
                    # forever — ephemeral channel_ids (transient web-
                    # ad-hoc channels, throwaway per-message channels)
                    # accumulated indefinitely. The queue is empty here;
                    # only skip the purge when ``drain()`` may be holding a
                    # reference to this queue while waiting on
                    # ``queue.join()`` (``self._closed``).
                    if not self._closed:
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

    async def drain(self, *, timeout: float | None = None) -> None:
        """Stop accepting new events and wait for in-flight turns to finish.

        ``timeout`` (seconds) bounds the wait (chainlink #510): a deploy SIGTERM
        finishes live turns instead of killing them, but a slow/wedged turn must
        not hang the shutdown past the compose ``stop_grace_period`` (after which
        Docker SIGKILLs straight through the drain). On timeout we log how many
        turns were still running and cancel them so the exit stays deterministic.
        ``None`` / ``0`` = wait unbounded (the prior behavior; used by tests)."""
        self._closed = True
        if not self._workers:
            return
        await log_event(
            "dispatcher_draining", workers=len(self._workers), timeout=timeout,
        )
        # Wait for queues to drain naturally — bounded by ``timeout`` if given.
        join_all = asyncio.gather(
            *(q.join() for q in self._queues.values()), return_exceptions=True
        )
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(join_all, timeout=timeout)
            except asyncio.TimeoutError:
                await log_event(
                    "dispatcher_drain_timeout",
                    timeout=timeout,
                    still_in_flight=len(self._in_flight),
                    queued=sum(q.qsize() for q in self._queues.values()),
                )
                log.warning(
                    "drain timed out after %ss — cancelling %d in-flight turn(s) "
                    "+ %d queued event(s) to exit deterministically",
                    timeout, len(self._in_flight),
                    sum(q.qsize() for q in self._queues.values()),
                )
        else:
            await join_all
        # Cancel any worker still parked on queue.get() (or mid-turn after the
        # drain timeout above).
        for task in self._workers.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
