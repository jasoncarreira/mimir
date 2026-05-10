"""Claude Agent SDK driver (SPEC §4.2, §9.3, §5.6).

Run-turn flow:
1. ``session_manager.touch(channel_id)`` — ensure an active SAGA session,
   reset its idle timer, attach ``saga_session_id`` to the TurnContext.
2. Append inbound to chat_history.jsonl + deques.
3. Flush any pending INDEX.md rebuilds.
4. Pre-message SAGA hook (skipped on ``trigger="saga_session_end"``):
   query SAGA, format hits into the turn prompt, stash atom_ids.
5. Build system + turn prompts. The synthesis turn uses a special template.
6. Set the ``contextvars`` TurnContext so SAGA tools can auto-credit.
7. Invoke ``query()``, collect messages, extract events.
8. Append outbound to chat_history.jsonl.
9. Post-message SAGA hook (skipped on ``trigger="saga_session_end"``):
   call ``mark_contributions`` with the union of pre-injected and
   mid-turn-queried atom_ids, scoped to the active session.
10. End-of-turn INDEX.md rebuild (debounced, SPEC §3.4).
11. Write the turns.jsonl record.

The TurnContext is the only mutable per-turn state. Subagent isolation
is enforced by the SDK spawning each Task as a separate Claude Code
subprocess — that's the load-bearing boundary, not asyncio ContextVars
(which would copy the parent's *reference* to the same TurnContext
object on ``create_task``, not a deep copy). The subprocess gets its
own contextvars from a fresh process. Don't rely on ContextVar
isolation for any in-process subagent that ever materializes; reset
the contextvar at the task boundary if that case arrives.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dispatcher import Dispatcher

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    InMemorySessionStore,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    project_key_for_directory,
)

from . import _context
from .channel_registry import ChannelRegistry
from .config import Config
from .event_logger import log_event
from .feedback import FeedbackLog
from . import git_tracking
from . import health
from .history import MessageBuffer
from .rate_limits import (
    RateLimitStore,
    off_pace_buckets,
    record_api_usage,
    render_off_pace_warning,
    running_on_claude_max,
    snapshot_from_response_bucket,
    snapshot_from_sdk_event,
)
from .session_boundary_log import SessionBoundaryLog, render_session_summaries
from .subagent_stats import (
    aggregate as aggregate_subagents,
    render_subagent_block,
)
from .usage_stats import (
    aggregate as aggregate_usage,
    event_recently_emitted,
    evaluate_cost_rate,
    render_usage_block,
)
from .hooks import make_post_tool_use_hook, make_pre_tool_use_hook
from .index import IndexGenerator
from .loop_detector import LoopDetector
from .memory import load_core
from .models import AgentEvent, TurnContext, TurnRecord, make_turn_id
from .saga_client import SagaClient, SagaError
from .sagatools import (
    _atom_ids_from_response,
    _atoms_in_payload,
    _format_atoms,
    _format_saga_payload,
    _source_atom_ids_from_triples,
)
from .prompts import build_system_prompt, build_turn_prompt
from .scheduler import Scheduler
from .search import Indexer
from ._jsonl_tail import tail_jsonl_records
from .jsonl_snapshot import JsonlSnapshot
from .session_manager import SessionManager
from .turn_hooks import (
    CancelTypingHook,
    GitCommitHook,
    IndexRebuildHook,
    PlanQuotaCaptureHook,
    PostMessageSagaHook,
    RateLimitObserverHook,
    SubagentLifecycleHook,
    TurnLifecycleHook,
    WikiBacklinksHook,
    fire_finalize,
    fire_on_message,
    fire_post_query,
    fire_pre_query,
)
from .shell_jobs import ShellJob, ShellJobRegistry
from .subagent_inbox import SubagentInbox, SubagentResult, render_subagent_updates
from .templates import render_saga_session_end
from .tools import SDK_PRESET_TOOLS, allowed_tool_names, build_mcp_server
from ._streaming_dispatch import StreamingAutoDispatcher
from .turn_logger import TurnLogger, extract_turn_events, truncate_input

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _filter_session_turns(
    turns_path,
    saga_session_id: str,
    *,
    idle_minutes: int = 10,
) -> list[dict]:
    """Read turns.jsonl tail-first and return records with the given
    saga_session_id, in chronological order.

    Pre-2026-05-10 this forward-read the whole file. Synthesis turns
    fire on session-idle (an operator-visible pause point) and the
    file caps at ~250 MB; the read could block the to_thread worker
    for seconds. Now we tail-read newest-first, accumulate matches,
    and break once we've crossed past the saga session's natural
    boundary.

    **Bound shape (post-PR-105 review fix).** A previous version of
    this function used a 200-record count-based break ("stop after
    200 non-matching records past the first match"). That assumed the
    target session's turns are contiguous in append-order, which is
    *not* generally true: turns.jsonl is a single file shared across
    channels and a long-lived session can interleave with hundreds
    of other-session turns. The count heuristic could silently drop
    older session turns from the synthesis prompt.

    Replaced with a **time-based break**: saga ends a session after
    ``idle_minutes`` of no activity, so any record older than
    ``newest_match_ts - 2 * idle_minutes`` cannot belong to this
    session. The 2× margin tolerates clock skew + a single
    out-of-order record at the boundary. Walks back at most
    ``2 * idle_minutes`` worth of file activity past the last match —
    O(session_window) rather than O(file_size).

    Caller (``Agent._render_saga_session_end_prompt``) passes
    ``self._config.saga_session_idle_minutes`` so the bound matches
    saga's actual session policy. The function-level default
    (``idle_minutes=10``) mirrors ``MIMIR_SAGA_SESSION_IDLE_MINUTES``'s
    config default — defensive only; production callers always
    override.
    """
    if not turns_path.is_file():
        return []
    margin_seconds = 2 * idle_minutes * 60
    out: list[dict] = []
    newest_match_ts: datetime | None = None
    try:
        for rec in tail_jsonl_records(turns_path):
            if rec.get("saga_session_id") == saga_session_id:
                out.append(rec)
                ts_str = rec.get("timestamp")
                if isinstance(ts_str, str):
                    try:
                        rec_ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if newest_match_ts is None or rec_ts > newest_match_ts:
                            newest_match_ts = rec_ts
                    except ValueError:
                        pass
            elif newest_match_ts is not None:
                ts_str = rec.get("timestamp")
                if isinstance(ts_str, str):
                    try:
                        rec_ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if (newest_match_ts - rec_ts).total_seconds() > margin_seconds:
                            break
                    except ValueError:
                        # Malformed ts on a non-match — keep scanning;
                        # don't break on a record we can't reason about.
                        pass
    except OSError:
        return []
    out.reverse()  # tail yields newest-first; restore chronological
    return out


# ─── ClaudeSDKClient pool (migration stages 1-4) ────────────────────
#
# Stage 1 of CLAUDE_SDK_CLIENT_MIGRATION.md: route the agent loop through
# a persistent ``ClaudeSDKClient`` instead of one-shot
# ``claude_agent_sdk.query()``. The persistent client keeps the Claude
# Code subprocess warm across turns.
#
# Stage 2: pass ``session_id=ctx.turn_id`` per call so each turn is
# scoped to its own session inside the persistent client. Prior-turn
# history can't leak into the next turn's input — the SDK's session
# store keys conversation state by ``session_id``.
#
# Stage 3: an explicit ``InMemorySessionStore`` is attached to
# ``ClaudeAgentOptions`` and ``Agent`` calls ``store.delete()`` after
# each turn completes. The store is owned by ``Agent`` (not the
# wrapper) because it has to survive client recycles — recycling the
# client when options drift would otherwise reset all in-flight
# session state. Per-turn delete bounds the store size so memory
# stays flat across long-lived processes.
#
# Stage 4 (chainlink #11, this revision): a pool of warm
# ``ClaudeSDKClient`` instances replaces the single-shared-client +
# global ``asyncio.Lock``. The dispatcher allows up to
# ``max_concurrent_turns`` turns to run concurrently, but every turn
# previously serialized on a single SDK lock — undoing the parallelism.
# The pool fixes that.
#
# Pool semantics (locked design):
#   - Lazy fill, max size 10. No pre-warming.
#   - Acquire: hand out an idle client if one is available; else if
#     pool size < max, construct + connect a new client; else await a
#     release.
#   - Fingerprint-tracked drain: the pool tracks a single "current"
#     fingerprint. When ``acquire(options)`` arrives with a different
#     fingerprint, the pool flips its fingerprint, disconnects all
#     idle clients immediately, and marks all in-flight clients
#     stale. In-flight clients finish their current request, then on
#     ``release`` see the stale flag and disconnect rather than
#     re-pooling. New acquires after the flip create fresh clients
#     with the new fingerprint. Net effect: mixed-fingerprint clients
#     are never concurrently in use, and in-flight work is never
#     abruptly disconnected.
#   - ``get_context_usage()`` rides the pool — no dedicated client.
#   - ``shutdown_sdk_client()`` disconnects every client in the pool
#     (idle and in-flight); concurrent acquires after shutdown
#     construct fresh clients (idempotent for tests / repeat startup).
#
# The original Stage 4 spec (CLAUDE_SDK_CLIENT_MIGRATION.md) prescribed
# a ``threading.local`` cache, mirroring saga's old ``_PersistentClaudeCode``
# (a daemon-thread + run_coroutine_threadsafe bridge — itself retired
# in chainlink #47 / Phase 3 of #20 in favor of saga's async-native
# ``_AsyncClaudeRunner``). ``threading.local`` was the wrong shape for
# mimir regardless: mimir runs all turns on a single asyncio event loop
# in a single OS thread, so ``threading.local`` would hand every coroutine
# the same client — same serialization, just without the lock to make
# it visible. The asyncio-aware pool here is the right shape for mimir's
# runtime. The retired threading.local note in CLAUDE_SDK_CLIENT_MIGRATION.md
# captures the reasoning.
#
# Test shape:
#   The module-level ``query`` and ``get_context_usage`` names + their
#   signatures are preserved so existing tests that
#   ``patch("mimir.agent.query", ...)`` keep working unchanged. Tests
#   that exercise the wrapper itself patch ``mimir.agent.ClaudeSDKClient``.

import hashlib


def _options_fingerprint(options: ClaudeAgentOptions) -> str:
    """Hash the options fields that, if changed, require recycling the
    underlying ClaudeSDKClient. Things bound to the client at connect
    time go in here; per-call data (the prompt) does not.

    Hooks/mcp_servers/tools are object references — they don't get
    hashed (mimir's are stable across an Agent's lifetime). The
    fingerprint is conservative: false-positive recycles are cheap,
    false-negatives stale a connected client against new options.
    """
    h = hashlib.sha256()
    h.update((options.system_prompt or "").encode("utf-8"))
    h.update(b"|")
    h.update((options.model or "").encode("utf-8"))
    h.update(b"|")
    h.update((str(getattr(options, "effort", "")) or "").encode("utf-8"))
    h.update(b"|")
    h.update(str(options.permission_mode).encode("utf-8"))
    h.update(b"|")
    h.update(str(getattr(options, "include_partial_messages", False)).encode("utf-8"))
    h.update(b"|")
    h.update(str(options.cwd or "").encode("utf-8"))
    return h.hexdigest()


_POOL_MAX_SIZE = 10


class _PoolEntry:
    """A pool member: a connected (or pending-connect) ClaudeSDKClient
    plus the fingerprint it was constructed with. ``stale`` is set when
    the pool's current fingerprint flips while this client is in use —
    on release the client disconnects instead of being returned.

    ``cell`` is a ``_TurnCell`` whose reference is captured by the
    SDK's forked hook task at first connect. Mutating ``cell.turn_id``
    is how the budget hook learns which turn is currently using THIS
    client (as opposed to some other client serving a different
    channel concurrently)."""

    __slots__ = ("client", "fingerprint", "stale", "cell")

    def __init__(self, client: ClaudeSDKClient, fingerprint: str) -> None:
        self.client = client
        self.fingerprint = fingerprint
        self.stale = False
        from ._context import _TurnCell
        self.cell = _TurnCell()


from saga.async_pool import BoundedAsyncPool


class ClientPool(BoundedAsyncPool[_PoolEntry]):
    """Asyncio-aware pool of ``ClaudeSDKClient`` instances. Replaces
    the single-shared-client + global ``asyncio.Lock`` so concurrent
    turns can run in parallel.

    Inherits the bookkeeping skeleton (max-size validation, lazy
    condition binding, idle stack) from ``saga.async_pool.BoundedAsyncPool``;
    adds mimir-specific policy: fingerprint-keyed drain-on-flip, an
    ``_in_flight`` set for size accounting (so ``size`` reflects both
    idle and in-flight clients), and the release-during-connect lock
    dance for async cold-start.

    Not thread-safe — assumes a single asyncio event loop, which is
    mimir's runtime model. The internal ``asyncio.Condition`` binds to
    the running loop on first use.
    """

    def __init__(self, *, max_size: int = _POOL_MAX_SIZE) -> None:
        super().__init__(max_size)
        self._in_flight: set[_PoolEntry] = set()
        # Pool's "current" fingerprint. None when empty (first acquire
        # sets it). When acquire arrives with a different fingerprint,
        # flips here and the drain happens.
        self._current_fingerprint: str | None = None

    @property
    def size(self) -> int:
        return len(self._idle) + len(self._in_flight)

    async def _drain_idle_for_fingerprint_change(
        self,
        old_fingerprint: str,
        new_fingerprint: str,
    ) -> None:
        """Disconnect every idle client; mark every in-flight client
        stale so it disconnects on release. Caller holds the lock.

        Emits a ``client_pool_drained`` event with both fingerprints
        (truncated to the first 8 chars for readability) and the counts
        of clients affected so an unstable system prompt — the most
        common cause of repeated fingerprint flips — surfaces in
        events.jsonl rather than only as latency drift. See CR#20."""
        idle = self._idle
        self._idle = []
        idle_disconnected = len(idle)
        in_flight_marked_stale = len(self._in_flight)
        for entry in idle:
            try:
                await entry.client.disconnect()
            except Exception:  # noqa: BLE001
                log.exception(
                    "ClaudeSDKClient disconnect failed during pool drain "
                    "(continuing — fresh clients will replace it)"
                )
        for entry in self._in_flight:
            entry.stale = True
        await log_event(
            "client_pool_drained",
            old_fingerprint_8=old_fingerprint[:8],
            new_fingerprint_8=new_fingerprint[:8],
            idle_disconnected=idle_disconnected,
            in_flight_marked_stale=in_flight_marked_stale,
        )

    async def acquire(self, options: ClaudeAgentOptions) -> _PoolEntry:
        """Claim a client for an exclusive request. Caller MUST call
        ``release(entry)`` when done (use ``acquire_ctx`` for
        ``async with`` form)."""
        fingerprint = _options_fingerprint(options)
        cond = self._condition()
        async with cond:
            while True:
                # Fingerprint flip: drain idle clients and mark in-flight
                # ones stale. Re-evaluated on every loop iteration so a
                # late-arriving flip while we're waiting is handled.
                if (
                    self._current_fingerprint is not None
                    and self._current_fingerprint != fingerprint
                ):
                    await self._drain_idle_for_fingerprint_change(
                        self._current_fingerprint, fingerprint
                    )
                    self._current_fingerprint = fingerprint
                elif self._current_fingerprint is None:
                    self._current_fingerprint = fingerprint

                # Hand out an idle client if one is at the current
                # fingerprint. Drained-but-not-yet-disconnected entries
                # would only land here if the drain code path missed
                # them — guard defensively.
                while self._idle:
                    entry = self._idle.pop()
                    if entry.fingerprint == fingerprint and not entry.stale:
                        self._in_flight.add(entry)
                        return entry
                    # Defensive: disconnect a stale/mismatched idle.
                    try:
                        await entry.client.disconnect()
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "ClaudeSDKClient disconnect of stale idle entry failed"
                        )

                # No idle client at current fingerprint; grow if room.
                if self.size < self.max_size:
                    client = ClaudeSDKClient(options=options)
                    entry = _PoolEntry(client, fingerprint)
                    # Reserve the slot before releasing the lock so
                    # size accounting is correct during connect (other
                    # waiters won't double-grow past max_size).
                    self._in_flight.add(entry)
                    cond.release()
                    # Bind THIS client's ``_TurnCell`` to the contextvar
                    # before connect. The SDK forks the hook control
                    # task during ``connect()`` and that task captures
                    # the contextvar value at fork time — capturing the
                    # cell reference means later mutations of
                    # ``entry.cell.turn_id`` (on each acquire/release)
                    # remain visible to the hook task.
                    #
                    # CR2 (agent runtime) fix: capture the Token from
                    # ``set()`` and ``reset()`` it after connect returns.
                    # Pre-fix the binding leaked: every ``asyncio.create_task``
                    # the acquiring task spawned afterward (e.g.
                    # ``_spawn_bg_task`` for log_event) inherited this
                    # cell — so any code that read the contextvar from
                    # the agent's own task would see whichever client
                    # was most recently constructed, not the one
                    # currently in use. The hook task itself is
                    # unaffected: it captured the cell reference at
                    # fork time, so the reset on our side doesn't take
                    # the binding away from the SDK's hook task.
                    from ._context import _current_client_cell
                    cell_token = _current_client_cell.set(entry.cell)
                    try:
                        try:
                            await client.connect()
                        except BaseException:
                            # Connect failed — back out the reservation,
                            # then propagate. We re-acquire the lock so the
                            # bookkeeping mutation is safe and notify any
                            # peer waiting at ``cond.wait()`` below that the
                            # in-flight count went down. Do NOT manually
                            # release here: the surrounding ``async with
                            # cond:`` block's ``__aexit__`` releases when
                            # the exception unwinds. A manual release would
                            # leave the lock unheld and ``__aexit__`` would
                            # then raise ``RuntimeError: Lock is not
                            # acquired`` — masking the real connect failure.
                            await cond.acquire()
                            self._in_flight.discard(entry)
                            cond.notify_all()
                            raise
                        await cond.acquire()
                    finally:
                        # Reset whether connect succeeded or failed. The
                        # SDK's forked hook task already captured the
                        # cell reference at fork time, so the reset
                        # here doesn't take the binding away from it.
                        _current_client_cell.reset(cell_token)
                    # If a fingerprint flip raced our connect, the
                    # current fingerprint has moved on. Mark stale —
                    # the caller will use the client for one request
                    # and on release it'll be disconnected. (We don't
                    # disconnect here because the caller is about to
                    # use the client; mid-acquire-disconnect would
                    # surface as a different error than connect-fail.)
                    if self._current_fingerprint != fingerprint:
                        entry.stale = True
                    return entry

                # At max size, all in flight. Wait for a release.
                await cond.wait()

    async def release(self, entry: _PoolEntry) -> None:
        """Return a client to the pool. If the entry was marked stale
        (fingerprint flipped while it was in flight) or its fingerprint
        no longer matches the pool's current fingerprint, disconnect
        instead of re-pooling."""
        cond = self._condition()
        async with cond:
            self._in_flight.discard(entry)
            stale = entry.stale or entry.fingerprint != self._current_fingerprint
            if not stale:
                # Healthy and current — return to idle.
                self._idle.append(entry)
                cond.notify_all()
                return
            # Stale — wake any waiters (a slot just freed) and fall
            # through to disconnect outside the lock.
            cond.notify_all()
        try:
            await entry.client.disconnect()
        except Exception:  # noqa: BLE001
            log.exception(
                "ClaudeSDKClient disconnect of stale in-flight "
                "entry failed during release (continuing)"
            )

    def acquire_ctx(
        self,
        options: ClaudeAgentOptions,
        *,
        turn_id: str | None = None,
    ) -> "_AcquireContext":
        """Async context manager wrapping ``acquire`` / ``release``.

        ``turn_id`` (optional) is stamped onto the entry's
        ``_TurnCell.turn_id`` while the client is checked out and
        cleared on release. The budget hook reads this to identify
        which turn the calling client belongs to in multi-channel
        concurrent setups (where ``get_only_active_turn`` returns None
        because >1 turn is in flight)."""
        return _AcquireContext(self, options, turn_id=turn_id)

    async def shutdown(self) -> None:
        """Disconnect every client in the pool (idle and in-flight).
        After return, the pool is empty and ready to be re-used (next
        acquire constructs fresh clients). Idempotent — safe to call
        when the pool has never been used.

        In-flight clients are disconnected here as well: graceful
        shutdown means no further work is in flight at the call site
        (server.py awaits ``dispatcher.drain()`` before invoking us),
        so an in-flight entry at this point is unusual but the right
        thing is still to disconnect it."""
        cond = self._condition()
        async with cond:
            idle = self._idle
            in_flight = list(self._in_flight)
            self._idle = []
            self._in_flight = set()
            self._current_fingerprint = None
            entries = idle + in_flight
            cond.notify_all()
        # Disconnect outside the lock so a slow disconnect doesn't
        # block waiters that just got cancelled.
        for entry in entries:
            try:
                await entry.client.disconnect()
            except Exception:  # noqa: BLE001
                log.exception(
                    "ClaudeSDKClient disconnect failed during pool shutdown"
                )


