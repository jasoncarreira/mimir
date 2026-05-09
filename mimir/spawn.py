"""``spawn_claude_code`` MCP tool — wrap ``claude -p`` in bash_async.

chainlink #60 (parent #50). Lets the model hand off long-running
mechanical work to a fresh Claude Code session that runs out-of-band.
The spawn shares mimir's OAuth credentials (HOME=/mimir-home) and burns
the same plan-window quota — accounting flows back into turns.jsonl as
``kind=claude_code_spawn`` records so ``aggregate_usage`` sees the
spend natively.

Three failure modes (per chainlink-50 spec §C):

- **spawn-failure**: ``registry.spawn`` raises before a job_id is
  returned (cwd missing, etc.). Tool returns an is_error block; no
  ``shell_job_complete`` ever fires for this attempt.
- **auth-failure**: process exited but the spawn JSON has
  ``is_error: true`` plus a 4xx ``api_error_status`` — token revoked,
  refresh failed, server-side quota exhausted.
  ``claude_code_spawn_auth_failed`` event.
- **work-failure**: spawn exited but the agent loop ended badly:
  ``terminal_reason`` ∈ {``max-turns``, ``max-budget-usd``,
  ``errored``} or the JSON itself was unparseable.
  ``claude_code_spawn_work_failed`` event.

Clean completions emit ``claude_code_spawn_completed`` (positive,
first-occurrence-only — see ``feedback._FIRST_OCCURRENCE_ONLY_KINDS``).

Per-profile budget defaults live here (``PROFILE_DEFAULTS``) because
``--max-budget-usd`` is a CLI-only flag, not an agent-frontmatter field.
``model`` and ``maxTurns`` flow from the profile's frontmatter via
``--agent <name>`` natively (verified in chainlink #59).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import SdkMcpTool, tool

from ._context import resolve_active_ctx
from ._tool_helpers import _content_block, _need, _safe
from .event_logger import log_event
from .models import TurnRecord, make_turn_id
from .shell_jobs import ShellJob, ShellJobRegistry
from .turn_logger import TurnLogger


log = logging.getLogger(__name__)


def spawn_tool_names() -> list[str]:
    return ["mcp__mimir__spawn_claude_code"]


# Per-profile budget defaults (operator decisions, chainlink #50 §5).
# ``--max-budget-usd`` is the only spawn-time field that doesn't live in
# the agent-frontmatter — keep it close to the spawn shape so adding a
# new profile is a one-line change.
PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "code-implementer": {"max_budget_usd": 25.0},
    "bench-runner":     {"max_budget_usd": 10.0},
    "doc-writer":       {"max_budget_usd": 5.0},
}
DEFAULT_AGENT = "code-implementer"
DEFAULT_TIMEOUT_SEC = 3600
GLOBAL_BUDGET_FALLBACK_USD = 10.0

# Brief is passed as a single argv element to ``claude -p``. Linux's
# per-element ``MAX_ARG_STRLEN`` is 128 KB on most kernels (32 *
# PAGE_SIZE); a brief above that fails at ``execve`` with ``E2BIG`` and
# a not-very-actionable error from Popen. Cap at 64 KB to leave headroom
# for kernels with smaller PAGE_SIZE and to surface the failure with a
# clear, actionable message before launch. If a future caller needs a
# bigger brief, switch the spawn to read from a file via stdin or a
# brief-path flag.
MAX_BRIEF_BYTES = 64 * 1024


def _parse_spawn_result_json(stdout_text: str) -> Optional[dict[str, Any]]:
    """Recover the final JSON object from a ``claude -p --output-format json``
    spawn's stdout. The bundled CLI may print noise lines after the
    JSON ("Shell cwd was reset to ..."), so iterate from the tail and
    return the first parseable JSON object. Returns ``None`` when
    nothing parses (e.g. the spawn died before emitting JSON).
    """
    if not stdout_text:
        return None
    for line in reversed(stdout_text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            continue
    # Single-blob fallback (no newlines, just one big JSON object).
    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        return None


def _classify_terminal(parsed: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a parsed spawn-result JSON onto one of the three completion
    event types and the per-event payload fields.

    Decision tree (chainlink-50 spec §C "Failure-shape distinction"):
      ``is_error`` + 4xx ``api_error_status``  → auth_failed
      ``is_error`` OR ``terminal_reason`` in   → work_failed
        {max-turns, max-budget-usd, errored}
      otherwise                                 → completed
    """
    is_error = bool(parsed.get("is_error"))
    api_error_status = parsed.get("api_error_status")
    terminal_reason = parsed.get("terminal_reason") or ""
    cost = parsed.get("total_cost_usd")
    duration_ms = parsed.get("duration_ms")
    model_usage = parsed.get("modelUsage") or {}

    common: dict[str, Any] = {
        "terminal_reason": terminal_reason,
        "cost_usd": cost,
        "duration_ms": duration_ms,
        "model_usage": model_usage,
    }

    if (
        is_error
        and isinstance(api_error_status, int)
        and 400 <= api_error_status < 500
    ):
        return (
            "claude_code_spawn_auth_failed",
            {**common, "api_error_status": api_error_status},
        )
    if is_error or terminal_reason in (
        "max-turns",
        "max-budget-usd",
        "errored",
    ):
        return ("claude_code_spawn_work_failed", common)
    return ("claude_code_spawn_completed", common)


