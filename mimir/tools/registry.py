"""All remaining tool ports — completes migration coverage.

Translates the 12 tools across mimir/{channeltools,scheduletools,
committools,spawn}.py to LangChain @tool. Patterns are identical to
extra_tools.py — same translation rule applies (decorator + type
hints + docstring → schema).

Tools ported (12 total):
  channeltools.py:    send_message, react, fetch_channel_history
  scheduletools.py:   list_schedules, add_schedule, remove_schedule,
                       reload_pollers
  committools.py:     commitment_complete, commitment_snooze,
                       commitment_dismiss, commitment_list
  spawn.py:           spawn_claude_code

Plus combined with extra_tools.py (file_search, mimir_get_turn,
shell_exec) and existing memory_tool.py (memory_query) + store_tool.py
(memory_store), that's **17 tools** ported total — complete coverage
of mimir's existing agent-facing surface.

Each tool's dependencies (channel registry, scheduler, commitments
store, spawn config) are injected via module-state setters parallel
to memory_tool.py's set_memory_client pattern.
"""
from __future__ import annotations

import contextvars
import json
import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

log = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig

from ..bridges._directives import parse_directives, ReactDirective

# Per-task ContextVar for channel_id — isolated across concurrent asyncio
# Tasks so concurrent turns on different channels don't race (S2-1 fix).
_current_channel_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mimir_current_channel_id", default=None
)


def _channel_from_config_or_state(
    channel_id: str | None, config: RunnableConfig | None
) -> str:
    """Resolve the effective channel_id for a tool call.

    Precedence (highest first):
      1. Explicit ``channel_id`` argument from the model
      2. LangGraph ``configurable["channel_id"]`` (set by run_turn)
      3. Module-global ``_STATE["current_channel_id"]`` (legacy
         dispatcher-set; back-compat path for callers that still
         use ``set_current_channel_id``)

    The LangGraph path is the new canonical route — ``_STATE`` is
    process-global and races across concurrent dispatcher turns.
    Returns ``""`` if no source supplies a channel.
    """
    cid = (channel_id or "").strip()
    if cid:
        return cid
    if config is not None:
        configurable = config.get("configurable") or {}
        from_config = (configurable.get("channel_id") or "").strip()
        if from_config:
            return from_config
    return (_current_channel_id_var.get() or "").strip()

from langchain_core.tools import InjectedToolArg, tool


# ────────────────────────────────────────────────────────────────────
# Module-state dependency injection (parallel to memory_tool.py)
# ────────────────────────────────────────────────────────────────────

_STATE: dict[str, Any] = {
    "channel_registry": None,
    "dispatcher": None,
    "scheduler": None,
    "commitments_store": None,
    "spawn_config": None,
    "current_channel_id": None,  # set per-turn by the dispatcher
}


def set_channel_registry(registry: Any) -> None:
    _STATE["channel_registry"] = registry


def set_dispatcher(dispatcher: Any) -> None:
    _STATE["dispatcher"] = dispatcher


def set_scheduler(scheduler: Any) -> None:
    _STATE["scheduler"] = scheduler


def set_commitments_store(store: Any) -> None:
    _STATE["commitments_store"] = store


def set_spawn_config(config: Any) -> None:
    _STATE["spawn_config"] = config


def set_current_channel_id(channel_id: str | None) -> contextvars.Token:
    """Set the per-task channel_id. Returns a Token; call
    reset_current_channel_id(token) in a finally block to restore.

    ContextVar is isolated per asyncio.Task — concurrent turns on
    different channels don't race. Replaces the old process-global
    _STATE["current_channel_id"] write (S2-1 fix).
    """
    return _current_channel_id_var.set(channel_id)


def reset_current_channel_id(token: contextvars.Token) -> None:
    """Restore the prior channel_id using the Token from set_current_channel_id."""
    _current_channel_id_var.reset(token)


# ────────────────────────────────────────────────────────────────────
# Channel tools (mimir/channeltools.py)
# ────────────────────────────────────────────────────────────────────