class _AcquireContext:
    """Async context manager returned by ``ClientPool.acquire_ctx``. The
    body runs with an exclusive ``ClaudeSDKClient``; on exit (success
    or exception) the client is released back to the pool."""

    def __init__(
        self,
        pool: ClientPool,
        options: ClaudeAgentOptions,
        *,
        turn_id: str | None = None,
    ) -> None:
        self._pool = pool
        self._options = options
        self._turn_id = turn_id
        self._entry: _PoolEntry | None = None

    async def __aenter__(self) -> ClaudeSDKClient:
        self._entry = await self._pool.acquire(self._options)
        # Stamp the cell so the budget hook can correlate this client's
        # tool calls to the current turn. ``cell`` is a per-client
        # holder captured by the SDK's hook task at first connect; live
        # mutations remain visible across the task boundary.
        self._entry.cell.turn_id = self._turn_id
        return self._entry.client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        entry = self._entry
        self._entry = None
        if entry is not None:
            entry.cell.turn_id = None
            await self._pool.release(entry)


# Module-level singleton pool. Lazy-init so module import doesn't need
# an event loop. Tests reset via ``_reset_pool_for_tests`` (or the
# legacy ``_sdk_client = None`` poke, retained for back-compat with
# fixtures that pre-date the pool — see ``_sdk_client`` shim below).
_pool: ClientPool | None = None