def _model_usage_to_record_usage(
    model_usage: dict[str, Any],
) -> Optional[dict[str, int]]:
    """Translate spawn's per-model ``modelUsage`` → flat ``usage`` dict.

    Spawn shape::
        {"claude-sonnet-4-5": {"inputTokens": N, "outputTokens": N,
                               "cacheCreationInputTokens": N,
                               "cacheReadInputTokens": N, "costUSD": N},
         ...}

    ``aggregate_usage`` sums per-window from each turn record's flat
    ``usage`` dict — collapse across models so the spawn appears as a
    single record with the total token spend.

    Returns ``None`` when the input is empty/missing so the field stays
    absent in the synthetic record (matches the convention for turns
    that produced no usage).
    """
    if not model_usage:
        return None
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    saw_any = False
    for entry in model_usage.values():
        if not isinstance(entry, dict):
            continue
        saw_any = True
        for src_key, dst_key in (
            ("inputTokens", "input_tokens"),
            ("outputTokens", "output_tokens"),
            ("cacheCreationInputTokens", "cache_creation_input_tokens"),
            ("cacheReadInputTokens", "cache_read_input_tokens"),
        ):
            v = entry.get(src_key)
            if isinstance(v, (int, float)):
                totals[dst_key] += int(v)
    return totals if saw_any else None


def _build_spawn_record(
    *,
    job_id: str,
    channel_id: Optional[str],
    agent_name: str,
    parsed: Optional[dict[str, Any]],
    spawn_started_at: float,
    spawn_finished_at: float,
) -> TurnRecord:
    """Synthetic ``kind="claude_code_spawn"`` TurnRecord for turns.jsonl.

    Most TurnRecord fields don't apply to a spawn (saga_atom_ids,
    events, output text, num_turns) — they default to empty/None.
    The fields ``aggregate_usage`` reads (``ts``, ``total_cost_usd``,
    ``usage``) are populated from the spawn's final JSON.
    """
    cost: Optional[float] = None
    usage: Optional[dict[str, int]] = None
    duration_ms = int((spawn_finished_at - spawn_started_at) * 1000)
    result_subtype: Optional[str] = None
    stop_reason: Optional[str] = None
    is_error: Optional[bool] = None
    if parsed is not None:
        c = parsed.get("total_cost_usd")
        if isinstance(c, (int, float)):
            cost = float(c)
        usage = _model_usage_to_record_usage(parsed.get("modelUsage") or {})
        sub = parsed.get("subtype")
        if isinstance(sub, str):
            result_subtype = sub
        sr = parsed.get("stop_reason") or parsed.get("terminal_reason")
        if isinstance(sr, str):
            stop_reason = sr
        ie = parsed.get("is_error")
        if isinstance(ie, bool):
            is_error = ie
        d = parsed.get("duration_ms")
        if isinstance(d, (int, float)):
            duration_ms = int(d)

    ts = datetime.now(timezone.utc).isoformat()
    return TurnRecord(
        ts=ts,
        turn_id=make_turn_id(),
        session_id=channel_id or "",
        saga_session_id=None,
        trigger="claude_code_spawn",
        channel_id=channel_id,
        input=f"spawn:{agent_name}:{job_id}",
        output="",
        duration_ms=duration_ms,
        result_subtype=result_subtype,
        result_is_error=is_error,
        stop_reason=stop_reason,
        total_cost_usd=cost,
        usage=usage,
        kind="claude_code_spawn",
    )


