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
import json
import logging
import time
from datetime import datetime, timezone

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
from .session_manager import SessionManager
from .subagent_inbox import SubagentInbox, SubagentResult, render_subagent_updates
from .templates import render_saga_session_end
from .tools import SDK_PRESET_TOOLS, allowed_tool_names, build_mcp_server
from .turn_logger import TurnLogger, extract_turn_events, truncate_input

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _filter_session_turns(turns_path, saga_session_id: str) -> list[dict]:
    """Read turns.jsonl and return all records with the given saga_session_id."""
    if not turns_path.is_file():
        return []
    out: list[dict] = []
    try:
        with turns_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("saga_session_id") == saga_session_id:
                    out.append(rec)
    except OSError:
        return []
    return out


# ─── ClaudeSDKClient wrapper (migration stages 1-3) ─────────────────
#
# Stage 1 of CLAUDE_SDK_CLIENT_MIGRATION.md: route the agent loop through
# a shared, persistent ``ClaudeSDKClient`` instead of one-shot
# ``claude_agent_sdk.query()``. The shared client keeps the Claude Code
# subprocess warm across turns.
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
# The lifecycle:
#   - First call: instantiate ``ClaudeSDKClient(options=...)`` and
#     ``connect()``.
#   - Subsequent calls with matching options-fingerprint: reuse.
#   - Options changed (system_prompt drifted, model swapped, etc.):
#     disconnect + reconnect with the new options. Recycle cost is one
#     subprocess restart (~1s) per fingerprint flip. In practice the
#     fingerprint is very stable (system prompt blocks change slowly).
#   - ``shutdown_sdk_client()`` is called from the server's cleanup
#     hook to release the subprocess on graceful shutdown.
#
# Test shape:
#   The module-level ``query`` name is preserved so existing tests that
#   ``patch("mimir.agent.query", new=fake_query)`` keep working
#   unchanged. Tests that exercise the wrapper itself patch
#   ``mimir.agent.ClaudeSDKClient``.
#
# An ``asyncio.Lock`` serializes calls — ``ClaudeSDKClient`` is
# single-threaded by design. Stage 4 swaps the singleton for a
# threading.local cache to lift the serialization.

import hashlib

_sdk_lock: asyncio.Lock | None = None
_sdk_client: ClaudeSDKClient | None = None
_sdk_options_fingerprint: str | None = None


def _get_sdk_lock() -> asyncio.Lock:
    """Lazy-create the lock so it binds to the running loop. Module
    import shouldn't require an event loop."""
    global _sdk_lock
    if _sdk_lock is None:
        _sdk_lock = asyncio.Lock()
    return _sdk_lock


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


async def query(
    *,
    prompt: str,
    options: ClaudeAgentOptions,
    session_id: str = "default",
    transport=None,
):
    """Stage 1+2 ClaudeSDKClient wrapper. Async-generator API matches
    the old ``claude_agent_sdk.query()`` shape so call sites and
    patched tests don't have to change.

    Stage 2: ``session_id`` is now a per-call parameter. The agent
    loop passes ``ctx.turn_id`` so each turn gets its own session
    inside the persistent client — prior-turn history can't bleed
    into the next turn's input. Defaults to ``"default"`` so other
    callers (and tests) that don't care keep their stage-1 behavior
    of a single accumulating session.

    No cleanup yet — the SDK's ``InMemorySessionStore`` keeps each
    turn's history for the lifetime of the client. Stage 3 wires an
    explicit store + per-turn delete.

    The ``transport`` parameter is accepted but unused — kept for
    signature compatibility with tests that may pass it.
    """
    global _sdk_client, _sdk_options_fingerprint
    fingerprint = _options_fingerprint(options)
    lock = _get_sdk_lock()
    async with lock:
        if _sdk_client is None or _sdk_options_fingerprint != fingerprint:
            if _sdk_client is not None:
                try:
                    await _sdk_client.disconnect()
                except Exception:  # noqa: BLE001
                    log.exception(
                        "ClaudeSDKClient disconnect failed during recycle "
                        "(continuing — fresh client about to replace it)"
                    )
                _sdk_client = None
            client = ClaudeSDKClient(options=options)
            await client.connect()
            _sdk_client = client
            _sdk_options_fingerprint = fingerprint
        client = _sdk_client
        assert client is not None  # for type-checkers
        await client.query(prompt, session_id=session_id)
        async for msg in client.receive_response():
            yield msg