def _get_pool() -> ClientPool:
    global _pool
    if _pool is None:
        _pool = ClientPool()
    return _pool


def _reset_pool_for_tests() -> None:
    """Reset the pool singleton. Tests use this between cases so
    state doesn't leak. Production code never calls this."""
    global _pool
    _pool = None


# Back-compat shims for tests that poke at the pre-pool module-level
# names (``_sdk_client`` / ``_sdk_options_fingerprint`` / ``_sdk_lock``).
# These are now derived views over the pool — assigning ``None`` to
# ``_sdk_client`` resets the pool. Reads return a representative idle
# client when the pool has one, else None. The legacy names are kept
# only for the tests in ``tests/test_agent_sdk_client.py`` that pre-date
# this work; new code reads/writes the pool directly.
class _LegacyClientProxy:
    """Module-level descriptor that maps the old singleton names onto
    pool state. ``agent_mod._sdk_client = None`` clears the pool;
    reading returns the first idle client (or None)."""

    def __get__(self, obj, objtype=None):
        if _pool is None:
            return None
        if _pool._idle:
            return _pool._idle[0].client
        if _pool._in_flight:
            return next(iter(_pool._in_flight)).client
        return None


# Plain module-level None-defaults; tests that read these get the same
# semantics they did pre-pool (None when nothing's connected). Tests
# that write None to reset are handled by the autouse fixture which
# also calls _reset_pool_for_tests when present.
_sdk_client = None
_sdk_options_fingerprint = None
_sdk_lock = None


async def query(
    *,
    prompt: str,
    options: ClaudeAgentOptions,
    session_id: str = "default",
    transport=None,
):
    """ClaudeSDKClient pool wrapper. Async-generator API matches the
    old ``claude_agent_sdk.query()`` shape so call sites and patched
    tests don't have to change.

    ``session_id`` is per-call. The agent loop passes ``ctx.turn_id``
    so each turn gets its own session inside the persistent client —
    prior-turn history can't bleed into the next turn's input.
    Defaults to ``"default"`` so other callers (and tests) that don't
    care keep their stage-1 behavior of a single accumulating session.

    The ``transport`` parameter is accepted but unused — kept for
    signature compatibility with tests that may pass it.
    """
    pool = _get_pool()
    # ``session_id`` here is ``ctx.turn_id`` (the agent loop passes it),
    # so it doubles as the cell-stamp value the budget hook uses to
    # find the right TurnContext under concurrent multi-channel use.
    async with pool.acquire_ctx(options, turn_id=session_id) as client:
        await client.query(prompt, session_id=session_id)
        async for msg in client.receive_response():
            yield msg


async def get_context_usage(options: ClaudeAgentOptions) -> dict | None:
    """Query a pooled persistent client for plan-window utilization.
    Returns the raw response dict (typically containing an ``apiUsage``
    key) or None on failure.

    Rides the same pool as ``query()`` so the probe reuses a warm
    client when one is available. Failures are caught and logged;
    never propagate. Plan-window capture is observability, not
    load-bearing.
    """
    pool = _get_pool()
    try:
        async with pool.acquire_ctx(options) as client:
            try:
                return await client.get_context_usage()
            except Exception:  # noqa: BLE001
                log.exception("client.get_context_usage() raised")
                return None
    except Exception:  # noqa: BLE001
        # Connect failure during acquire (the slot has been backed
        # out by the pool already).
        log.exception("ClaudeSDKClient connect failed in get_context_usage")
        return None


async def shutdown_sdk_client() -> None:
    """Disconnect every ClaudeSDKClient in the pool (called from server
    cleanup). Idempotent — safe to call when no client was ever
    connected."""
    global _pool
    if _pool is None:
        return
    pool = _pool
    await pool.shutdown()
    # After shutdown the pool is reusable, but for parity with the old
    # singleton behavior (``_sdk_client = None`` post-shutdown), drop
    # the singleton so the next acquire gets a fresh, definitively-
    # empty pool.
    _pool = None