def build_spawn_tool(
    registry: ShellJobRegistry,
    turn_logger: Optional[TurnLogger],
    mimir_home: Path,
    spawns_dir: Path,
    schedule_from_thread: Callable[[Awaitable[Any]], None],
    chain_on_complete: Optional[Callable[[ShellJob], None]] = None,
) -> list[SdkMcpTool]:
    """Build the ``spawn_claude_code`` tool, closures binding the
    cross-cutting collaborators it needs at completion-time.

    - ``registry`` — shared ``ShellJobRegistry``; the spawn appears in
      ``bash_jobs_list`` / ``bash_job_output`` like any other async job.
    - ``turn_logger`` — append a synthetic ``kind=claude_code_spawn``
      TurnRecord on completion. ``None`` skips that step (tests).
    - ``mimir_home`` — used as ``HOME`` override so the bundled CLI
      reads ``<mimir_home>/.claude/.credentials.json``.
    - ``spawns_dir`` — brief files written here as ``<job_id>.md``
      (durable, no cleanup per spec §4).
    - ``schedule_from_thread`` — bridge from the registry's waiter
      thread back to the agent's asyncio loop. Receives a coroutine
      and arranges to await it; no-ops if the loop hasn't been
      captured yet.
    - ``chain_on_complete`` — invoked AFTER spawn-specific accounting
      so the regular ``shell_job_complete`` AgentEvent still wakes
      the spawning channel. ``None`` disables the chain (tests).
    """
    spawns_dir.mkdir(parents=True, exist_ok=True)

    @tool(
        "spawn_claude_code",
        "Spawn a fresh Claude Code session via ``claude -p`` with a "
        "specific agent profile (e.g. ``code-implementer``, "
        "``bench-runner``, ``doc-writer``). Use for long-running "
        "mechanical work that exceeds the ~120 tool-call ceiling of an "
        "Agent subagent — large refactors, full bench runs, multi-file "
        "conversions. The spawn gets its own context window; you stay "
        "free to handle other channels. Completion fires a "
        "``claude_code_spawn_completed`` event (or ``_auth_failed`` / "
        "``_work_failed``) plus a regular ``shell_job_complete`` "
        "wake-up on this channel. The spawn's cost lands in turns.jsonl "
        "as a ``kind=claude_code_spawn`` record so the homeostat sees "
        "its plan-window spend. Pass ``session_id`` (your "
        "saga_session_id) so the wake-up routes here.",
        {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": (
                        "Full task brief — goal, repo state, acceptance "
                        "criteria, test invocations, PR conventions. "
                        "The spawn sees this verbatim as user-1."
                    ),
                },
                "working_dir": {
                    "type": "string",
                    "description": (
                        "Subprocess cwd (typically the repo root, e.g. "
                        "/workspace/mimir)."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Agent profile name. Defaults to "
                        "'code-implementer'. Profile defines model, "
                        "maxTurns, tools, system prompt."
                    ),
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Git branch the spawn should work on. Recorded "
                        "in the brief; the spawn's profile handles the "
                        "actual checkout."
                    ),
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": (
                        "Outer wall-clock cap (default 3600). The spawn "
                        "is wrapped in ``timeout N``."
                    ),
                },
                "max_budget_usd": {
                    "type": "number",
                    "description": (
                        "Hard $ cap. Defaults per profile "
                        "(code-implementer $25, bench-runner $10, "
                        "doc-writer $5)."
                    ),
                },
                "max_turns": {
                    "type": "integer",
                    "description": (
                        "Loop cap. Defaults to the profile's frontmatter "
                        "maxTurns; only set this to override."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model override. Defaults to the profile's "
                        "frontmatter model; only set this to override."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Your current saga_session_id, so the wake-up "
                        "event routes back here."
                    ),
                },
            },
            "required": ["brief", "working_dir"],
        },
    )
    @_safe("spawn_claude_code")
    async def spawn_claude_code(args: dict[str, Any]) -> dict[str, Any]:
        brief = _need(args, "brief")
        working_dir = _need(args, "working_dir")
        agent_name = args.get("agent") or DEFAULT_AGENT
        branch = args.get("branch")
        # Reject huge briefs before spawn so the caller sees an
        # actionable error, not an opaque ``[Errno 7] argument list
        # too long`` from execve. See MAX_BRIEF_BYTES note above.
        brief_bytes = len(brief.encode("utf-8"))
        if brief_bytes > MAX_BRIEF_BYTES:
            await log_event(
                "claude_code_spawn_spawn_failed",
                agent=agent_name,
                working_dir=working_dir,
                error=f"brief too large: {brief_bytes} bytes > {MAX_BRIEF_BYTES}",
                reason="brief_too_large",
                brief_bytes=brief_bytes,
            )
            return _content_block(
                f"spawn_claude_code failed to launch: brief is "
                f"{brief_bytes} bytes (max {MAX_BRIEF_BYTES}). "
                f"Briefs are passed as a single argv element to "
                f"``claude -p`` and would exceed Linux's per-element "
                f"limit. Trim the brief, or split into a setup brief "
                f"plus follow-up work.",
                is_error=True,
            )
        timeout_sec = int(args.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
        defaults = PROFILE_DEFAULTS.get(agent_name, {})
        budget_arg = args.get("max_budget_usd")
        if budget_arg is None:
            max_budget_usd = float(
                defaults.get("max_budget_usd", GLOBAL_BUDGET_FALLBACK_USD)
            )
        else:
            max_budget_usd = float(budget_arg)
        max_turns_arg = args.get("max_turns")
        model_arg = args.get("model")

        ctx, _resolution = resolve_active_ctx(args)
        channel_id = ctx.channel_id if ctx is not None else None

        # Brief-file naming: use a fresh uuid (not the registry's
        # job_id, which we don't know until after spawn). The two
        # diverging is fine — the file is for human navigation, not
        # registry lookup. Both names land in events/turns logs.
        brief_uuid = uuid.uuid4().hex[:10]
        brief_path = spawns_dir / f"j_{brief_uuid}.md"
        brief_text = brief
        if branch:
            brief_text = f"<!-- branch: {branch} -->\n\n{brief}"
        brief_path.write_text(brief_text, encoding="utf-8")

        argv = [
            "timeout", str(timeout_sec),
            "claude", "-p", brief_text,
            "--agent", agent_name,
            "--output-format", "json",
            "--add-dir", working_dir,
            "--setting-sources", "user,project",
            "--max-budget-usd", f"{max_budget_usd}",
        ]
        if max_turns_arg is not None:
            argv += ["--max-turns", str(int(max_turns_arg))]
        if model_arg:
            argv += ["--model", str(model_arg)]

        env_overlay: dict[str, Optional[str]] = {
            "HOME": os.fspath(mimir_home),
            # Match claude_agent_sdk's subprocess transport: don't let
            # the spawn detect itself as nested in a Claude Code
            # session (changes its hook + env behavior in confusing
            # ways).
            "CLAUDECODE": None,
        }

        spawn_started_at = time.time()

        def _on_complete(job: ShellJob) -> None:
            """Waiter-thread completion handler. Reads stdout, parses
            JSON, classifies result, schedules async writes, then
            chains to the regular shell_job_complete wake-up.
            Exceptions are logged but never propagated — registry
            already guards but defense in depth here too."""
            spawn_finished_at = time.time()
            try:
                stdout_text = ""
                try:
                    stdout_text = job.stdout_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    pass
                parsed = _parse_spawn_result_json(stdout_text)
                if parsed is None:
                    event_type = "claude_code_spawn_work_failed"
                    payload: dict[str, Any] = {
                        "job_id": job.job_id,
                        "agent": agent_name,
                        "exit_code": job.exit_code,
                        "duration_ms": int(
                            (spawn_finished_at - spawn_started_at) * 1000
                        ),
                        "terminal_reason": "parse_failed",
                        "parse_failed": True,
                        "channel_id": job.channel_id,
                    }
                else:
                    event_type, classified_fields = _classify_terminal(parsed)
                    payload = {
                        "job_id": job.job_id,
                        "agent": agent_name,
                        "exit_code": job.exit_code,
                        "channel_id": job.channel_id,
                        **classified_fields,
                    }
                record = _build_spawn_record(
                    job_id=job.job_id,
                    channel_id=job.channel_id,
                    agent_name=agent_name,
                    parsed=parsed,
                    spawn_started_at=spawn_started_at,
                    spawn_finished_at=spawn_finished_at,
                )

                async def _emit() -> None:
                    await log_event(event_type, **payload)
                    if turn_logger is not None:
                        try:
                            await turn_logger.write(record)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "spawn synthetic turn-record write failed",
                            )

                schedule_from_thread(_emit())
            except Exception:  # noqa: BLE001
                log.exception(
                    "spawn completion accounting failed for %s", job.job_id,
                )
            finally:
                if chain_on_complete is not None:
                    try:
                        chain_on_complete(job)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "chain_on_complete raised for %s", job.job_id,
                        )

        try:
            job = registry.spawn(
                f"claude -p (agent={agent_name})",
                argv=argv,
                channel_id=channel_id,
                on_complete=_on_complete,
                env_overlay=env_overlay,
                cwd=working_dir,
            )
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "claude_code_spawn_spawn_failed",
                agent=agent_name,
                working_dir=working_dir,
                error=str(exc)[:500],
                channel_id=channel_id,
            )
            return _content_block(
                f"spawn_claude_code failed to launch: {exc}",
                is_error=True,
            )

        # Redact the brief out of the argv before logging — the brief
        # itself can be tens of KB and lives at brief_path. Replace the
        # element with a pointer so post-mortem grep sees the flag set
        # ("--agent foo --max-budget-usd 25 ...") without bloating
        # events.jsonl.
        cmd_argv_redacted = list(argv)
        try:
            brief_idx = cmd_argv_redacted.index(brief_text)
            cmd_argv_redacted[brief_idx] = f"<brief at {brief_path}>"
        except ValueError:
            pass
        await log_event(
            "claude_code_spawn_started",
            job_id=job.job_id,
            agent=agent_name,
            working_dir=working_dir,
            brief_path=str(brief_path),
            max_budget_usd=max_budget_usd,
            timeout_sec=timeout_sec,
            channel_id=channel_id,
            cmd_argv=cmd_argv_redacted,
            resolved_model=str(model_arg) if model_arg else None,
            resolved_max_turns=int(max_turns_arg) if max_turns_arg is not None else None,
        )

        return _content_block(
            f"Spawned claude_code job {job.job_id} (agent={agent_name}, "
            f"budget ${max_budget_usd:.0f}). Brief at {brief_path}. "
            f"On exit a claude_code_spawn_{{completed,auth_failed,"
            f"work_failed}} event fires plus a shell_job_complete "
            f"wake-up on this channel. Use bash_job_output("
            f"job_id={job.job_id!r}) to peek at progress."
        )

    return [spawn_claude_code]


__all__: tuple[str, ...] = (
    "build_spawn_tool",
    "spawn_tool_names",
    "PROFILE_DEFAULTS",
    "DEFAULT_AGENT",
    "DEFAULT_TIMEOUT_SEC",
)