async def get_context_usage(options: ClaudeAgentOptions) -> dict | None:
    """Stage 5: query the shared persistent client for plan-window
    utilization. Returns the raw response dict (typically containing
    an ``apiUsage`` key) or None if no client is connected and we
    couldn't safely connect one for a usage probe.

    Reuses the same fingerprint-keyed singleton as ``query()`` so the
    probe rides on whatever client the agent loop already warmed.
    When no client is connected yet (first call before any ``query()``
    has run), this connects one — same recycle semantics as ``query()``.
    Failures are caught and logged; never propagate. Plan-window
    capture is observability, not load-bearing.
    """
    global _sdk_client, _sdk_options_fingerprint
    fingerprint = _options_fingerprint(options)
    lock = _get_sdk_lock()
    async with lock:
        if _sdk_client is None or _sdk_options_fingerprint != fingerprint:
            if _sdk_client is not None:
                try:
                    await _sdk_client.disconnect()
                except Exception:  # noqa: BLE001
                    log.exception(
                        "ClaudeSDKClient disconnect failed during recycle "
                        "for get_context_usage (continuing — fresh client "
                        "about to replace it)"
                    )
                _sdk_client = None
            try:
                client = ClaudeSDKClient(options=options)
                await client.connect()
            except Exception:  # noqa: BLE001
                log.exception("ClaudeSDKClient connect failed in get_context_usage")
                return None
            _sdk_client = client
            _sdk_options_fingerprint = fingerprint
        client = _sdk_client
        assert client is not None  # for type-checkers
        try:
            return await client.get_context_usage()
        except Exception:  # noqa: BLE001
            log.exception("client.get_context_usage() raised")
            return None