class Agent:
    def __init__(
        self,
        config: Config,
        turn_logger: TurnLogger,
        message_buffer: MessageBuffer,
        index_generator: IndexGenerator,
        indexer: Indexer | None = None,
        saga_client: SagaClient | None = None,
        session_manager: SessionManager | None = None,
        scheduler: Scheduler | None = None,
        subagent_inbox: SubagentInbox | None = None,
        channel_registry: ChannelRegistry | None = None,
        dispatcher: "Dispatcher | None" = None,
    ) -> None:
        self._config = config
        self._turn_logger = turn_logger
        self._buffer = message_buffer
        self._indexes = index_generator
        self._indexer = indexer
        self._saga = saga_client
        self._sessions = session_manager
        self._scheduler = scheduler
        self._inbox = subagent_inbox or SubagentInbox()
        self._channels = channel_registry
        # Used by the shell-job completion bridge to enqueue
        # ``shell_job_complete`` events from the waiter thread back into
        # the originating channel's queue. Optional — tests construct an
        # Agent without a dispatcher and shell jobs simply complete
        # silently in that case.
        self._dispatcher = dispatcher
        # Captured at first turn (when we know we're on the asyncio
        # loop). Worker threads use this to schedule coroutines back
        # onto the loop via ``run_coroutine_threadsafe``.
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # CR#10: per-Agent JsonlSnapshot caches for events.jsonl + turns.jsonl.
        # Six per-turn call sites used to re-read these files from scratch each
        # time (feedback assembly, usage block, self-state block, session
        # summaries, subagent aggregate, budget partition). The snapshot wraps
        # ``tail_jsonl_records`` with an mtime-checked TTL so within a single
        # turn (~1s wall-clock) those reads share one cached parse instead of
        # 6+ independent stream-and-decode passes.
        self._events_snapshot = JsonlSnapshot(config.events_log)
        self._turns_snapshot = JsonlSnapshot(config.turns_log)
        self._feedback = FeedbackLog(
            events_path=config.events_log,
            turns_path=config.turns_log,
            default_window_hours=config.feedback_window_hours,
            default_limit_per_polarity=config.feedback_limit_per_polarity,
            events_snapshot=self._events_snapshot,
            turns_snapshot=self._turns_snapshot,
        )
        self._session_boundary_log = SessionBoundaryLog(
            path=config.home / ".mimir" / "session_boundaries.jsonl",
        )
        # Plan-window rate-limit state from RateLimitEvent (5h rolling,
        # 7d plan / Opus / Sonnet, overage). Single JSON file, replaces
        # on each transition.
        self._rate_limits = RateLimitStore(
            path=config.home / ".mimir" / "rate_limits.json",
        )

        # Stage 3: explicit SessionStore + per-turn delete. The store
        # is owned by ``Agent`` (not the SDK-client wrapper) so it
        # survives options-fingerprint client recycles — otherwise
        # recycling the client would reset all session state and
        # break the per-turn delete contract for in-flight turns.
        # ``project_key`` is derived once from ``config.home`` so
        # ``run_turn`` can target the right namespace without
        # re-deriving on every turn.
        self._session_store = InMemorySessionStore()
        self._session_project_key = project_key_for_directory(str(config.home))

        # §12.4: S3-S4 homeostat. Constructed once so the scheduler
        # consults the same instance the prompt's `## Self-state` block
        # is rendered from. Wire into the scheduler immediately so
        # heartbeats fired before the first turn are still arbitrated.
        # chainlink #13: billing-mode aware. Quota-mode installs get a
        # provider list (today: AnthropicQuotaProvider only); pay-as-
        # you-go gets an empty list and the arbiter routes through the
        # existing spike_ratio path.
        from .billing import AnthropicQuotaProvider, BillingMode, QuotaProvider
        from .budget import HomeostaticArbiter
        quota_providers: list[QuotaProvider] = []
        if config.billing_mode is BillingMode.QUOTA:
            quota_providers.append(AnthropicQuotaProvider(self._rate_limits))
        self._arbiter = HomeostaticArbiter(
            home=config.home,
            rate_limit_store=self._rate_limits,
            turns_log=config.turns_log,
            billing_mode=config.billing_mode,
            quota_providers=quota_providers,
            cost_hourly_limit_usd=config.cost_hourly_limit_usd or None,
            cost_spike_ratio=config.cost_rate_spike_ratio or None,
            cost_spike_floor_usd=config.cost_rate_spike_floor_usd or None,
            fallback_model=config.model,
            # CR#10 follow-up (#79 review): without these the homeostat
            # path (called every turn from _assemble_self_state_block)
            # falls through to direct tail_jsonl_records, defeating the
            # cache. Threading the per-Agent snapshots in here makes
            # both _partition_turns and pending_forget_candidates_count
            # share the same parsed list as the rest of the per-turn
            # readers.
            events_snapshot=self._events_snapshot,
            turns_snapshot=self._turns_snapshot,
        )
        if scheduler is not None:
            scheduler._arbiter = self._arbiter

        # Async shell-job registry — backs the bash_async / bash_jobs_list /
        # bash_job_output MCP tools. Constructed once; threads spawned by
        # ``spawn()`` live for the duration of the subprocess they wrap.
        # Files land in ``<home>/logs/bash-jobs/<job_id>.{out,err}``.
        self._shell_jobs = ShellJobRegistry(
            jobs_dir=config.home / "logs" / "bash-jobs",
        )

        self._mcp_server = build_mcp_server(
            config.home,
            indexer=indexer,
            saga_client=saga_client,
            scheduler=scheduler,
            channel_registry=channel_registry,
            message_buffer=message_buffer,
            session_boundary_log=self._session_boundary_log,
            turns_log=config.turns_log,
            turn_logger=self._turn_logger,
            shell_jobs=self._shell_jobs,
            on_shell_job_complete=self._handle_shell_job_complete,
            schedule_from_thread=self._schedule_from_thread,
            mimir_home=config.home,
        )

        # Hooks layer mimir's path confinement + post-write reindex onto the
        # SDK preset tools (Read/Write/Edit/Bash/Glob).
        async def _reindex(rel: str) -> None:
            if self._indexer is not None:
                await self._indexer.reindex_path(rel)

        # Auto-include Claude Code's persisted-output dir in the
        # file-op roots when it exists. The CLI writes overflow
        # bash output (>~32KB) to ``~/.claude/projects/.../tool-results/``
        # and instructs the agent to ``Read`` it; without this root
        # the Read is denied and the agent loses access to its own
        # tool output. See ``_paths.claude_code_persisted_output_root``
        # for the full rationale.
        from ._paths import claude_code_persisted_output_root

        extra_roots = list(config.file_op_extra_roots)
        cc_overflow = claude_code_persisted_output_root()
        if cc_overflow not in extra_roots:
            extra_roots.append(cc_overflow)

        self._pre_tool_hook = make_pre_tool_use_hook(
            config.home,
            extra_roots=extra_roots,
        )
        self._post_tool_hook = make_post_tool_use_hook(
            config.home, _reindex if indexer is not None else None
        )

        # Per-turn lifecycle hooks (CR#15). Each fires at one of four
        # seams in run_turn: pre_query / on_message / post_query /
        # finalize. Order matters — finalize hooks run sequentially, so
        # IndexRebuild must precede GitCommit (the commit needs the
        # regenerated INDEX.md files in the working tree). New
        # extensible behaviors register here rather than editing
        # run_turn directly.
        self._turn_hooks: list[TurnLifecycleHook] = [
            RateLimitObserverHook(
                rate_limits=self._rate_limits,
                is_max_oauth=running_on_claude_max,
            ),
            SubagentLifecycleHook(inbox=self._inbox),
            PlanQuotaCaptureHook(
                capture_fn=self._capture_plan_quota_from_client,
            ),
            PostMessageSagaHook(hook_fn=self._post_message_hook),
            WikiBacklinksHook(home=self._config.home),
            IndexRebuildHook(indexes=self._indexes),
            GitCommitHook(
                home=self._config.home,
                enabled=self._config.git_tracking_enabled,
            ),
            CancelTypingHook(channels=self._channels),
        ]

        # Bounded set for fire-and-forget background tasks (event-log
        # writes, etc.). CPython warns that the result of asyncio.
        # create_task() may be GC'd before it has run; without retaining
        # a reference, short events.jsonl writes can vanish under load
        # before the task body executes. Adding to the set + a discard
        # callback is the standard idiom from PEP 458 / asyncio docs.
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn_bg_task(self, coro) -> asyncio.Task:
        """Schedule a fire-and-forget coroutine while keeping a reference.

        The set membership prevents the task from being garbage-collected
        mid-run; ``add_done_callback`` removes it once the coroutine
        finishes (success or error) so the set bound stays at the
        in-flight count.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ─── Async shell-job completion bridge ──────────────────────────────

    def _schedule_from_thread(self, coro) -> bool:
        """Late-bound bridge for code running in non-loop threads.

        ``spawn_claude_code``'s completion handler runs on the registry's
        waiter thread and needs to ``log_event`` / ``turn_logger.write``
        from there. Mirrors ``_handle_shell_job_complete``.

        **CR2-#5 fix.** Returns True when the coroutine was successfully
        scheduled, False when it was dropped (no loop, loop closed, or
        ``run_coroutine_threadsafe`` raised). On drop, the coroutine
        is explicitly ``close()``-d to suppress Python's
        "coroutine was never awaited" RuntimeWarning, and a structured
        ``log.warning`` is emitted so the silent drop is observable.
        Callers that need to persist accounting beyond the in-process
        log (e.g. spawn completion's synthetic TurnRecord) can check
        the return value and fall back to a sync write path.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            log.warning(
                "schedule_from_thread dropped coroutine (loop unavailable);"
                " caller should persist via fallback path",
            )
            return False
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return True
        except Exception:
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            log.exception(
                "schedule_from_thread failed to dispatch coroutine"
            )
            return False

    def _handle_shell_job_complete(self, job: ShellJob) -> None:
        """Thread-safe bridge: a shell-job waiter thread invokes this when
        the subprocess exits. Schedules the async handler onto the
        captured asyncio loop so we can enqueue a ``shell_job_complete``
        AgentEvent without crossing thread boundaries unsafely.

        Silently no-ops when no loop has been captured yet (e.g. a job
        completed before the first turn ran — shouldn't happen in
        practice but the guard is cheap) or when the dispatcher isn't
        wired (unit tests). Never raises — the registry guards against
        callback errors but a pre-callback raise here would still
        crash the daemon thread.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if self._dispatcher is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._on_shell_job_complete(job),
                loop,
            )
        except Exception:
            # Last-resort guard. Never let a daemon-thread invocation
            # crash the registry.
            log.exception("schedule of shell-job-complete handler failed")

    async def _on_shell_job_complete(self, job: ShellJob) -> None:
        """Async handler that runs on the asyncio loop. Builds a turn
        prompt summarizing the job's exit state + output tails and
        enqueues a ``shell_job_complete`` AgentEvent into the dispatcher.

        Routes back to the channel that spawned the job. When channel_id
        is None (job spawned from a bare scheduled tick that lacked a
        channel reference), the event is silently dropped — there's no
        sensible default routing target.
        """
        if job.channel_id is None:
            await log_event(
                "shell_job_complete_no_channel",
                job_id=job.job_id,
                exit_code=job.exit_code,
            )
            return

        try:
            data = self._shell_jobs.read_output(
                job.job_id, tail_lines=100, stream="both",
            )
        except Exception:
            data = {"stdout_tail": "", "stderr_tail": ""}

        stdout_tail = (data.get("stdout_tail") or "").strip()
        stderr_tail = (data.get("stderr_tail") or "").strip()
        # Bound each stream so a runaway job doesn't blow the prompt budget.
        max_chars = 4000
        if len(stdout_tail) > max_chars:
            stdout_tail = stdout_tail[-max_chars:]
        if len(stderr_tail) > max_chars:
            stderr_tail = stderr_tail[-max_chars:]

        elapsed = round(job.elapsed_seconds, 1)
        body_lines = [
            f"Shell job {job.job_id} complete (status={job.status}, "
            f"exit_code={job.exit_code}, elapsed={elapsed}s).",
            f"Command: {job.command}",
            "",
            "--- stdout tail ---",
            stdout_tail or "(empty)",
            "",
            "--- stderr tail ---",
            stderr_tail or "(empty)",
        ]
        body = "\n".join(body_lines)

        event = AgentEvent(
            trigger="shell_job_complete",
            channel_id=job.channel_id,
            content=body,
            source_id=f"shell_job:{job.job_id}",
            source="system",
            extra={"job_id": job.job_id, "exit_code": job.exit_code},
        )
        try:
            accepted = await self._dispatcher.enqueue(event)
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "shell_job_complete_enqueue_failed",
                job_id=job.job_id,
                error=str(exc)[:500],
            )
            return
        # Success path observability: closes the loop for "did the
        # wake-up actually go out?" without making the operator
        # cross-reference the next turn's prompt against the
        # _spawned event. ``accepted=False`` means the dispatcher
        # rejected (queue full / closed) — distinct from a raise.
        await log_event(
            "shell_job_complete_routed",
            job_id=job.job_id,
            channel_id=job.channel_id,
            exit_code=job.exit_code,
            accepted=accepted,
        )

    def _build_options(self, system_prompt: str) -> ClaudeAgentOptions:
        effort = self._config.effort
        if effort not in ("low", "medium", "high", "max"):
            effort = "high"
        # ``betas``: Anthropic beta headers passed through the SDK to
        # the API. Currently a list of one — the 1M-context flag — when
        # the operator hasn't opted out via MIMIR_CONTEXT_1M=false.
        betas: list = []
        if self._config.context_1m:
            from .usage_stats import CONTEXT_1M_BETA
            betas.append(CONTEXT_1M_BETA)
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            tools=list(SDK_PRESET_TOOLS),
            mcp_servers={"mimir": self._mcp_server},
            betas=betas,
            allowed_tools=allowed_tool_names(
                include_search=self._indexer is not None,
                include_saga=self._saga is not None,
                include_scheduler=self._scheduler is not None,
                include_channels=self._channels is not None,
            ),
            permission_mode="bypassPermissions",
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        # MultiEdit / NotebookEdit kept in the regex so the
                        # path-confinement hook still fires if either becomes
                        # available later; dropping them costs nothing today.
                        matcher="Read|Write|Edit|MultiEdit|Glob|Grep|NotebookEdit",
                        hooks=[self._pre_tool_hook],
                    )
                ],
                "PostToolUse": [
                    HookMatcher(
                        matcher="Write|Edit|MultiEdit",
                        hooks=[self._post_tool_hook],
                    )
                ],
            },
            model=self._config.model,
            effort=effort,
            thinking={"type": "adaptive", "display": "summarized"},
            env=self._config.sdk_env_overrides(),
            cwd=str(self._config.home),
            # Stage 3: per-turn session_id (Stage 2) writes into this
            # store; ``run_turn`` deletes by ``ctx.turn_id`` after the
            # turn completes so memory stays bounded across long-lived
            # processes.
            session_store=self._session_store,
            # Streaming chunks needed when capture_rate_limits is on —
            # the message_start event carries the per-response
            # rate_limits block we want. The extra deltas are cheap
            # (filtered out in the run_turn message loop).
            include_partial_messages=self._config.capture_rate_limits,
        )

    # ---- chat history --------------------------------------------------

    async def _record_inbound(self, event: AgentEvent) -> None:
        if not event.content or event.trigger == "saga_session_end":
            return
        # Shell-job completion wake-ups are payload-shaped (multi-line
        # status + tails), not message-shaped. Skip recording them in
        # chat_history so the recent-activity block doesn't fill with
        # shell dumps; the wake-up turn still sees them in its prompt
        # via build_turn_prompt's shell_job_complete branch.
        if event.trigger == "shell_job_complete":
            return
        kind = "user_message" if event.trigger == "user_message" else "system_note"
        msg = self._buffer.make_message(
            channel_id=event.channel_id,
            kind=kind,
            content=event.content,
            author=event.author,
            author_display=event.author_display or event.author,
            msg_id=event.source_id,
            source=event.source,
        )
        await self._buffer.append(msg)

    async def _record_outbound(
        self, channel_id: str, output: str, *, source: str | None = None
    ) -> None:
        if not output:
            return
        msg = self._buffer.make_message(
            channel_id=channel_id,
            kind="assistant_message",
            content=output,
            source=source,
        )
        await self._buffer.append(msg)

    # chainlink #5 — streaming auto-dispatch callbacks.
    #
    # The streaming dispatcher (mimir._streaming_dispatch) handles the
    # "plan" flush — text emitted before the first tool_use, sent
    # mid-turn so the user sees forward progress. The result flush
    # still goes through _auto_dispatch_or_record at end-of-turn. The
    # callbacks below glue the dispatcher to the standard observability
    # surfaces: events.jsonl + last_assistant_message_id tracking on
    # the TurnContext (so reactions defaulting to "the message I just
    # delivered" land on the plan flush, not on a stale prior reply).
    def _on_streaming_plan_dispatched(
        self,
        ctx: TurnContext,
        event: AgentEvent,
        bridge,
    ):
        async def _cb(plan_text: str, result, directives: tuple) -> None:
            # ``plan_text`` is the *cleaned* plan (actions stripped) —
            # what the user actually saw on the bridge send. Recording
            # this to chat_history keeps Recent-activity consistent
            # with delivery; the raw plan_buffer (with <actions>
            # markup) never makes it into chat_history.
            #
            # ``result`` is None when the plan was directives-only —
            # there was no cleaned text to send via the bridge, but
            # we still need to dispatch the parsed directives so
            # things like an inline ack-react actually fire.
            send_msg_id: str | None = None
            if result is not None:
                send_msg_id = result.message_id
                ctx.last_assistant_message_id = send_msg_id
                text_for_log = (
                    plan_text if len(plan_text) <= 4096
                    else plan_text[:4096] + "…[truncated]"
                )
                await log_event(
                    "auto_dispatch_streamed_plan",
                    channel_id=event.channel_id,
                    bridge=getattr(bridge, "name", None),
                    message_id=result.message_id,
                    chunks=result.chunks,
                    text=text_for_log,
                    actions_in_plan=len(directives),
                )
                # Record the plan chunk to chat_history immediately so
                # Recent activity reflects what was sent. The result
                # chunk is appended later by _auto_dispatch_or_record.
                await self._record_outbound(
                    event.channel_id, plan_text, source=event.source,
                )

            if directives and self._channels is not None:
                from .channeltools import _dispatch_action_directives

                outbound_root = (
                    self._config.home / "attachments" / "outbound"
                )
                # default_message_id: prefer the just-sent plan flush
                # so a bare ``<react>`` lands on it; otherwise fall
                # back to whatever was last delivered on this turn.
                # When the agent emits the inline ack-react pattern
                # (``<react message="<inbound-id>" />``) the explicit
                # message id wins anyway.
                try:
                    directive_results = await _dispatch_action_directives(
                        self._channels,
                        fallback_channel_id=event.channel_id,
                        directives=directives,
                        default_message_id=(
                            send_msg_id or ctx.last_assistant_message_id
                        ),
                        outbound_root=outbound_root,
                    )
                    await log_event(
                        "auto_dispatch_streamed_plan_actions",
                        channel_id=event.channel_id,
                        bridge=getattr(bridge, "name", None),
                        message_id=send_msg_id,
                        directives=directive_results,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "streaming plan-flush directive dispatch failed",
                    )

        return _cb

    def _on_streaming_plan_failed(self, event: AgentEvent, bridge):
        async def _cb(plan_text: str, error: str) -> None:
            await log_event(
                "auto_dispatch_streamed_plan_failed",
                channel_id=event.channel_id,
                bridge=getattr(bridge, "name", None),
                error=error,
                plan_chars=len(plan_text),
            )

        return _cb

    # VSM: S1 outbound — auto-dispatch the SDK's final assistant text
    #                    when the agent didn't call send_message. Without
    #                    this, the natural-text reply is recorded only to
    #                    chat_history (lettabot/muninnbot pattern: the
    #                    final text IS the reply).
    # loop_id: outbound-auto
    async def _auto_dispatch_or_record(
        self, ctx: TurnContext, event: AgentEvent, output: str,
    ) -> None:
        """When the agent emits final text without calling send_message
        explicitly, deliver the text via the channel bridge. Parses
        ``<actions>`` directives the same way ``send_message`` does so
        the agent can react / send-file via natural-text directives too.

        Only fires for user-visible inbound triggers (``user_message``,
        ``react_received``, etc.) on bridge-routable chat channels.
        Heartbeats and other ``scheduled_tick`` events are explicitly
        "end silently" — those still go through ``_record_outbound``
        only. Bench / web-stub bridges that don't actually deliver to
        a third-party service skip auto-dispatch and just record.

        Always writes to chat_history regardless of dispatch outcome —
        so Recent activity reflects what the agent said even when
        delivery failed (the agent self-corrects when it sees a stale
        conversation that doesn't match what it thought it sent)."""
        # Heartbeat / cron tick / synth turn → never auto-dispatch.
        # Heartbeats are explicitly silent; scheduler:* channels would
        # try to dispatch back through the dispatcher to a synthetic
        # channel that has no bridge, generating noise.
        auto_eligible = event.trigger in ("user_message", "react_received")

        dispatched = False
        clean_text = output
        if auto_eligible and self._channels is not None:
            bridge = self._channels.find(event.channel_id)
            # Skip auto-dispatch on benchmark + bench-bridge channels —
            # the bench harness reads the SDK's final text directly.
            if bridge is not None and bridge.name not in ("bench",):
                from .bridges._directives import parse_directives
                from .channeltools import _dispatch_action_directives

                parsed = parse_directives(output)
                clean_text = parsed.clean_text or ""
                outbound_root = (
                    self._config.home / "attachments" / "outbound"
                )
                # Send the cleaned text first so reactions land on the
                # just-sent message id by default. When clean_text is
                # empty (the agent emitted an actions-only reply), skip
                # the main send — directives still fire.
                send_msg_id: str | None = None
                if clean_text.strip():
                    try:
                        result = await self._channels.send(
                            event.channel_id, clean_text,
                        )
                        if result.sent:
                            dispatched = True
                            send_msg_id = result.message_id
                            ctx.last_assistant_message_id = send_msg_id
                            # Cap logged text at 4KB to keep events.jsonl
                            # tight; same threshold the send_message
                            # tool uses (channeltools.py).
                            text_for_log = (
                                clean_text if len(clean_text) <= 4096
                                else clean_text[:4096] + "…[truncated]"
                            )
                            await log_event(
                                "auto_dispatch_ok",
                                channel_id=event.channel_id,
                                bridge=bridge.name,
                                message_id=send_msg_id,
                                chunks=result.chunks,
                                text=text_for_log,
                            )
                        else:
                            log.warning(
                                "auto-dispatch: bridge %r returned sent=False: %s",
                                bridge.name, result.error,
                            )
                            await log_event(
                                "auto_dispatch_failed",
                                channel_id=event.channel_id,
                                bridge=bridge.name,
                                error=result.error,
                            )
                    except Exception as exc:  # noqa: BLE001
                        log.exception("auto-dispatch send failed")
                        await log_event(
                            "auto_dispatch_failed",
                            channel_id=event.channel_id,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                if parsed.directives and (dispatched or not clean_text.strip()):
                    try:
                        directive_results = await _dispatch_action_directives(
                            self._channels,
                            fallback_channel_id=event.channel_id,
                            directives=parsed.directives,
                            default_message_id=send_msg_id
                            or ctx.last_assistant_message_id,
                            outbound_root=outbound_root,
                        )
                        # Directives-only path (no main text) doesn't
                        # land an auto_dispatch_ok above — emit one
                        # here so the audit log captures the activity.
                        if not dispatched and directive_results:
                            await log_event(
                                "auto_dispatch_ok",
                                channel_id=event.channel_id,
                                bridge=bridge.name,
                                message_id=None,
                                chunks=0,
                                text="",
                                directives=directive_results,
                            )
                    except Exception:  # noqa: BLE001
                        log.exception("auto-dispatch directives failed")

        # Always record the cleaned text to chat_history so Recent
        # activity reflects what was sent (or what would have been
        # sent on dispatch failure). Empty cleaned text — directive-
        # only response — still gets a placeholder so the turn
        # registers in history.
        record_text = clean_text if clean_text.strip() else output
        await self._record_outbound(
            event.channel_id, record_text, source=event.source,
        )

    # ---- SAGA hooks ----------------------------------------------------

    def _assemble_usage_block(
        self,
    ) -> tuple[str | None, list[tuple[str, dict]]]:
        """Read turns.jsonl tail-first, aggregate over 1h / 5h / 7d,
        evaluate the cost-rate alert, render the Resource usage prompt
        section. Returns ``(block_text, deferred_events)`` where
        ``block_text`` is None when disabled via config or when no
        turns have been recorded yet, and ``deferred_events`` is a list
        of ``(event_kind, kwargs)`` pairs the caller should ``log_event``
        on the running loop.

        Side effects (deferred): a ``cost_rate_alert`` /
        ``cost_rate_advisory`` entry when a threshold is currently
        tripped AND no prior alert lies within the cooldown window;
        a ``rate_limit_off_pace`` entry under the same shape. The
        annotated alert is included in the rendered block regardless
        of cooldown — the agent should keep seeing the warning while
        the spike persists.

        Note on deferred-vs-immediate: this method runs inside
        ``asyncio.to_thread`` (CR#5 — keeps JSONL scans off the event
        loop). The worker thread has no running loop, so we can't
        ``asyncio.create_task`` from here. The caller flushes
        ``deferred_events`` on the dispatcher loop after the
        to_thread returns."""
        deferred: list[tuple[str, dict]] = []
        if not self._config.usage_block_enabled:
            return None, deferred
        try:
            report = aggregate_usage(
                self._config.turns_log,
                fallback_model=self._config.model,
                snapshot=self._turns_snapshot,
            )
        except Exception:  # noqa: BLE001
            log.exception("usage_stats.aggregate failed; skipping block")
            return None, deferred

        alert = evaluate_cost_rate(
            report,
            hourly_limit_usd=self._config.cost_hourly_limit_usd or None,
            spike_ratio=self._config.cost_rate_spike_ratio or None,
            spike_floor_usd_per_hour=self._config.cost_rate_spike_floor_usd or None,
        )
        if alert is not None:
            # chainlink #13: under quota mode, cost-rate spikes are
            # advisory (logged but not suppressing — the binding
            # constraint is plan-window utilization, which costs
            # nothing to respect). Emit a separate ``cost_rate_advisory``
            # kind so the algedonic feedback renderer can phrase it as
            # "FYI" rather than "scheduled work suppressed".
            from .billing import BillingMode
            advisory = self._config.billing_mode is BillingMode.QUOTA
            event_kind = "cost_rate_advisory" if advisory else "cost_rate_alert"
            if not event_recently_emitted(
                self._config.events_log,
                event_kind,
                cooldown_minutes=self._config.cost_alert_cooldown_minutes,
                snapshot=self._events_snapshot,
            ):
                deferred.append(
                    (
                        event_kind,
                        {
                            "reason": alert.reason,
                            "rate_now_usd_per_hour": round(alert.rate_now_usd_per_hour, 4),
                            "threshold_usd_per_hour": round(alert.threshold_usd_per_hour, 4),
                            "baseline_usd_per_hour": (
                                round(alert.baseline_usd_per_hour, 4)
                                if alert.baseline_usd_per_hour is not None
                                else None
                            ),
                        },
                    )
                )

        # Plan-window state from the SDK's stream. Per-response capture
        # (when capture_rate_limits=True) gives us current state on
        # every turn; the transition-event capture is a backstop.
        plan_lines: list[str] = []
        off_pace_lines: list[str] = []
        try:
            from .rate_limits import render_plan_quota_lines
            current = self._rate_limits.current()
            plan_lines = render_plan_quota_lines(current)
            off_pace = off_pace_buckets(current)
            off_pace_lines = render_off_pace_warning(off_pace)
            # Cooldown-gated rate_limit_off_pace event for the algedonic
            # surfacing. Sustained spikes only re-emit once per cooldown
            # window so the firehose stays clean; the resource block keeps
            # showing the warning every turn while it's tripped.
            if off_pace and not event_recently_emitted(
                self._config.events_log,
                "rate_limit_off_pace",
                cooldown_minutes=self._config.cost_alert_cooldown_minutes,
                snapshot=self._events_snapshot,
            ):
                worst_key, worst_snap, worst_proj = off_pace[0]
                deferred.append(
                    (
                        "rate_limit_off_pace",
                        {
                            "rate_limit_type": worst_key,
                            "utilization": worst_snap.utilization,
                            "on_pace_utilization": round(worst_proj.on_pace_utilization, 4),
                            "hours_until_reset": round(worst_proj.hours_until_reset, 2),
                            "resets_at": worst_snap.resets_at,
                        },
                    )
                )
        except Exception:  # noqa: BLE001
            log.exception("rate_limits read/projection failed")

        # Subagent token spend — climbers / researchers / critics
        # spawned via the Task tool burn tokens that count against the
        # parent's plan budget. Surface so the agent knows where the
        # budget is going (not just "we're at 73% of weekly Opus" but
        # "and a climber that started 2h ago has burned 320k tokens").
        subagent_body: str | None = None
        try:
            subagent_report = aggregate_subagents(self._config.events_log)
            subagent_body = render_subagent_block(subagent_report)
        except Exception:  # noqa: BLE001
            log.exception("subagent_stats aggregate failed")

        # Reflect the active 1M-context beta in the renderer's
        # context-window arithmetic — the `% of` denominator should
        # match the cap actually in effect on the wire.
        active_betas: list[str] = []
        if self._config.context_1m:
            from .usage_stats import CONTEXT_1M_BETA
            active_betas.append(CONTEXT_1M_BETA)

        return (
            render_usage_block(
                report,
                fallback_model=self._config.model,
                budget_5h_usd=self._config.usage_5h_limit_usd or None,
                budget_weekly_usd=self._config.usage_weekly_limit_usd or None,
                alert=alert,
                plan_quota_lines=plan_lines,
                off_pace_warning=off_pace_lines,
                subagent_block=subagent_body,
                betas=active_betas or None,
            ),
            deferred,
        )

    def _assemble_upcoming_block(self) -> str | None:
        """v0.5+ §12.1: feedforward — render the `## Upcoming` block from
        the scheduler's next-N firings + the plan-window reset times.
        Returns None when both sources are empty."""
        try:
            from .upcoming import render_upcoming_block
            return render_upcoming_block(
                scheduler=self._scheduler,
                rate_limit_store=self._rate_limits,
            )
        except Exception:  # noqa: BLE001 — never crash a turn for this
            log.exception("_assemble_upcoming_block failed; skipping")
            return None

    def _assemble_self_state_block(self) -> str | None:
        """v0.5+ §12.4: render the `## Self-state` block — homeostat's
        view of the four layered constraints (plan window / cost rate /
        S3-S4 share / tokens), plus the PR 4b ``uncommitted in
        /mimir-home`` line and per-turn skill bucket telemetry
        (chainlink #15 — moved out of the system prompt's `## Skills`
        block so a skill invocation doesn't bust the prompt-cache
        prefix). Returns None when the homeostat has nothing useful to
        surface yet (fresh agent, no signal) AND no skill telemetry
        either."""
        try:
            arbiter_body = self._arbiter.render_self_state_block()
        except Exception:  # noqa: BLE001
            log.exception("_assemble_self_state_block (arbiter) failed; skipping")
            arbiter_body = None
        git_line = self._assemble_git_status_line()
        skill_body = self._assemble_skill_telemetry_lines()
        parts = [s for s in (arbiter_body, git_line, skill_body) if s]
        if not parts:
            return None
        return "\n".join(parts)

    def _assemble_git_status_line(self) -> str | None:
        """PR 4b: ``- uncommitted in /mimir-home: <count> file(s) — <topN>``
        line for the Self-state block. Catches the case where commits
        failed (secret-scan refused, push outage during operator
        intervention, manual edits left the tree dirty). Suppressed when:

        - ``MIMIR_GIT_TRACKING_ENABLED`` is False (tracking off entirely)
        - count == 0 (the common case — clean tree)
        - ``health.git_status_summary`` errored (returns (0, []))

        Synchronous: runs on the prompt-render path, which is itself
        synchronous. ``git_status_summary`` blocks the caller for ~5-10ms.
        Rendering lives in ``health.render_git_status_line`` so other
        surfaces (CLI, web UI) can reuse the exact same output.
        """
        if not self._config.git_tracking_enabled:
            return None
        try:
            return health.render_git_status_line(self._config.home)
        except Exception:  # noqa: BLE001
            log.exception("_assemble_git_status_line failed; skipping")
            return None

    def _assemble_skill_block(self) -> str | None:
        """v0.5+ §12.3: render the system-prompt `## Skills` block —
        the **install-stable** catalog of skill names. Volatile
        success-rate telemetry (Proven/Risky buckets, ``N/M in window``
        counts) lives in `_assemble_skill_telemetry_lines` and gets
        composed into the per-turn `## Self-state` block instead, so
        the system prompt stays cacheable across turns (chainlink #15).

        Returns None when no skills are seeded.

        Skills enumerated via ``installed_skill_names(home)`` so user-
        installed skills under ``<home>/.claude/skills/`` appear
        alongside bundled ones."""
        try:
            from .skill_outcomes import (
                SkillPinConfig, render_skill_catalog,
            )
            from .skill_defs import installed_skill_names
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            pin = SkillPinConfig.load(
                self._config.home / "state" / "skill-pin.yaml"
            )
            return render_skill_catalog(seeded, pin)
        except Exception:  # noqa: BLE001
            log.exception("_assemble_skill_block failed; skipping")
            return None

    def _assemble_skill_telemetry_lines(self) -> str | None:
        """Per-turn skill bucket telemetry (Proven/Risky with
        ``N/M in window`` counts) for inclusion in the
        ``## Self-state`` block. The install-stable skill catalog
        lives in the system prompt (`_assemble_skill_block`); this
        is the volatile half — pulled out so a skill invocation
        doesn't perturb the system-prompt cache prefix.

        Returns None when no skills have in-window activity."""
        try:
            from .skill_outcomes import (
                SkillPinConfig, aggregate, render_skill_telemetry,
            )
            from .skill_defs import installed_skill_names
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            aggs = aggregate(self._config.turns_log)
            pin = SkillPinConfig.load(
                self._config.home / "state" / "skill-pin.yaml"
            )
            return render_skill_telemetry(seeded, aggs, pin)
        except Exception:  # noqa: BLE001
            log.exception("_assemble_skill_telemetry_lines failed; skipping")
            return None

    async def _assemble_session_summaries(
        self, *, channel_id: str | None
    ) -> str | None:
        """Render the Recent session summaries block. Tries SAGA first
        (chronological recall via /v1/sessions/recent); falls back to
        the local mirror on empty / failure. Returns None when both are
        empty or the section is disabled.

        chainlink #63: each boundary header gets ``(~Xh ago, N turns
        this channel)`` markers; Unfinished sub-bullets get a
        ``[verify before quoting]`` suffix past either staleness
        threshold; later boundaries' ``closed_since`` corrective lists
        drop resolved items from earlier Unfinished renderings.
        """
        count = self._config.recent_boundaries
        if count <= 0:
            return None
        boundaries: list[dict] = []
        if self._saga is not None:
            # CR2-#3: a transient SAGA outage at prompt-assembly time
            # must not crash the turn — degrade to the local-mirror
            # fallback the same way an empty result does. Mirrors the
            # try/except pattern in _assemble_self_state_block.
            try:
                boundaries = await self._saga.recent_session_boundaries(
                    channel_id=channel_id, count=count,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "_assemble_session_summaries: SAGA "
                    "recent_session_boundaries failed; falling back to "
                    "local mirror"
                )
                boundaries = []
        if not boundaries:
            boundaries = self._session_boundary_log.recent(
                channel_id=channel_id, count=count,
            )
        # Per-boundary turn-count: walk the cached turns snapshot once
        # per render. Boundaries are typically 3 — counting from each
        # is cheap (the snapshot iterator is in-memory after the first
        # call within the TTL window).
        turn_counts: dict[str, int] = {}
        if channel_id is not None and boundaries:
            from .session_boundary_log import count_turns_since
            snapshot_records = self._turns_snapshot.records
            for b in boundaries:
                ts = str(b.get("ts") or b.get("timestamp") or "")
                if not ts:
                    continue
                turn_counts[ts] = count_turns_since(
                    self._config.turns_log,
                    channel_id=channel_id,
                    since_ts=ts,
                    snapshot_records=snapshot_records,
                )
        now = datetime.now(tz=timezone.utc)
        return render_session_summaries(
            boundaries,
            now=now,
            turn_counts=turn_counts,
            stale_age_hours=self._config.unfinished_stale_age_hours,
            stale_turns=self._config.unfinished_stale_turns,
        )

    # VSM: S3 — pre-turn retrieval; saga.query feeds likely-relevant
    #          atoms into the prompt before the agent runs. Precondition
    #          for the post-turn credit pass (loop 1.1).
    # loop_id: pre-message
    async def _pre_message_hook(self, ctx: TurnContext, event: AgentEvent) -> str | None:
        """Query SAGA, stash atom_ids on ctx, return a formatted prompt block
        (or None if nothing relevant). Skipped on synthesis turns.

        Floors the per-atom confidence tier at the configured threshold
        (default "medium") because auto-fetched atoms cost system-prompt
        budget every turn — low-confidence noise here is net-negative.

        Passes the last few same-channel messages as ``context`` so SAGA
        can rewrite referential queries ("yes, look for that") into
        self-contained form when its
        ``[retrieval] enable_contextual_rewrite`` flag is on. Filtered by
        the same source allowlist as Recent activity so bench / API /
        scheduler traffic stays out of the rewrite path."""
        if self._saga is None or ctx.trigger == "saga_session_end":
            return None
        if not event.content:
            return None
        min_tier = (self._config.saga_pre_message_min_tier or "").strip() or None
        # Pull last 11 same-channel messages and drop the just-recorded
        # inbound (step 2 of run_turn appended it); SAGA uses up to 10.
        recent = self._buffer.recent_for_channel(
            event.channel_id,
            11,
            source_allowlist=self._config.recent_sources,
        )
        if recent and recent[-1].kind == "user_message" and recent[-1].content == event.content:
            recent = recent[:-1]
        context = [
            {
                "role": "user" if m.kind == "user_message" else "assistant",
                "content": m.content[:400],
            }
            for m in recent[-10:]
            if m.kind in ("user_message", "assistant_message")
        ] or None
        try:
            payload = await self._saga.query(
                event.content,
                top_k=12,
                session_id=ctx.saga_session_id,
                min_confidence_tier=min_tier,
                context=context,
            )
        except SagaError as exc:
            await log_event(
                "saga_query_error",
                where="pre_message_hook",
                error=str(exc),
                turn_id=ctx.turn_id,
            )
            return None
        ids = _atom_ids_from_response(payload)
        # P42: also credit the atoms whose triples were surfaced — when
        # the agent grounds its reply in a triple, the originating atom
        # earned its keep. Same mark_contributions path as for raw atom
        # hits; the post-message hook treats both identically.
        triple_source_ids = _source_atom_ids_from_triples(payload)
        if not ids and not triple_source_ids:
            return None
        seen = set(ctx.saga_atom_ids)
        for aid in list(ids) + triple_source_ids:
            if aid not in seen:
                ctx.saga_atom_ids.append(aid)
                seen.add(aid)
        return _format_saga_payload(payload)

    # VSM: S3 — post-turn credit pass; saga's retrieval ranking learns
    #          which atoms helped (access_log.contributed boost).
    # loop_id: 1.1
    async def _post_message_hook(self, ctx: TurnContext, output: str) -> None:
        """Credit pre-injected ∪ mid-turn-queried atoms via mark_contributions.

        Fallback path: ``send_message`` is the primary credit hook (it
        carries the actual delivered text — see channeltools.py). This hook
        only fires when the turn produced no send_message (e.g. scheduled
        ticks that wrote to memory but didn't reply, or background work).
        Skipped on synthesis turns (the agent already called saga_feedback
        per atom in step 2 of the synthesis prompt).

        CR#19: synthesis turns get a *different* post-check before the
        early return — verify step 3 of the synthesis prompt ran (the
        ``saga_end_session`` tool call). When missing, emit a
        ``saga_synthesis_skipped_boundary`` algedonic so the operator
        sees silent contract failures and the next turn's prompt
        surfaces it as a negative signal."""
        if ctx.trigger == "saga_session_end":
            await self._check_synthesis_boundary_called(ctx)
        if self._saga is None or ctx.trigger == "saga_session_end":
            return
        if ctx.send_message_count > 0:
            # send_message already credited the atoms with the real reply.
            return
        if not ctx.saga_atom_ids or not output:
            return
        atom_ids_for_feedback = list(dict.fromkeys(ctx.saga_atom_ids))
        try:
            await self._saga.feedback(
                atom_ids_for_feedback,  # de-dup, preserve order
                output,
                session_id=ctx.saga_session_id,
            )
            await log_event(
                "saga_feedback_sent",
                where="post_message_hook",
                turn_id=ctx.turn_id,
                n_atoms=len(atom_ids_for_feedback),
                text_len=len(output),
            )
        except SagaError as exc:
            await log_event(
                "saga_feedback_error",
                where="post_message_hook",
                error=str(exc),
                turn_id=ctx.turn_id,
            )

    async def _check_synthesis_boundary_called(self, ctx: TurnContext) -> None:
        """CR#19: synthesis-turn post-check. The synthesis prompt asks
        the agent to call ``saga_end_session`` (step 3); the tool
        handler flips ``ctx.saga_end_session_called`` on success. If
        the flag is still False at end of turn, the agent skipped step
        3 and the next session has no boundary atom — a silent
        contract failure the operator only notices days later via empty
        ``Recent session summaries`` blocks. Emit an algedonic event so
        the failure surfaces immediately and the agent's next turn
        prompt carries it as a negative signal."""
        if ctx.saga_end_session_called:
            return
        await log_event(
            "saga_synthesis_skipped_boundary",
            turn_id=ctx.turn_id,
            saga_session_id=ctx.saga_session_id,
            channel_id=ctx.channel_id,
        )

    # ---- plan-window capture (Stage 5) ------------------------------

    async def _capture_plan_quota_from_client(
        self, options: ClaudeAgentOptions,
    ) -> None:
        """Stage 5 of CLAUDE_SDK_CLIENT_MIGRATION.md: query the shared
        persistent ``ClaudeSDKClient`` for ``apiUsage`` and write each
        window bucket into ``self._rate_limits``. Replaces the
        throwaway-subprocess cron poller (mimir/quota_poller.py) with
        per-turn capture off the warm client we already have.

        ``options`` must be the same options object used for this
        turn's ``query()`` call so the fingerprint matches and the
        warm client is reused — passing fresh options would force a
        disconnect+reconnect, defeating the persistence win.

        No-op when the agent isn't on Claude Max OAuth — direct API
        keys / OpenRouter / Minimax don't surface useful per-window
        utilization, so the probe would just waste an IPC roundtrip.

        Best-effort: failures are caught + logged via
        ``quota_capture_failed`` events and do not propagate. Logs
        ``quota_capture_ok`` on success so the audit trail is the same
        shape the cron poller used (``quota_poll_ok`` / ``quota_poll_failed``
        renamed to ``quota_capture_*`` to mark the new code path).
        """
        if not running_on_claude_max():
            return
        try:
            response = await get_context_usage(options)
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "quota_capture_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        api_usage: dict | None = None
        if isinstance(response, dict):
            api_usage = response.get("apiUsage")
        if not isinstance(api_usage, dict) or not api_usage:
            # Daemon doesn't have plan-window data yet (fresh OAuth
            # session before any messages flow), or the user is on a
            # non-Max plan that doesn't surface this data.
            await log_event(
                "quota_capture_ok",
                windows={},
                note="apiUsage empty",
            )
            return
        try:
            recorded = await record_api_usage(self._rate_limits, api_usage)
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "quota_capture_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        await log_event("quota_capture_ok", windows=recorded)

    # ---- synthesis turn ------------------------------------------------

    async def _build_synthesis_prompt(self, ctx: TurnContext, event: AgentEvent) -> str:
        """For trigger='saga_session_end' — load the synthesis template,
        embed the session's turn window from turns.jsonl.

        When the window is empty (turns.jsonl was rotated past the
        session's records — e.g. a long-idle session with high turn
        throughput in the meantime), the synthesis would produce a
        meaningless boundary atom with no content. Log a warning event
        so the algedonic surface and the operator can see it; the turn
        still runs (the agent gets a chance to write a "no record"
        boundary rather than crash)."""
        saga_session_id = ctx.saga_session_id or event.extra.get("saga_session_id", "")
        idle_minutes = self._config.saga_session_idle_minutes
        # CR#4: synchronous read of turns.jsonl off the event loop. The file
        # can grow to ~250MB at MIMIR_MAX_TURNS=5000 (the default) with large
        # event lists per row; reading it on the loop blocked dispatcher
        # workers (typing
        # indicators, oauth poller cron, scheduled-tick dispatch) for
        # 100-500ms during synthesis. Same pattern as scheduler.list_jobs.
        turns_window = await asyncio.to_thread(
            _filter_session_turns,
            self._config.turns_log,
            saga_session_id,
            idle_minutes=idle_minutes,
        )
        if not turns_window:
            self._spawn_bg_task(
                log_event(
                    "saga_synthesis_empty_window",
                    saga_session_id=saga_session_id,
                    channel_id=event.channel_id,
                    reason="turns.jsonl rotated past this session's records",
                )
            )
        return render_saga_session_end(
            channel_id=event.channel_id,
            saga_session_id=saga_session_id,
            idle_minutes=idle_minutes,
            turns_window=turns_window,
            prompts_dir=self._config.prompts_dir,
        )

    # ---- run_turn ------------------------------------------------------

    async def _build_turn_prompt(
        self,
        ctx: TurnContext,
        event: AgentEvent,
        saga_block: str | None,
        subagent_block: str | None,
    ) -> tuple[str, list]:
        """Assemble the per-turn user-side prompt + the recent-message
        list. Synthesis turns get a dedicated synthesis prompt; everything
        else builds the standard turn prompt with the algedonic /
        session-summary / usage / upcoming / self-state blocks.

        Returns ``(turn_prompt, recent)`` — recent is needed for the
        ``turn_started`` event's ``recent_message_count``.
        """
        if event.trigger == "saga_session_end":
            return await self._build_synthesis_prompt(ctx, event), []

        recent = self._buffer.assemble_recent_activity(
            channel_id=event.channel_id,
            author=event.author,
            recent_per_channel=self._config.recent_per_channel,
            recent_author_cross=self._config.recent_author_cross,
            cross_hours=self._config.recent_cross_hours,
            source_allowlist=self._config.recent_sources,
        )
        feedback_block = (
            self._feedback.recent_block()
            if self._config.feedback_limit_per_polarity > 0
            else None
        )
        session_summaries_block = await self._assemble_session_summaries(
            channel_id=event.channel_id,
        )
        # CR#5: _assemble_usage_block walks turns.jsonl via aggregate_usage;
        # _assemble_self_state_block walks the same file plus events.jsonl
        # via the homeostat snapshot. Move both off the event loop so other
        # tasks (saga writes, message buffering, log_event flushes) aren't
        # blocked during the per-turn JSONL scans.
        usage_block, deferred_usage_events = await asyncio.to_thread(
            self._assemble_usage_block
        )
        # Flush deferred events on the running loop. Can't spawn tasks
        # inside the to_thread worker — it has no loop.
        for event_kind, event_kwargs in deferred_usage_events:
            self._spawn_bg_task(log_event(event_kind, **event_kwargs))
        upcoming_block = self._assemble_upcoming_block()
        self_state_block = await asyncio.to_thread(
            self._assemble_self_state_block,
        )
        turn_prompt = build_turn_prompt(
            event,
            recent_messages=recent,
            saga_block=saga_block,
            subagent_block=subagent_block,
            recent_message_chars=self._config.recent_message_chars,
            resolver=self._buffer.resolver,
            feedback_block=feedback_block,
            session_summaries_block=session_summaries_block,
            usage_block=usage_block,
            upcoming_block=upcoming_block,
            self_state_block=self_state_block,
            # chainlink #23 #26 Option P: surface this turn's
            # saga_session_id so the model can pass it as ``session_id``
            # on saga_query / saga_store / saga_feedback /
            # saga_mark_contributions tool calls. Required because the
            # SDK's MCP dispatch path runs handlers on a fresh task that
            # can't see ``_current_turn``
            # (state/wiki/concepts/mcp-tool-contextvar-stale.md).
            saga_session_id=ctx.saga_session_id,
        )
        return turn_prompt, recent

    def _build_system_prompt(self) -> tuple[str, int]:
        """Assemble the system prompt. Returns ``(prompt, core_block_count)``
        — core_block_count goes into the turn_started event."""
        core_blocks = load_core(self._config.home)
        memory_index_body = self._indexes.read_memory_index()
        skill_block = self._assemble_skill_block()
        return (
            build_system_prompt(
                core_blocks=core_blocks,
                memory_index_body=memory_index_body,
                operator_alert_channel=self._config.operator_alert_channel,
                skill_block=skill_block,
            ),
            len(core_blocks),
        )

    def _make_streaming_dispatcher(
        self, ctx: TurnContext, event: AgentEvent,
    ) -> StreamingAutoDispatcher:
        """chainlink #5: streaming auto-dispatcher. Eligibility matches
        ``_auto_dispatch_or_record`` exactly — user-facing inbound on a
        registered, non-bench bridge. On heartbeat / scheduler ticks
        the dispatcher is created disabled and ``observe()`` is a no-op."""
        streaming_eligible = (
            event.trigger in ("user_message", "react_received")
            and self._channels is not None
        )
        streaming_bridge = (
            self._channels.find(event.channel_id)
            if streaming_eligible and self._channels is not None
            else None
        )
        return StreamingAutoDispatcher(
            channel_id=event.channel_id,
            bridge=streaming_bridge,
            on_plan_dispatched=self._on_streaming_plan_dispatched(
                ctx, event, streaming_bridge,
            ),
            on_plan_failed=self._on_streaming_plan_failed(
                event, streaming_bridge,
            ),
            eligible=streaming_eligible,
        )

    async def _run_query_loop(
        self,
        ctx: TurnContext,
        event: AgentEvent,
        *,
        prompt: str,
        options: ClaudeAgentOptions,
        streaming_dispatcher: StreamingAutoDispatcher,
    ) -> tuple[list, str | None]:
        """Drive the SDK ``query()`` async-generator. Manages the
        ``_current_turn`` contextvar binding, fires per-message hooks +
        the streaming dispatcher, and ensures the per-turn
        SessionStore entry is dropped on exit (success OR crash).

        Returns ``(messages, error)``. ``error`` is non-None when the
        query loop raised; downstream phases (post_query, finalize) can
        still run on a None ctx but generally short-circuit on error.
        """
        token = _context.set_current_turn(ctx)
        messages: list = []
        error: str | None = None
        try:
            try:
                async for msg in query(
                    prompt=prompt,
                    options=options,
                    session_id=ctx.turn_id,
                ):
                    messages.append(msg)
                    # observe() is best-effort — failures inside the
                    # streaming dispatcher must never break the main
                    # message loop. Logged inside the dispatcher.
                    try:
                        await streaming_dispatcher.observe(msg)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "streaming_dispatcher.observe failed; "
                            "continuing message loop",
                        )
                    # Per-message turn-lifecycle hooks (rate-limit
                    # observer, subagent lifecycle). These are
                    # exception-isolated by ``fire_on_message``; a
                    # broken hook can't sink the loop.
                    await fire_on_message(
                        self._turn_hooks, ctx, event, msg,
                    )
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                log.exception("query() failed for turn %s", ctx.turn_id)
        finally:
            _context.reset_current_turn(token)
            # Stage 3: drop this turn's session entries from the store
            # so memory stays flat across long-lived processes. Runs
            # in finally so a query() crash still cleans up. Adapter
            # delete failures are logged but don't propagate — the
            # turn record + observability path matters more than a
            # leaked session entry.
            try:
                await self._session_store.delete(
                    {
                        "project_key": self._session_project_key,
                        "session_id": ctx.turn_id,
                    }
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "session_store.delete failed for turn %s "
                    "(continuing — entry will be evicted on next process restart)",
                    ctx.turn_id,
                )
        return messages, error

    async def _record_turn_outbound(
        self,
        ctx: TurnContext,
        event: AgentEvent,
        output: str,
        streaming_dispatcher: StreamingAutoDispatcher,
        streaming_active_for_log: bool,
    ) -> None:
        """Outbound chat_history write. Skipped on synthesis turns
        (no user-facing reply) and when send_message already attempted
        a dispatch (success OR failure — failure stays in events.jsonl
        rather than being claimed in chat_history).

        chainlink #5: when the streaming dispatcher already flushed a
        "plan" chunk, this delivers only the "result" half (text after
        the first tool_use). Plan-only replies emit a diagnostic event
        instead of double-recording.
        """
        if not (
            output
            and event.trigger != "saga_session_end"
            and ctx.send_message_attempts == 0
        ):
            return
        if streaming_active_for_log:
            result_text = streaming_dispatcher.result_text()
            if result_text:
                await self._auto_dispatch_or_record(ctx, event, result_text)
            else:
                # Plan was streamed but the result chunk is empty
                # (no post-tool text, or the only text was an
                # actions-only plan). Audit the case so the log
                # shows the streamed plan was the entire reply.
                #
                # No _record_outbound here: the streaming-plan
                # callback already wrote the cleaned plan text to
                # chat_history when there was a real bridge send,
                # and writing the raw plan_buffer here would
                # double-record (and would inject raw <actions>
                # markup the user never saw on the directives-only
                # plan path). Bridge-failure / directives-only
                # cases have nothing user-visible left to record.
                await log_event(
                    "auto_dispatch_streamed_only_plan",
                    channel_id=event.channel_id,
                    plan_chars=len(streaming_dispatcher.state.plan_text()),
                )
            return
        await self._auto_dispatch_or_record(ctx, event, output)

    async def run_turn(self, event: AgentEvent) -> TurnRecord:
        # Capture the running loop on first turn — worker threads (shell
        # job waiters, etc.) use this to schedule coroutines back onto
        # the loop via run_coroutine_threadsafe. Idempotent: subsequent
        # calls re-bind to the same loop, which is fine.
        #
        # CR#10: invalidate the snapshots at the start of every turn so
        # this turn's prompt-assembly reads pick up the previous turn's
        # writes (TurnRecord + log_events) regardless of whether the
        # snapshot's TTL has elapsed. Cheaper than tracking writes in
        # log_event / turn_logger directly; the within-turn reads share
        # one parse, the across-turn writes are visible immediately.
        self._events_snapshot.invalidate()
        self._turns_snapshot.invalidate()

        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass  # not on a loop (shouldn't happen, but the guard is cheap)

        ctx = TurnContext(
            turn_id=make_turn_id(),
            session_id=event.channel_id,
            trigger=event.trigger,
            channel_id=event.channel_id,
            started_at=time.monotonic(),
            tool_call_budget=self._config.tool_call_budget,
            loop_detector=LoopDetector(
                soft_limit=self._config.send_loop_soft_limit,
                hard_limit=self._config.send_loop_hard_limit,
                similarity_threshold=self._config.send_loop_similarity,
            ),
        )

        # SAGA session attach. Synthesis turns already carry the closed
        # session's id; for everything else we touch (creating if needed).
        if event.trigger == "saga_session_end":
            ctx.saga_session_id = event.extra.get("saga_session_id")
        elif self._sessions is not None:
            session = await self._sessions.touch(event.channel_id)
            ctx.saga_session_id = session.saga_session_id
            self._sessions.increment_turn_count(event.channel_id)

        # Inbound → chat_history (skipped for synthesis turns; their
        # "input" is the turn-window block, not a real user message).
        await self._record_inbound(event)

        # Flush any out-of-band INDEX changes before reading the index.
        await self._indexes.flush()

        # Pre-message SAGA hook — produces a "Possibly relevant memories"
        # block to slot into the turn prompt — and drain any background-
        # subagent notifications (SPEC §4.4).
        saga_block = await self._pre_message_hook(ctx, event)
        pending_subagents = await self._inbox.drain(event.channel_id)
        subagent_block = (
            render_subagent_updates(pending_subagents)
            if pending_subagents else None
        )

        turn_prompt, recent = await self._build_turn_prompt(
            ctx, event, saga_block, subagent_block,
        )
        system_prompt, core_block_count = self._build_system_prompt()

        await log_event(
            "turn_started",
            turn_id=ctx.turn_id,
            channel_id=ctx.channel_id,
            trigger=ctx.trigger,
            saga_session_id=ctx.saga_session_id,
            core_block_count=core_block_count,
            recent_message_count=len(recent),
            saga_atoms_pre_injected=len(ctx.saga_atom_ids),
        )

        # Build options once — the same object is passed to query() and
        # then reused for the post-turn plan-quota capture so the
        # ClaudeSDKClient fingerprint matches and the warm client is
        # reused (see _capture_plan_quota_from_client).
        options = self._build_options(system_prompt)
        streaming_dispatcher = self._make_streaming_dispatcher(ctx, event)

        await fire_pre_query(self._turn_hooks, ctx, event)

        messages, error = await self._run_query_loop(
            ctx, event,
            prompt=turn_prompt,
            options=options,
            streaming_dispatcher=streaming_dispatcher,
        )

        # chainlink #5: when streaming was active and a plan flush
        # already went out, pass streaming_active=True so intermediate
        # text is demoted to reasoning events (turns.jsonl mirrors what
        # the user actually saw). For all other turns this is a no-op
        # — same single-flush behavior as before.
        streaming_active_for_log = (
            streaming_dispatcher.streamed_plan
            and not streaming_dispatcher.disabled_by_explicit_send
        )
        events_list, output = extract_turn_events(
            messages, streaming_active=streaming_active_for_log,
        )
        duration_ms = int((time.monotonic() - ctx.started_at) * 1000)

        # ResultMessage extraction — last-wins so retries that emit more
        # than one ResultMessage in the same turn keep the final value.
        result_msg: ResultMessage | None = None
        for msg in messages:
            if isinstance(msg, ResultMessage):
                result_msg = msg

        # Post-query lifecycle hooks: plan-quota capture, post-message
        # saga hook, and any §12-shaped follow-ons that read the
        # completed message stream.
        await fire_post_query(
            self._turn_hooks, ctx, event,
            messages=messages, output=output, error=error, options=options,
        )

        # Outbound chat_history write — happens here (between
        # post_query hooks and finalize) because the post-message saga
        # hook may consume the output text without persisting it.
        # ``_record_turn_outbound`` short-circuits on synthesis turns
        # and on any prior send_message attempt.
        await self._record_turn_outbound(
            ctx, event, output,
            streaming_dispatcher=streaming_dispatcher,
            streaming_active_for_log=streaming_active_for_log,
        )

        record = TurnRecord(
            ts=_utc_now_iso(),
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
            saga_session_id=ctx.saga_session_id,
            trigger=ctx.trigger,
            channel_id=ctx.channel_id,
            input=truncate_input(turn_prompt),
            saga_atom_ids=list(dict.fromkeys(ctx.saga_atom_ids)),
            events=events_list,
            output=output,
            duration_ms=duration_ms,
            error=error,
            result_subtype=result_msg.subtype if result_msg else None,
            result_is_error=result_msg.is_error if result_msg else None,
            stop_reason=result_msg.stop_reason if result_msg else None,
            num_turns=result_msg.num_turns if result_msg else None,
            total_cost_usd=result_msg.total_cost_usd if result_msg else None,
            usage=result_msg.usage if result_msg else None,
            permission_denials=list(result_msg.permission_denials or []) if result_msg else [],
        )
        await self._turn_logger.write(record)

        await log_event(
            "turn_finished",
            turn_id=ctx.turn_id,
            channel_id=ctx.channel_id,
            duration_ms=duration_ms,
            error=error,
            result_subtype=result_msg.subtype if result_msg else None,
            result_is_error=result_msg.is_error if result_msg else None,
            total_cost_usd=result_msg.total_cost_usd if result_msg else None,
            event_count=len(events_list),
            output_chars=len(output),
            saga_atoms_total=len(record.saga_atom_ids),
        )

        # Finalize lifecycle hooks: index rebuild, git commit,
        # cancel-typing. Order matters — IndexRebuild precedes
        # GitCommit so auto-regenerated INDEX.md files are part of the
        # same commit as the writes that triggered them.
        await fire_finalize(self._turn_hooks, ctx, event, record)
        return record