@tool
async def send_message(
    text: str,
    channel_id: Optional[str] = None,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Emit a message to a channel.

    If channel_id is omitted, uses the current turn's channel. Subject
    to a per-turn loop-detection circuit breaker — repeated near-
    duplicates first warn, then refuse.

    Args:
        text: The message body to send.
        channel_id: Target channel ID. Defaults to current turn's.
    """
    channels = _STATE["channel_registry"]
    if channels is None:
        return "send_message failed: no channel registry configured"
    if not text or not text.strip():
        return "send_message failed: text is required"
    cid = _channel_from_config_or_state(channel_id, config)
    if not cid:
        return "send_message failed: no channel_id and no current channel"

    # Loop-detection circuit breaker (SPEC §7.2.4). The per-turn
    # LoopDetector lives on the active TurnContext; agent.run_turn
    # attaches it at the start of every turn. Pre-181-J this hook
    # was missing — repeated near-duplicate sends would ship
    # indefinitely. Now: HARD_STOP refuses the send with a recovery
    # hint; SOFT_WARN allows but logs a one-time per-turn warning
    # event so operator dashboards can flag the near-loop.
    from .._context import get_current_turn
    from ..event_logger import log_event as _log_event
    from ..loop_detector import BreakerVerdict

    detector = None
    ctx = get_current_turn()
    if ctx is not None:
        detector = getattr(ctx, "loop_detector", None)
    if detector is not None:
        decision = detector.check(text)
        if decision.verdict == BreakerVerdict.HARD_STOP:
            await _log_event(
                "send_message_loop_hard_stop",
                channel_id=cid,
                streak=decision.streak,
                similarity=round(decision.similarity, 4),
            )
            return (
                "send_message hard stop: repeated near-duplicate loop. "
                "This send is refused. Reflect on what's wrong with the "
                "approach before sending again — try a completely "
                "different tactic or finish the turn."
            )
        if decision.verdict == BreakerVerdict.SOFT_WARN:
            if detector.mark_warning_emitted():
                await _log_event(
                    "send_message_loop_warning",
                    channel_id=cid,
                    streak=decision.streak,
                    similarity=round(decision.similarity, 4),
                )

    bridge = channels.find(cid)
    if bridge is None:
        return f"send_message failed: no bridge for channel {cid!r}"

    # Strip <actions>...</actions> directive blocks from the outbound
    # text and dispatch parsed directives (react, send-file) after send.
    parsed = parse_directives(text)
    clean_text = parsed.clean_text

    result = None
    if clean_text:
        try:
            result = await bridge.send(cid, clean_text)
        except Exception as exc:
            return f"send_message failed: {exc}"

        # Append outbound to chat-history buffer so the agent's next
        # turn sees its own reply in Recent activity. Dropped in PR
        # #181's deepagents migration; restoring here closes the
        # regression for the send_message-tool path (the most common
        # outbound path in production). No-op when no buffer is
        # registered (test paths that bypass ``server.serve``).
        #
        # ``source`` is left ``None`` here — unlike the agent-fallback
        # path which threads ``ctx.channel_source``, the tool runs
        # detached from the active TurnContext. Render code treats
        # missing source the same as a non-allowlisted source for
        # the cross-author cross-pull check; in practice every
        # cross-channel render path filters on the inbound channel's
        # source, so an empty source on outbound just means it stays
        # scoped to its own channel (which is the right default).
        from ..history import get_global_buffer
        _buf = get_global_buffer()
        if _buf is not None and result is not None:
            try:
                msg = _buf.make_message(
                    channel_id=cid,
                    kind="assistant_message",
                    content=clean_text,
                    msg_id=getattr(result, "message_id", None),
                    source=None,
                )
                await _buf.append(msg)
            except Exception:  # noqa: BLE001
                # Best-effort — don't fail the tool call if the
                # buffer hiccups. Log a warning rather than swallowing
                # silently so disk-full / permission-denied issues
                # are visible in events.jsonl downstream.
                log.warning(
                    "send_message: chat_history append failed", exc_info=True,
                )

    for _directive in parsed.directives:
        if isinstance(_directive, ReactDirective):
            _target = _directive.message_id or (
                result.message_id if result else None
            )
            try:
                await bridge.react(cid, _target, _directive.emoji)
            except Exception:
                pass  # non-fatal; directive failures don't abort the send
        # SendFileDirective: not yet implemented via this path

    return f"send_message ok: channel={cid} message_id={result}"


@tool
async def react(
    emoji: str,
    message_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """React to a message with an emoji.

    Defaults to the most recent assistant message on the current
    channel. Bridges that don't support native reactions (e.g. Bluesky)
    log a no-op.

    Args:
        emoji: Reaction emoji (e.g. "👍").
        message_id: Specific message to react to. Defaults to most
            recent on the channel.
        channel_id: Channel scope. Defaults to current turn's.
    """
    channels = _STATE["channel_registry"]
    if channels is None:
        return "react failed: no channel registry configured"
    cid = _channel_from_config_or_state(channel_id, config)
    if not cid:
        return "react failed: no channel_id and no current channel"
    bridge = channels.find(cid)
    if bridge is None:
        return f"react failed: no bridge for channel {cid!r}"
    try:
        await bridge.react(cid, message_id, emoji)
    except Exception as exc:
        return f"react failed: {exc}"
    return f"react ok: channel={cid} emoji={emoji}"


@tool
async def fetch_channel_history(
    channel_id: Optional[str] = None,
    limit: int = 20,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Fetch recent messages from a channel.

    Args:
        channel_id: Channel to read. Defaults to current turn's.
        limit: Max messages to return (1-100, default 20).
    """
    channels = _STATE["channel_registry"]
    if channels is None:
        return "fetch_channel_history failed: no channel registry"
    cid = _channel_from_config_or_state(channel_id, config)
    if not cid:
        return "fetch_channel_history failed: no channel_id and no current"
    try:
        k = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        k = 20
    bridge = channels.find(cid)
    if bridge is None or not hasattr(bridge, "fetch_history"):
        return f"fetch_channel_history failed: bridge {cid!r} doesn't support history"
    try:
        history = await bridge.fetch_history(cid, limit=k)
    except Exception as exc:
        return f"fetch_channel_history failed: {exc}"
    return json.dumps(history, indent=2, ensure_ascii=False, default=str)


# ────────────────────────────────────────────────────────────────────
# Scheduler tools (mimir/scheduletools.py)
# ────────────────────────────────────────────────────────────────────

@tool
async def list_schedules() -> str:
    """List all scheduled jobs (heartbeat, reflect, custom ticks).

    Returns each job's configuration: name, cron, channel, and the
    prompt-source (one of ``prompt`` / ``prompt_file`` / ``callable``).
    Runtime fields (``last_run`` / ``next_fire``) live on apscheduler
    ``Job`` objects rather than the YAML-config ``SchedulerJob``;
    surfacing them here would require joining the two views, which
    we don't do today.
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "list_schedules failed: no scheduler configured"
    try:
        jobs = await scheduler.list_jobs()
    except Exception as exc:
        return f"list_schedules failed: {exc}"
    if not jobs:
        return "(no scheduled jobs)"
    out: list[dict[str, Any]] = []
    for j in jobs:
        entry: dict[str, Any] = {
            "name": j.name,
            "cron": j.cron,
            "channel_id": j.channel_id,
        }
        # Surface whichever prompt-source field is populated (mutually
        # exclusive per SchedulerJob's contract). Inline prompts are
        # truncated to keep this tool's output skim-friendly.
        if getattr(j, "prompt_file", None):
            entry["prompt_file"] = j.prompt_file
        elif getattr(j, "callable_name", None):
            entry["callable"] = j.callable_name
        elif j.prompt:
            entry["prompt"] = (
                j.prompt if len(j.prompt) <= 200 else j.prompt[:200] + "..."
            )
        # ``time_of_day`` is an alternative to ``cron`` — surface it
        # when the operator picked that style instead.
        time_of_day = getattr(j, "time_of_day", None)
        if time_of_day:
            entry["time_of_day"] = time_of_day
        out.append(entry)
    return json.dumps(out, indent=2, ensure_ascii=False, default=str)


@tool
async def add_schedule(
    name: str,
    cron: str,
    prompt: str,
    channel_id: Optional[str] = None,
) -> str:
    """Add a new scheduled tick.

    Args:
        name: Unique job identifier.
        cron: 5-field cron expression (e.g. ``"0 9 * * *"`` for 9am daily).
        prompt: Inline prompt to fire on the cron tick.
        channel_id: Channel to dispatch the tick on. Defaults to
            ``scheduler:<name>`` synthetic.
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "add_schedule failed: no scheduler configured"
    try:
        job = await scheduler.add_job(
            name=name, cron=cron, prompt=prompt, channel_id=channel_id,
        )
    except Exception as exc:
        return f"add_schedule failed: {exc}"
    return f"add_schedule ok: name={job.name} cron={job.cron}"


@tool
async def remove_schedule(name: str) -> str:
    """Remove a scheduled tick by name."""
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "remove_schedule failed: no scheduler configured"
    try:
        removed = await scheduler.remove_job(name)
    except Exception as exc:
        return f"remove_schedule failed: {exc}"
    if not removed:
        return f"remove_schedule: no job named {name!r}"
    return f"remove_schedule ok: name={name}"


@tool
async def reload_pollers() -> str:
    """Re-read pollers.yaml and re-register all pollers.

    Use after editing the file to apply changes without restarting
    the agent. Returns counts of registered / replaced / removed.
    """
    scheduler = _STATE["scheduler"]
    if scheduler is None:
        return "reload_pollers failed: no scheduler configured"
    try:
        stats = await scheduler.reload_pollers()
    except Exception as exc:
        return f"reload_pollers failed: {exc}"
    return (
        f"reload_pollers ok: total={stats.get('total', 0)} "
        f"(fresh={stats.get('registered', 0)})"
    )


# ────────────────────────────────────────────────────────────────────
# Commitments tools (mimir/committools.py)
# ────────────────────────────────────────────────────────────────────

@tool
async def commitment_complete(commitment_id: str, message_id: Optional[str] = None) -> str:
    """Mark a tracked commitment as completed.

    Args:
        commitment_id: The commitment to close out.
        message_id: Optional message ID that triggered the completion (for audit).
    """
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_complete failed: no commitments store"
    try:
        result = await store.complete(commitment_id, message_id=message_id)
    except Exception as exc:
        return f"commitment_complete failed: {exc}"
    return f"commitment_complete ok: id={commitment_id} result={result}"


@tool
async def commitment_snooze(
    commitment_id: str,
    until_iso: str,
    reason: Optional[str] = None,
) -> str:
    """Snooze a commitment until a future ISO datetime.

    Args:
        commitment_id: The commitment to snooze.
        until_iso: ISO-8601 datetime when the commitment reactivates (e.g. "2026-05-20T10:00:00Z").
        reason: Optional snooze reason recorded in the log.
    """
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_snooze failed: no commitments store"
    try:
        dt = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
        until_unix = dt.timestamp()
        result = await store.snooze(commitment_id, until_unix=until_unix, reason=reason)
    except Exception as exc:
        return f"commitment_snooze failed: {exc}"
    return f"commitment_snooze ok: id={commitment_id} until={until_iso}"


@tool
async def commitment_dismiss(commitment_id: str, reason: Optional[str] = None) -> str:
    """Dismiss a commitment without completing it.

    Args:
        commitment_id: The commitment to dismiss.
        reason: Optional dismissal reason recorded in the log.
    """
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_dismiss failed: no commitments store"
    try:
        result = await store.dismiss(commitment_id, reason=reason)
    except Exception as exc:
        return f"commitment_dismiss failed: {exc}"
    return f"commitment_dismiss ok: id={commitment_id}"


from ..commitments.models import CommitmentStatus as _CommitmentStatus

# Non-terminal statuses worth surfacing to ``commitment_list``. Sourced
# from the ``CommitmentStatus`` enum so a future rename can't silently
# drift this set out of sync with the real state machine.
_ACTIVE_STATUSES = frozenset({
    _CommitmentStatus.PENDING.value,
    _CommitmentStatus.DELIVERED.value,
    _CommitmentStatus.SNOOZED.value,
})


@tool
async def commitment_list(due_within_days: int = 7) -> str:
    """List active (non-terminal) commitments, optionally filtered by due window.

    Args:
        due_within_days: Only include commitments whose due window ends within
            this many days from now. Pass 0 to list all active commitments
            regardless of due date (default 7 days).
    """
    import time as _time
    store = _STATE["commitments_store"]
    if store is None:
        return "commitment_list failed: no commitments store"
    try:
        # store.list() is synchronous
        all_items = store.list()
    except Exception as exc:
        return f"commitment_list failed: {exc}"
    now = _time.time()
    cutoff = now + due_within_days * 86400 if due_within_days > 0 else None
    items = [
        c for c in all_items
        if c.status in _ACTIVE_STATUSES
        and (
            cutoff is None
            or c.due_window_end_unix is None  # unbound — always include
            or c.due_window_end_unix <= cutoff
        )
    ]
    if not items:
        label = "all active" if due_within_days == 0 else f"due within {due_within_days} days"
        return f"(no active commitments — {label})"
    return json.dumps(
        [
            {
                "id": c.id,
                "text": c.text,
                "status": c.status,
                "channel_id": c.channel_id,
                "due_window_hint": c.due_window_hint,
                "due_window_end_unix": c.due_window_end_unix,
            }
            for c in items
        ],
        indent=2, ensure_ascii=False, default=str,
    )


# ────────────────────────────────────────────────────────────────────
# Spawn (mimir/spawn.py)
# ────────────────────────────────────────────────────────────────────

def _run_claude_subprocess(argv: list[str], cwd: str | None, timeout_s: int) -> tuple[int, str, str]:
    """Sync subprocess.run wrapper — called from a thread via to_thread.

    Keeping the blocking I/O in a helper that's invoked through
    ``asyncio.to_thread`` keeps spawn_claude_code from freezing the
    dispatcher's event loop for the duration of the subprocess (up to
    ``timeout_s=1800`` by default). Returns (returncode, stdout, stderr)
    or raises subprocess.TimeoutExpired / FileNotFoundError unchanged.
    """
    proc = subprocess.run(  # noqa: S603 — argv is constructed, not shell
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout, proc.stderr


@tool
async def spawn_claude_code(
    prompt: str,
    cwd: Optional[str] = None,
    timeout_s: int = 1800,
    name: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Spawn a Claude Code subprocess to execute a complex task.

    Use for work that needs deep context isolation, long-running
    multi-step plans, or independent execution from the parent agent.
    The subprocess runs ``claude -p <prompt>`` and captures its
    output, final cost, and modelUsage metrics.

    Pre-fix this was a sync function that called ``subprocess.run``
    directly. deepagents awaited the sync callable, freezing the
    dispatcher's event loop for up to ``timeout_s=1800`` seconds —
    every other channel's worker blocked until the spawn finished.
    Now async, with the blocking subprocess call wrapped in
    ``asyncio.to_thread``.

    **Model selection heuristic** (chainlink #158): pick based on
    cognitive *depth* required, not output *shape* (destination path).
    Passing ``model`` explicitly avoids the output-path-as-proxy
    mis-tier (e.g. a wiki-page destination triggering a lighter model
    for analytical work that needs deeper reasoning).

    - ``model="opus"`` — analytical / evaluative work: VSM analysis,
      gap inventories, skeptical code review, design decisions,
      adversarial synthesis. Use when the task is "think critically"
      even if the output lands at a doc/wiki path.
    - ``model="sonnet"`` — default for most spawn work: implementation
      tasks, benchmark runs, doc writing, spec drafts, mechanical
      multi-step work where the path is well-defined.
    - ``model="haiku"`` — high-throughput mechanical tasks that are
      genuinely simple (e.g. format conversion, short summaries).
      Rarely the right choice for spawn work; prefer sonnet as the
      safe default.
    - Omit ``model`` to let the claude CLI use its configured default
      (currently sonnet-tier).

    Args:
        prompt: The task to hand to the spawned Claude Code instance.
        cwd: Working directory for the subprocess. Defaults to home.
        timeout_s: Subprocess timeout (default 30 min).
        name: Optional label recorded in the spawn log.
        model: Claude model alias or full name (e.g. ``"opus"``,
            ``"sonnet"``, ``"claude-opus-4-7"``). Passed as
            ``--model`` to the claude CLI. Omit to use the CLI
            default. Use ``"opus"`` for analytical/evaluative work
            even when the output destination is doc-shaped.
    """
    cfg = _STATE["spawn_config"]
    if cfg is None:
        return "spawn_claude_code failed: no spawn config"
    if not prompt or not prompt.strip():
        return "spawn_claude_code failed: prompt is required"
    cwd_path = Path(cwd).expanduser() if cwd else cfg.get("default_cwd")
    argv = ["claude", "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    try:
        returncode, stdout, stderr = await asyncio.to_thread(
            _run_claude_subprocess,
            argv,
            str(cwd_path) if cwd_path else None,
            timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"spawn_claude_code timed out after {timeout_s}s"
    except FileNotFoundError:
        return "spawn_claude_code failed: 'claude' CLI not on PATH"
    if returncode != 0:
        return (
            f"spawn_claude_code failed: exit={returncode} "
            f"stderr={stderr[:500]}"
        )
    try:
        result = json.loads(stdout)
        return json.dumps(
            {"result": result.get("result", "")[:2000],
             "cost_usd": result.get("total_cost_usd"),
             "num_turns": result.get("num_turns"),
             "name": name},
            indent=2,
        )
    except json.JSONDecodeError:
        return f"spawn_claude_code: raw output: {stdout[:2000]}"


# ────────────────────────────────────────────────────────────────────
# Convenience: assemble all tools for the deepagent factory
# ────────────────────────────────────────────────────────────────────

def all_mimir_tools() -> list:
    """Return the full mimir tool surface for create_deep_agent.

    Combines tools from memory_tool, store_tool, extra_tools, and
    this module. Production cutover would wire the dep-injection
    setters in mimir/server.py:build_app once and let the agent
    discover them all at construction time.

    Web tools (Tavily ``web_search`` + ``fetch_url``) are appended
    only when the active LLM provider is not ``claude_code`` — Claude
    Code subprocesses ship native WebSearch/WebFetch and stacking
    Tavily on top would duplicate the surface. See
    ``mimir.tools.web.web_tools_enabled`` for the gating predicate.
    """
    from .memory import memory_query
    from .store import memory_store
    from .extra import file_search, get_turn, mimir_get_turn, rebuild_index, shell_exec
    from .web import web_tools_enabled
    from .shell_async import bash_async, bash_job_output, bash_jobs_list
    from .saga_ops import (
        saga_end_session,
        saga_feedback,
        saga_forget,
        saga_mark_contributions,
    )
    tools = [
        # Memory (read + write)
        memory_query, memory_store,
        # SAGA ops (outcome marker, manual credit, session boundary, forget)
        saga_feedback, saga_mark_contributions, saga_end_session, saga_forget,
        # Indexer (file search + mid-turn index rebuild)
        file_search,
        rebuild_index,
        # Turn-history lookup (mimir_get_turn is canonical; get_turn
        # is a back-compat alias for skill prompts that reference the
        # pre-rename name)
        mimir_get_turn, get_turn,
        # Shell exec (allowlist-scoped, sync — fine for sub-second cmds)
        shell_exec,
        # Async shell — long-running jobs that wake the agent via
        # ``shell_job_complete`` on exit. The companion list/output
        # tools query the per-process ShellJobRegistry.
        bash_async, bash_jobs_list, bash_job_output,
        # Channel ops
        send_message, react, fetch_channel_history,
        # Scheduler
        list_schedules, add_schedule, remove_schedule, reload_pollers,
        # Commitments
        commitment_complete, commitment_snooze,
        commitment_dismiss, commitment_list,
        # Spawn
        spawn_claude_code,
    ]
    web_search_on, fetch_url_on = web_tools_enabled()
    if web_search_on or fetch_url_on:
        from .web import fetch_url, web_search
        if web_search_on:
            tools.append(web_search)
        if fetch_url_on:
            tools.append(fetch_url)
    # MCP-bridged tools (populated by server.py:_on_startup after the
    # MCP servers come up; empty when MCP is unconfigured).
    from .mcp import get_mcp_tools
    tools.extend(get_mcp_tools())
    # Per-turn tool-call budget gating moved to ``BudgetGateMiddleware``
    # (mimir/tools/budget_gate.py) and wired via ``create_deep_agent
    # (middleware=...)`` in agent.py. The middleware intercepts every
    # tool call — registered AND deepagents built-ins — so the
    # previous per-tool wrapping pattern (apply_budget_gate) was
    # removed to avoid double-counting.
    return tools
