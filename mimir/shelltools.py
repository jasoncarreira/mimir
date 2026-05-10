"""Async shell-job MCP tools.

Three tools:

- ``bash_async`` — spawn a shell command in the background, return the
  ``job_id`` immediately. When the command exits the registry fires a
  ``shell_job_complete`` AgentEvent that wakes the spawning channel
  with the job's exit code + tail output.
- ``bash_jobs_list`` — list registered jobs (running by default;
  ``scope`` ∈ {running, visible, all}).
- ``bash_job_output`` — tail one job's stdout/stderr.

These are MCP-dispatched tools, so they hit the chainlink #23 forked-task
ctx-staleness pattern — they use ``resolve_active_ctx`` (the same
three-level lookup chain saga tools use) to find the spawning channel
when the model doesn't pass ``session_id`` explicitly.

The agent's regular ``Bash`` tool is the SDK preset — sync, blocking.
``bash_async`` is the new MCP-side companion: same shell, just detached
with a wake-up callback.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from claude_agent_sdk import SdkMcpTool, tool

from ._context import resolve_active_ctx
from ._tool_helpers import _content_block, _need, _safe
from .event_logger import log_event
from .shell_jobs import (
    SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES,
    ShellJob,
    ShellJobRegistry,
    normalize_shell_job_scope,
    normalize_shell_job_stream,
    parse_shell_job_tail_lines,
    shell_job_snapshots,
)


log = logging.getLogger(__name__)


def shell_tool_names() -> list[str]:
    return [
        "mcp__mimir__bash_async",
        "mcp__mimir__bash_jobs_list",
        "mcp__mimir__bash_job_output",
    ]


def build_shell_tools(
    registry: ShellJobRegistry,
    on_complete: Callable[[ShellJob], None] | None = None,
) -> list[SdkMcpTool]:
    """Build the three async shell-job tools backed by ``registry``.

    Tools capture ``registry`` and ``on_complete`` in closure; the
    registry is constructed once per agent and lives for the process
    lifetime. ``on_complete`` is the bridge that fires the
    ``shell_job_complete`` AgentEvent when the subprocess exits — see
    ``Agent._handle_shell_job_complete`` for the implementation. When
    ``on_complete`` is None (e.g. unit tests building tools without an
    Agent), spawned jobs run silently to completion with no wake-up."""

    @tool(
        "bash_async",
        "Spawn a shell command in the background. Returns immediately with "
        "a ``job_id`` you can use to retrieve output later. When the "
        "command exits, a ``shell_job_complete`` event fires on this "
        "channel with the exit code and tail output — you'll see it as a "
        "fresh turn, no need to poll. Use this for commands that block on "
        "an event you're waiting for (a webhook arriving, a CI pipeline "
        "finishing, a long build) where you want THIS conversation to "
        "resume on completion. Don't use for sub-second commands — just "
        "call ``Bash`` synchronously. Don't use for things that might "
        "never finish without a timeout — wrap in ``timeout 1h ...``. "
        "Pass ``session_id`` (your current saga_session_id from the "
        "Current-message header) so the completion event routes back to "
        "this channel.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["command"],
        },
    )
    @_safe("bash_async")
    async def bash_async(args: dict[str, Any]) -> dict[str, Any]:
        command = _need(args, "command")
        ctx, resolution_path = resolve_active_ctx(args)
        await log_event(
            "bash_async_ctx_resolution",
            resolution_path=resolution_path,
            saga_session_id=ctx.saga_session_id if ctx else None,
            turn_id=ctx.turn_id if ctx else None,
        )
        channel_id = ctx.channel_id if ctx is not None else None
        # bash -lc gives a login shell so PATH and env are loaded; the
        # registry's spawn() handles process-group isolation and drainer
        # threads.
        job = registry.spawn(
            command,
            argv=["bash", "-lc", command],
            channel_id=channel_id,
            on_complete=on_complete,
        )
        await log_event(
            "bash_async_spawned",
            job_id=job.job_id,
            pid=job.pid,
            command=command[:500],
            channel_id=channel_id,
        )
        return _content_block(
            f"Spawned job {job.job_id} (pid {job.pid}). When it exits, a "
            f"shell_job_complete event will fire on this channel with the "
            f"exit code and output tail. Check progress with "
            f"``bash_jobs_list`` or ``bash_job_output(job_id={job.job_id!r})``."
        )

    @tool(
        "bash_jobs_list",
        "List registered async shell jobs. ``scope`` ∈ {running, visible, "
        "all} — running is the default (in-flight only); visible adds "
        "recently-finished jobs that ran long enough to surface; all "
        "includes everything in the registry. Returns a JSON list of "
        "snapshots (job_id, pid, command, status, exit_code, elapsed).",
        {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
            },
            "required": [],
        },
    )
    @_safe("bash_jobs_list")
    async def bash_jobs_list(args: dict[str, Any]) -> dict[str, Any]:
        try:
            scope = normalize_shell_job_scope(args.get("scope"))
        except ValueError as exc:
            return _content_block(f"bash_jobs_list failed: {exc}", is_error=True)
        snapshots = shell_job_snapshots(registry, scope=scope)
        if not snapshots:
            return _content_block(f"No jobs in scope={scope}.")
        # Render compact one-line-per-job summary.
        lines = [f"Jobs (scope={scope}, count={len(snapshots)}):"]
        for s in snapshots:
            lines.append(
                f"  {s['job_id']} [{s['status']}] elapsed={s['elapsed_seconds']}s "
                f"pid={s['pid']} — {s['command'][:120]}"
            )
        return _content_block("\n".join(lines))

    @tool(
        "bash_job_output",
        "Return tail of stdout/stderr for one job. ``stream`` ∈ {stdout, "
        "stderr, both}; ``tail_lines`` defaults to "
        f"{SHELL_JOB_OUTPUT_DEFAULT_TAIL_LINES}. Use after spawning to "
        "check progress mid-flight, or after a shell_job_complete event "
        "to see more than the wake-up summary's truncated tail.",
        {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tail_lines": {"type": "integer"},
                "stream": {"type": "string"},
            },
            "required": ["job_id"],
        },
    )
    @_safe("bash_job_output")
    async def bash_job_output(args: dict[str, Any]) -> dict[str, Any]:
        job_id = _need(args, "job_id")
        try:
            tail_lines = parse_shell_job_tail_lines(args.get("tail_lines"))
            stream = normalize_shell_job_stream(args.get("stream"))
        except ValueError as exc:
            return _content_block(f"bash_job_output failed: {exc}", is_error=True)
        # CR2 + PR #111 review: ``read_output`` does sync file IO
        # (seek-from-end tail). Wrap in ``to_thread`` so a multi-MB
        # read doesn't block the event loop while the tail walks
        # backward through chunks.
        import asyncio
        result = await asyncio.to_thread(
            registry.read_output,
            job_id, tail_lines=tail_lines, stream=stream,
        )
        if "error" in result:
            return _content_block(result["error"], is_error=True)
        # Render: header line + per-stream blocks. Keep it compact so
        # the agent's prompt budget isn't blown by a chatty job.
        lines = [
            f"Job {result['job_id']} [{result['status']}] "
            f"elapsed={result['elapsed_seconds']}s pid={result['pid']} "
            f"exit_code={result['exit_code']}",
            f"Command: {result['command']}",
        ]
        if stream in ("stdout", "both"):
            stdout = result.get("stdout_tail") or "(empty)"
            lines.append("")
            lines.append("--- stdout tail ---")
            lines.append(stdout)
        if stream in ("stderr", "both"):
            stderr = result.get("stderr_tail") or "(empty)"
            lines.append("")
            lines.append("--- stderr tail ---")
            lines.append(stderr)
        return _content_block("\n".join(lines))

    return [bash_async, bash_jobs_list, bash_job_output]


__all__: tuple[str, ...] = (
    "build_shell_tools",
    "shell_tool_names",
)
