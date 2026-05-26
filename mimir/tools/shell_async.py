"""Async-shell tools: ``bash_async`` / ``bash_jobs_list`` / ``bash_job_output``.

Port of ``mimir/shelltools.py`` from main, adapted from the
SDK MCP-tool surface to native LangChain ``@tool`` callables. The
synchronous ``shell_exec`` in ``mimir/tools/extra.py`` is fine for
sub-second commands, but anything that has to wait on a webhook /
CI / long build / etc. needs the async-job path so the dispatcher's
event loop isn't held captive for the full subprocess duration.

A ``shell_job_complete`` AgentEvent fires when each spawned process
exits, so the agent gets a fresh turn with the exit-code + tail
output rather than having to poll ``bash_jobs_list``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.tools import tool

from ..shell_jobs import (
    SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES,
    ShellJobRegistry,
    normalize_shell_job_scope,
    normalize_shell_job_stream,
    parse_shell_job_tail_lines,
    shell_job_snapshots,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent normalization for wait-on-pending guard (chainlink #189)
# ---------------------------------------------------------------------------

_INTENT_PREFIX_MAX_CHARS = 100
_ENV_VAR_TOKEN_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")


def _intent_prefix(command: str) -> str:
    """Return a normalized intent string for duplicate-spawn detection.

    Strips leading env-var assignment tokens so env-export variant retries
    map to the same intent key as the original command.  Handles:

    - ``export FOO=bar social-cli …``  (``export`` keyword + VAR=val tokens)
    - ``FOO=bar BAZ=qux social-cli …``  (bare VAR=val prefix, no ``export``)
    - Multiline variants (each leading line stripped)
    - Mixed same-line and multi-line patterns

    Returns the first ``_INTENT_PREFIX_MAX_CHARS`` characters of the
    remaining command body, lower-cased with whitespace collapsed.

    Intent equality is heuristic — identical prefixes are sufficient evidence
    of same-intent for the guard.  Commands with different syntax but the
    same logical goal may not match; the guard is tuned for the observed
    ``bash_async`` retry pattern (env-export prefix variations before the
    same executable + args).
    """
    # Tokenise and skip leading ``export`` keywords and ``VAR=val`` tokens.
    tokens = command.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "export":
            # ``export`` keyword: consume it and keep scanning VAR=val tokens.
            i += 1
        elif _ENV_VAR_TOKEN_RE.match(tok):
            # ``VAR=val`` assignment — skip.
            i += 1
        else:
            break
    remaining = " ".join(tokens[i:])
    normalized = " ".join(remaining.lower().split())
    return normalized[:_INTENT_PREFIX_MAX_CHARS]


# Module-level dependency injection — populated by ``server.py:build_app``
# (or, in tests, by hand). ``_on_complete`` is the bridge into
# ``Agent._handle_shell_job_complete`` that fires the
# ``shell_job_complete`` AgentEvent when a job exits.
_REGISTRY: Optional[ShellJobRegistry] = None
_ON_COMPLETE: Optional[Callable[[Any], None]] = None


def set_shell_job_registry(
    registry: ShellJobRegistry,
    on_complete: Callable[[Any], None] | None = None,
) -> None:
    """Wire the per-process ShellJobRegistry into the async-shell tools.

    Called once at startup from ``server.py:build_app``. ``on_complete``
    is the wake-up bridge: when a spawned subprocess exits, it's
    invoked with the ``ShellJob`` so the agent dispatcher gets a
    fresh ``shell_job_complete`` turn. ``None`` is valid (test
    harnesses + bench runners); jobs still spawn + log, but no event
    fires on exit.
    """
    global _REGISTRY, _ON_COMPLETE
    _REGISTRY = registry
    _ON_COMPLETE = on_complete


@tool(
    "bash_async",
    description=(
        "Spawn a shell command in the background. Returns immediately "
        "with a ``job_id``. When the command exits, a "
        "``shell_job_complete`` event fires on this channel with the "
        "exit code and tail output — you'll see it as a fresh turn, "
        "no need to poll. Use this for commands that block on an event "
        "you're waiting for (a webhook, a CI pipeline, a long build). "
        "Don't use for sub-second commands — call shell_exec instead. "
        "Don't use for things that might never finish — wrap in "
        "``timeout 1h ...``."
    ),
)
async def bash_async(
    command: str,
    session_id: Optional[str] = None,
) -> str:
    """Args:
        command: The shell command to spawn. Runs via ``bash -lc`` so
            PATH and login env are loaded.
        session_id: Optional saga session id, threaded onto the
            completion event so it routes back to the right channel.
    """
    if _REGISTRY is None:
        return "bash_async failed: no shell-job registry configured"
    if not command or not command.strip():
        return "bash_async failed: command is required"

    # Resolve channel for completion-event routing. We prefer the
    # turn-current channel (set by Agent.run_turn) when available; if
    # we can't find one, the job still spawns but the completion
    # event fires on no channel (operator-visible via events.jsonl).
    from .._context import get_current_turn
    from .registry import _STATE as _registry_state
    ctx = get_current_turn()
    channel_id: str | None = None
    if ctx is not None:
        channel_id = getattr(ctx, "channel_id", None)
    if not channel_id:
        channel_id = (_registry_state.get("current_channel_id") or "").strip() or None

    # Wait-on-pending guard (chainlink #189): refuse if a same-intent job is
    # already running on this channel.  Prevents the retry-escalation failure
    # mode where the agent spawns N async variants of the same command without
    # ever seeing results from the first (each variant has slightly different
    # env exports, so the generic 3× circuit-breaker doesn't trip).
    #
    # Scope: per-channel.  Jobs running on other channels (unrelated pollers,
    # other conversations) don't block each other — they have different intents
    # by nature.
    if channel_id is not None:
        new_intent = _intent_prefix(command)
        for running_job in _REGISTRY.running_jobs():
            if running_job.channel_id != channel_id:
                continue
            if _intent_prefix(running_job.command) == new_intent:
                return (
                    f"bash_async refused: a job with the same intent is already "
                    f"running (job_id={running_job.job_id!r}, pid={running_job.pid}). "
                    f"Check its status with "
                    f"bash_job_output(job_id={running_job.job_id!r}) before spawning "
                    f"a retry.  To see all in-flight jobs: bash_jobs_list()."
                )

    try:
        job = _REGISTRY.spawn(
            command,
            argv=["bash", "-lc", command],
            channel_id=channel_id,
            on_complete=_ON_COMPLETE,
        )
    except Exception as exc:  # noqa: BLE001
        return f"bash_async failed: {exc}"

    return (
        f"Spawned job {job.job_id} (pid {job.pid}). When it exits, a "
        f"shell_job_complete event will fire on this channel with the "
        f"exit code and output tail. Check progress with "
        f"``bash_jobs_list`` or "
        f"``bash_job_output(job_id={job.job_id!r})``."
    )


@tool(
    "bash_jobs_list",
    description=(
        "List registered async shell jobs. ``scope`` ∈ "
        "{running, visible, all} — running is the default (in-flight "
        "only); visible adds recently-finished jobs; all includes "
        "everything in the registry."
    ),
)
async def bash_jobs_list(scope: Optional[str] = None) -> str:
    """Args:
        scope: One of ``running`` (default), ``visible``, ``all``.
    """
    if _REGISTRY is None:
        return "bash_jobs_list failed: no shell-job registry configured"
    try:
        resolved_scope = normalize_shell_job_scope(scope)
    except ValueError as exc:
        return f"bash_jobs_list failed: {exc}"
    snapshots = shell_job_snapshots(_REGISTRY, scope=resolved_scope)
    if not snapshots:
        return f"No jobs in scope={resolved_scope}."
    lines = [f"Jobs (scope={resolved_scope}, count={len(snapshots)}):"]
    for s in snapshots:
        cmd = (s.get("command") or "")[:120]
        lines.append(
            f"  {s['job_id']} [{s['status']}] "
            f"elapsed={s['elapsed_seconds']}s pid={s['pid']} — {cmd}"
        )
    return "\n".join(lines)


@tool(
    "bash_job_output",
    description=(
        "Return tail of stdout/stderr for one job. ``stream`` ∈ "
        "{stdout, stderr, both}; ``tail_lines`` defaults to "
        f"{SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES}. Use after spawning "
        "to check mid-flight progress, or after a shell_job_complete "
        "event to read more than the wake-up summary tail."
    ),
)
async def bash_job_output(
    job_id: str,
    tail_lines: Optional[int] = None,
    stream: Optional[str] = None,
) -> str:
    """Args:
        job_id: The ``job_id`` returned by ``bash_async``.
        tail_lines: How many lines from the end to include
            (default ``SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES``).
        stream: ``stdout`` / ``stderr`` / ``both`` (default ``both``).
    """
    if _REGISTRY is None:
        return "bash_job_output failed: no shell-job registry configured"
    if not job_id:
        return "bash_job_output failed: job_id is required"
    try:
        resolved_tail = parse_shell_job_tail_lines(tail_lines)
        resolved_stream = normalize_shell_job_stream(stream)
    except ValueError as exc:
        return f"bash_job_output failed: {exc}"

    # ``read_output`` does sync file IO (seek-from-end tail). Wrap in
    # ``asyncio.to_thread`` so a multi-MB read doesn't freeze the
    # event loop while the tail walks backward through chunks.
    result = await asyncio.to_thread(
        _REGISTRY.read_output,
        job_id, tail_lines=resolved_tail, stream=resolved_stream,
    )
    if "error" in result:
        return result["error"]
    lines = [
        f"Job {result['job_id']} [{result['status']}] "
        f"elapsed={result['elapsed_seconds']}s pid={result['pid']} "
        f"exit_code={result['exit_code']}",
        f"Command: {result.get('command', '')}",
    ]
    if resolved_stream in ("stdout", "both"):
        stdout = result.get("stdout_tail") or "(empty)"
        lines.append("")
        lines.append("--- stdout tail ---")
        lines.append(stdout)
    if resolved_stream in ("stderr", "both"):
        stderr = result.get("stderr_tail") or "(empty)"
        lines.append("")
        lines.append("--- stderr tail ---")
        lines.append(stderr)
    return "\n".join(lines)


__all__ = (
    "bash_async",
    "bash_jobs_list",
    "bash_job_output",
    "set_shell_job_registry",
)