async def shutdown_sdk_client() -> None:
    """Disconnect the shared ClaudeSDKClient (called from server cleanup).
    Idempotent — safe to call when no client was ever connected."""
    global _sdk_client, _sdk_options_fingerprint
    lock = _get_sdk_lock()
    async with lock:
        if _sdk_client is not None:
            try:
                await _sdk_client.disconnect()
            except Exception:  # noqa: BLE001
                log.exception("ClaudeSDKClient disconnect failed during shutdown")
            _sdk_client = None
            _sdk_options_fingerprint = None


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
        self._feedback = FeedbackLog(
            events_path=config.events_log,
            turns_path=config.turns_log,
            default_window_hours=config.feedback_window_hours,
            default_limit_per_polarity=config.feedback_limit_per_polarity,
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
        from .budget import HomeostaticArbiter
        self._arbiter = HomeostaticArbiter(
            home=config.home,
            rate_limit_store=self._rate_limits,
            turns_log=config.turns_log,
            cost_hourly_limit_usd=config.cost_hourly_limit_usd or None,
            cost_spike_ratio=config.cost_rate_spike_ratio or None,
            cost_spike_floor_usd=config.cost_rate_spike_floor_usd or None,
            fallback_model=config.model,
        )
        if scheduler is not None:
            scheduler._arbiter = self._arbiter

        self._mcp_server = build_mcp_server(
            config.home,
            indexer=indexer,
            saga_client=saga_client,
            scheduler=scheduler,
            channel_registry=channel_registry,
            message_buffer=message_buffer,
            session_boundary_log=self._session_boundary_log,
            turns_log=config.turns_log,
        )

        # Hooks layer mimir's path confinement + post-write reindex onto the
        # SDK preset tools (Read/Write/Edit/Bash/Glob).
        async def _reindex(rel: str) -> None:
            if self._indexer is not None:
                await self._indexer.reindex_path(rel)

        self._pre_tool_hook = make_pre_tool_use_hook(
            config.home,
            extra_roots=list(config.file_op_extra_roots),
        )
        self._post_tool_hook = make_post_tool_use_hook(
            config.home, _reindex if indexer is not None else None
        )

    def _build_options(self, system_prompt: str) -> ClaudeAgentOptions:
        effort = self._config.effort
        if effort not in ("low", "medium", "high", "max"):
            effort = "high"
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            tools=list(SDK_PRESET_TOOLS),
            mcp_servers={"mimir": self._mcp_server},
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

    def _assemble_usage_block(self) -> str | None:
        """Read turns.jsonl tail-first, aggregate over 1h / 5h / 7d,
        evaluate the cost-rate alert, render the Resource usage prompt
        section. Returns None when disabled via config or when no turns
        have been recorded yet.

        Side effect: emits a ``cost_rate_alert`` event when a threshold
        is currently tripped AND no prior alert lies within the cooldown
        window. The annotated alert is included in the rendered block
        regardless of cooldown — the agent should keep seeing the
        warning while the spike persists."""
        if not self._config.usage_block_enabled:
            return None
        try:
            report = aggregate_usage(
                self._config.turns_log,
                fallback_model=self._config.model,
            )
        except Exception:  # noqa: BLE001
            log.exception("usage_stats.aggregate failed; skipping block")
            return None

        alert = evaluate_cost_rate(
            report,
            hourly_limit_usd=self._config.cost_hourly_limit_usd or None,
            spike_ratio=self._config.cost_rate_spike_ratio or None,
            spike_floor_usd_per_hour=self._config.cost_rate_spike_floor_usd or None,
        )
        if alert is not None and not event_recently_emitted(
            self._config.events_log,
            "cost_rate_alert",
            cooldown_minutes=self._config.cost_alert_cooldown_minutes,
        ):
            asyncio.create_task(
                log_event(
                    "cost_rate_alert",
                    reason=alert.reason,
                    rate_now_usd_per_hour=round(alert.rate_now_usd_per_hour, 4),
                    threshold_usd_per_hour=round(alert.threshold_usd_per_hour, 4),
                    baseline_usd_per_hour=(
                        round(alert.baseline_usd_per_hour, 4)
                        if alert.baseline_usd_per_hour is not None
                        else None
                    ),
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
            ):
                worst_key, worst_snap, worst_proj = off_pace[0]
                asyncio.create_task(
                    log_event(
                        "rate_limit_off_pace",
                        rate_limit_type=worst_key,
                        utilization=worst_snap.utilization,
                        on_pace_utilization=round(worst_proj.on_pace_utilization, 4),
                        hours_until_reset=round(worst_proj.hours_until_reset, 2),
                        resets_at=worst_snap.resets_at,
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

        return render_usage_block(
            report,
            fallback_model=self._config.model,
            budget_5h_usd=self._config.usage_5h_limit_usd or None,
            budget_weekly_usd=self._config.usage_weekly_limit_usd or None,
            alert=alert,
            plan_quota_lines=plan_lines,
            off_pace_warning=off_pace_lines,
            subagent_block=subagent_body,
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
        S3-S4 share / tokens). Returns None when the homeostat has
        nothing useful to surface yet (fresh agent, no signal)."""
        try:
            return self._arbiter.render_self_state_block()
        except Exception:  # noqa: BLE001
            log.exception("_assemble_self_state_block failed; skipping")
            return None

    def _assemble_skill_block(self) -> str | None:
        """v0.5+ §12.3: render the system-prompt `## Skills` block —
        proven / untried / risky buckets ordered by recent success
        rate. Returns None when no skills are seeded.

        Skills enumerated via ``installed_skill_names(home)`` so user-
        installed skills under ``<home>/.claude/skills/`` appear
        alongside bundled ones — the ranker isn't limited to the
        package-bundled set."""
        try:
            from .skill_outcomes import (
                SkillPinConfig, aggregate, render_skill_block,
            )
            from .skill_defs import installed_skill_names
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            aggs = aggregate(self._config.turns_log)
            pin = SkillPinConfig.load(
                self._config.home / "state" / "skill-pin.yaml"
            )
            return render_skill_block(seeded, aggs, pin)
        except Exception:  # noqa: BLE001
            log.exception("_assemble_skill_block failed; skipping")
            return None

    async def _assemble_session_summaries(
        self, *, channel_id: str | None
    ) -> str | None:
        """Render the Recent session summaries block. Tries SAGA first
        (chronological recall via /v1/sessions/recent); falls back to
        the local mirror on empty / failure. Returns None when both are
        empty or the section is disabled."""
        count = self._config.recent_boundaries
        if count <= 0:
            return None
        boundaries: list[dict] = []
        if self._saga is not None:
            boundaries = await self._saga.recent_session_boundaries(
                channel_id=channel_id, count=count,
            )
        if not boundaries:
            boundaries = self._session_boundary_log.recent(
                channel_id=channel_id, count=count,
            )
        return render_session_summaries(boundaries)

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
        per atom in step 2 of the synthesis prompt)."""
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

    def _build_synthesis_prompt(self, ctx: TurnContext, event: AgentEvent) -> str:
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
        turns_window = _filter_session_turns(self._config.turns_log, saga_session_id)
        if not turns_window:
            asyncio.create_task(
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

    async def run_turn(self, event: AgentEvent) -> TurnRecord:
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

        # 1. SAGA session attach. Synthesis turns already carry the closed
        #    session's id; for everything else we touch (creating if needed).
        if event.trigger == "saga_session_end":
            ctx.saga_session_id = event.extra.get("saga_session_id")
        elif self._sessions is not None:
            session = await self._sessions.touch(event.channel_id)
            ctx.saga_session_id = session.saga_session_id
            self._sessions.increment_turn_count(event.channel_id)

        # 2. Inbound → chat_history (skipped for synthesis turns; their
        #    "input" is the turn-window block, not a real user message).
        await self._record_inbound(event)

        # 3. Flush any out-of-band INDEX changes before reading the index.
        await self._indexes.flush()

        # 4. Pre-message SAGA hook — produces a "Possibly relevant memories"
        #    block to slot into the turn prompt.
        saga_block = await self._pre_message_hook(ctx, event)

        # 4b. Drain any background-subagent notifications that landed since
        #     the last turn for this channel (SPEC §4.4).
        pending_subagents = await self._inbox.drain(event.channel_id)
        subagent_block = (
            render_subagent_updates(pending_subagents) if pending_subagents else None
        )

        # 5. Build prompts.
        if event.trigger == "saga_session_end":
            turn_prompt = self._build_synthesis_prompt(ctx, event)
            recent: list = []
        else:
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
            usage_block = self._assemble_usage_block()
            upcoming_block = self._assemble_upcoming_block()
            self_state_block = self._assemble_self_state_block()
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
            )

        core_blocks = load_core(self._config.home)
        memory_index_body = self._indexes.read_memory_index()
        skill_block = self._assemble_skill_block()
        system_prompt = build_system_prompt(
            core_blocks=core_blocks,
            memory_index_body=memory_index_body,
            operator_alert_channel=self._config.operator_alert_channel,
            skill_block=skill_block,
        )

        await log_event(
            "turn_started",
            turn_id=ctx.turn_id,
            channel_id=ctx.channel_id,
            trigger=ctx.trigger,
            saga_session_id=ctx.saga_session_id,
            core_block_count=len(core_blocks),
            recent_message_count=len(recent),
            saga_atoms_pre_injected=len(ctx.saga_atom_ids),
        )

        # 6. Set TurnContext on the contextvar so SAGA tools auto-credit.
        token = _context.set_current_turn(ctx)
        # Build options once — the same object is passed to query() and
        # then reused for the post-turn plan-quota capture so the
        # ClaudeSDKClient fingerprint matches and the warm client is
        # reused (see _capture_plan_quota_from_client).
        options = self._build_options(system_prompt)
        messages: list = []
        error: str | None = None
        try:
            try:
                async for msg in query(
                    prompt=turn_prompt,
                    options=options,
                    session_id=ctx.turn_id,
                ):
                    messages.append(msg)
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

        events_list, output = extract_turn_events(messages)
        duration_ms = int((time.monotonic() - ctx.started_at) * 1000)

        # 7a. ResultMessage capture (Phase 8 — resume detection + cost).
        #     The SDK emits one ResultMessage per turn at end-of-stream. We
        #     keep the last one in case retries land more than one. None
        #     when query() crashed before emitting any.
        result_msg: ResultMessage | None = None
        for msg in messages:
            if isinstance(msg, ResultMessage):
                result_msg = msg

        # 7a.b. Rate-limit capture from two sources:
        #   - RateLimitEvent: emitted on state transitions (allowed →
        #     allowed_warning → rejected). Sparse but authoritative.
        #   - StreamEvent(message_start): when capture_rate_limits is on
        #     (include_partial_messages=True), every API response's
        #     ``message_start`` carries a ``rate_limits`` block for
        #     Claude.ai subscribers. Captured per-response — we get
        #     current state on every turn, not only on transitions.
        #
        # Both write to the same per-type store; the last update wins.
        # In practice the per-response stream-event path is fresher, so
        # it dominates; the transition path is a backstop when
        # capture_rate_limits is disabled.
        #
        # **Max OAuth gate:** under Claude Max OAuth, response headers
        # do NOT carry per-window utilization% (Anthropic only includes
        # those for direct API key deployments). The OAuth poller
        # (oauth_usage_poller.py) and the in-process Stage 5 capture
        # at 7a.c below are the real-value writers in that mode. The
        # StreamEvent path here would write ``utilization=null`` on
        # every turn, clobbering the poller's real numbers — so we
        # skip it. Direct-API-key deployments are the inverse: the
        # OAuth poller is gated off, and StreamEvent headers carry
        # the real values.
        is_max_oauth = running_on_claude_max()
        for msg in messages:
            if isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                rl_type = getattr(info, "rate_limit_type", None)
                if not rl_type:
                    continue
                try:
                    await self._rate_limits.record(
                        rl_type, snapshot_from_sdk_event(info),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("rate_limits.record failed for %s", rl_type)
                if info.status in ("allowed_warning", "rejected"):
                    await log_event(
                        "rate_limit_warning"
                        if info.status == "allowed_warning"
                        else "rate_limit_rejected",
                        rate_limit_type=rl_type,
                        utilization=info.utilization,
                        resets_at=info.resets_at,
                    )
            elif isinstance(msg, StreamEvent):
                if is_max_oauth:
                    # See gate explanation above. Response headers
                    # under Max OAuth carry no utilization% — capturing
                    # here would null-clobber the OAuth poller.
                    continue
                ev = msg.event or {}
                if ev.get("type") != "message_start":
                    continue
                api_message = ev.get("message") or {}
                rate_limits = api_message.get("rate_limits")
                if not isinstance(rate_limits, dict):
                    continue
                for bucket_type, bucket in rate_limits.items():
                    if not isinstance(bucket, dict):
                        continue
                    try:
                        await self._rate_limits.record(
                            bucket_type,
                            snapshot_from_response_bucket(bucket),
                        )
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "rate_limits.record from response failed for %s",
                            bucket_type,
                        )

        # 7a.c. Plan-window apiUsage capture (Stage 5 of
        #       CLAUDE_SDK_CLIENT_MIGRATION.md). Query the shared
        #       persistent client's ``get_context_usage()`` and persist
        #       each ``apiUsage`` bucket into the same RateLimitStore.
        #       Replaces the throwaway-subprocess cron poller —
        #       fresher cadence (every turn vs every 10 min) with no
        #       extra subprocess cost (the client is already warm
        #       from the query() above). Skipped when the message
        #       loop crashed; the next successful turn picks it up.
        if not error:
            try:
                await self._capture_plan_quota_from_client(options)
            except Exception:  # noqa: BLE001
                log.exception("_capture_plan_quota_from_client raised")

        # 7b. Subagent lifecycle (SPEC §4.4). The SDK yields three messages:
        #
        #   - TaskStartedMessage: task begins; carries description + task_type
        #   - TaskProgressMessage: periodic during long-running tasks; carries
        #     cumulative TaskUsage (total_tokens, tool_uses, duration_ms)
        #   - TaskNotificationMessage: terminal (completed/failed/stopped);
        #     carries final TaskUsage
        #
        # All three log to events.jsonl with task_id + usage fields so
        # subagent_stats.py can aggregate token spend over time. Climber
        # subagents in particular can run for hours and burn most of the
        # parent's plan budget — surfacing this lets the agent see "where
        # the budget is going."
        task_descriptions: dict[str, str] = {}
        for msg in messages:
            if isinstance(msg, TaskStartedMessage):
                task_descriptions[msg.task_id] = msg.description
                await log_event(
                    "subagent_started",
                    turn_id=ctx.turn_id,
                    channel_id=event.channel_id,
                    task_id=msg.task_id,
                    description=msg.description,
                    task_type=getattr(msg, "task_type", None),
                )
        for msg in messages:
            if isinstance(msg, TaskProgressMessage):
                u = msg.usage or {}
                await log_event(
                    "subagent_progress",
                    turn_id=ctx.turn_id,
                    channel_id=event.channel_id,
                    task_id=msg.task_id,
                    description=msg.description,
                    last_tool_name=getattr(msg, "last_tool_name", None),
                    total_tokens=u.get("total_tokens"),
                    tool_uses=u.get("tool_uses"),
                    duration_ms=u.get("duration_ms"),
                )
            elif isinstance(msg, TaskNotificationMessage):
                await self._inbox.push(
                    event.channel_id,
                    SubagentResult(
                        task_id=msg.task_id,
                        status=msg.status,
                        summary=msg.summary,
                        output_file=msg.output_file,
                        description=task_descriptions.get(msg.task_id),
                        usage=msg.usage,
                        received_ts=_utc_now_iso(),
                    ),
                )
                u = msg.usage or {}
                await log_event(
                    "subagent_notification",
                    turn_id=ctx.turn_id,
                    channel_id=event.channel_id,
                    task_id=msg.task_id,
                    status=msg.status,
                    total_tokens=u.get("total_tokens"),
                    tool_uses=u.get("tool_uses"),
                    duration_ms=u.get("duration_ms"),
                )

        # 8. Outbound → chat_history (skip for synthesis turn — there's no
        #    user-facing message; the prompt instructs the agent not to send).
        #    Outbound inherits the inbound's source so the assistant reply
        #    participates in Recent activity rendering on the same allowlist
        #    as the human turn (open-strix-style).
        #
        #    Skip when send_message already wrote the delivered text to the
        #    buffer. The SDK's `output` is final-assistant-text — when the
        #    agent answered via mcp__mimir__send_message it's typically a
        #    short narration ("Sent.") and persisting it would shadow the
        #    real reply in Recent activity.
        # Gate on attempts (not successes) — if the agent tried to send
        # via send_message and the dispatch failed, the failure is in
        # events.jsonl; chat_history shouldn't claim a delivery that
        # didn't happen.
        if (
            output
            and event.trigger != "saga_session_end"
            and ctx.send_message_attempts == 0
        ):
            await self._auto_dispatch_or_record(ctx, event, output)

        # 9. Post-message SAGA hook.
        if not error:
            await self._post_message_hook(ctx, output)

        # 10. End-of-turn INDEX rebuild (debounced).
        self._indexes.mark_dirty("all")
        await self._indexes.flush()

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
        return record
