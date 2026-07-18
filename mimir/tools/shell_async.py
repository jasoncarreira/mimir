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
import logging
import re
from typing import Annotated, Any, Callable, Optional

from langchain_core.tools import InjectedToolArg, tool

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
# Intent normalization for wait-on-pending guard (chainlink #189 / #192)
# ---------------------------------------------------------------------------

# Bumped from 100 → 200 to reduce false-positive truncation collisions where
# two genuinely-different commands share a long common prefix (chainlink #192).
_INTENT_PREFIX_MAX_CHARS = 200
_ENV_VAR_TOKEN_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")

# Matches shell-wrapper invocations: /bin/bash -c, bash -lc, /bin/sh -c, etc.
# Used to unwrap the inner command before intent normalisation.
_SHELL_WRAPPER_RE = re.compile(
    r"^\s*(?:/[^\s]*/)?(?:ba)?sh\s+(?:-[a-z]+\s+)+",
    re.IGNORECASE,
)

# Matches AT-protocol and HTTP/S URIs — used as a supplementary intent key.
_URI_RE = re.compile(r"(?:at://|https?://)\S+")


def _unwrap_shell_wrapper(command: str) -> str:
    """Strip a shell-wrapper prefix and return the inner command.

    If ``command`` is of the form ``/bin/bash -c '...'`` (or any
    ``[/path/]bash``/``sh`` with ``-c`` / ``-lc`` / etc.), extract the
    quoted inner body and return it.  Handles both single-quoted and
    double-quoted bodies.  Returns ``command`` unchanged if it does not
    start with a recognised shell wrapper.

    Examples::

        '/bin/bash -c "cd /tmp && echo hi"'  →  'cd /tmp && echo hi'
        "bash -lc 'export X=1 && foo bar'"   →  'export X=1 && foo bar'
        'echo hello'                          →  'echo hello'  (unchanged)
    """
    stripped = command.strip()
    m = _SHELL_WRAPPER_RE.match(stripped)
    if not m:
        return command
    after_shell = stripped[m.end():]
    if not after_shell:
        return command
    # Strip outer single or double quotes if the remainder is fully quoted.
    if after_shell[0] in ("'", '"'):
        quote_char = after_shell[0]
        close = after_shell.rfind(quote_char)
        if close > 0:
            return after_shell[1:close]
    return after_shell


def _take_last_chain_segment(command: str) -> str:
    """Return the last ``&&``-separated segment of a shell command chain.

    Strips leading ``cd /path &&`` patterns and similar multi-step setup
    chains so the core executable is what remains.

    Examples::

        'cd /mimir-home && social-cli like at://...'  →  'social-cli like at://...'
        'cd /a && export X=1 && node cli.js foo'      →  'node cli.js foo'
        'git fetch origin'                             →  'git fetch origin' (unchanged)
    """
    parts = [p.strip() for p in command.split("&&")]
    parts = [p for p in parts if p]
    return parts[-1] if parts else command


def _intent_prefix(command: str) -> str:
    """Return a normalized intent string for duplicate-spawn detection.

    Handles (in order):

    1. **Shell-wrapper escalation** (chainlink #192) — strips ``/bin/bash -c '...'``
       wrappers so the inner command is what's compared.
    2. **``&&``-chain prefixes** — takes the last segment of chains like
       ``cd /path && export X=1 && <real-command>``.
    3. **Leading env-var exports** — strips ``export FOO=bar`` and bare
       ``VAR=val`` prefix tokens (original chainlink #189 logic).
    4. **Absolute-path executables** — normalises ``/usr/bin/node`` →
       ``node`` so the guard is insensitive to whether the caller used a
       symbolic name or the full binary path.

    Returns the first ``_INTENT_PREFIX_MAX_CHARS`` characters lower-cased
    with whitespace collapsed.  Intent equality is heuristic; see also
    ``_intent_suffix_key`` for the URI-based supplementary check.
    """
    cmd = command.strip()

    # 1. Unwrap shell wrappers (e.g. /bin/bash -c '...')
    cmd = _unwrap_shell_wrapper(cmd)

    # 2. Take the last segment of a && chain (strips cd /path && ... prefixes)
    cmd = _take_last_chain_segment(cmd)

    # 3. Tokenise and skip leading ``export`` keywords and ``VAR=val`` tokens.
    tokens = cmd.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "export":
            i += 1
        elif _ENV_VAR_TOKEN_RE.match(tok):
            i += 1
        else:
            break
    tokens = tokens[i:]

    # 4. Normalise the executable: absolute paths → basename.
    if tokens and "/" in tokens[0]:
        tokens[0] = tokens[0].rsplit("/", 1)[-1]

    remaining = " ".join(tokens)
    normalized = " ".join(remaining.lower().split())
    return normalized[:_INTENT_PREFIX_MAX_CHARS]


def _intent_suffix_key(command: str) -> str | None:
    """Return the rightmost URI in the command, or ``None`` if absent.

    Supplementary intent key for wrapper-invariance (chainlink #192): when
    an agent escalates from ``social-cli like at://X`` to a bash-wrapped
    ``/usr/bin/node cli.js dispatch at://X``, the executables and verb differ
    so ``_intent_prefix`` misses the match.  The AT-URI ``at://X`` is constant
    across all escalation levels and serves as a reliable secondary signal.

    Matches ``at://…``, ``https://…``, and ``http://…`` URIs.  Strips
    trailing shell-quote artefacts (``'``, ``"``).  Returns the rightmost
    match lower-cased so the check is case-insensitive.
    """
    # Strip trailing quotes that bash -c wrappers leave on the last token.
    cmd = command.strip().rstrip("'\"")
    matches = _URI_RE.findall(cmd)
    if not matches:
        return None
    uri = matches[-1].rstrip("'\".,;)")
    return uri.lower()


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
    mimir_direct_argv: Annotated[Optional[list[str]], InjectedToolArg] = None,
) -> str:
    """Args:
        command: The shell command to spawn. Runs via ``bash -lc`` so
            PATH and login env are loaded.
        session_id: Optional saga session id, threaded onto the
            completion event so it routes back to the right channel.
        mimir_direct_argv: Server-injected exact argv for trusted-service calls.
    """
    if _REGISTRY is None:
        return "bash_async failed: no shell-job registry configured"
    if not command or not command.strip():
        return "bash_async failed: command is required"

    # Resolve channel for completion-event routing. We prefer the
    # turn-current channel (set by Agent.run_turn) when available; if
    # we can't find one, the job still spawns but the completion
    # event fires on no channel (operator-visible via events.jsonl).
    # chainlink #392: resolve the routing channel via the purpose-built
    # three-level chain (saga_session_id -> single_active -> contextvar). The
    # old _STATE["current_channel_id"] fallback was DEAD — the S2-1 fix moved the
    # per-turn channel to a ContextVar (_current_channel_id_var) this path never
    # read — and get_current_turn() alone returns stale None under SDK/MCP
    # forked-task dispatch. The result was channel_id=None: the dup-spawn guard
    # below was silently skipped and the shell_job_complete wake-up was dropped
    # (_on_shell_job_complete early-returns on a None channel).
    from .._context import resolve_active_ctx
    from .registry import _current_channel_id_var
    ctx, _resolution = resolve_active_ctx({"session_id": session_id})
    channel_id: str | None = None
    if ctx is not None:
        channel_id = getattr(ctx, "channel_id", None)
    if not channel_id:
        # Live per-task channel id (set by the dispatcher), replacing the dead
        # _STATE key.
        channel_id = (_current_channel_id_var.get() or "").strip() or None

    # Wait-on-pending guard (chainlink #189 / #192): refuse if a same-intent
    # job is already running on this channel.  Prevents the retry-escalation
    # failure mode where the agent spawns N async variants of the same command
    # without ever seeing results from the first.
    #
    # Two-key match (chainlink #192 — wrapper-invariance):
    #   1. Prefix key — normalised first N chars after stripping wrappers,
    #      cd-chains, env-exports, and path prefixes on the executable.
    #      Catches plain retries and env-export variants.
    #   2. Suffix key — rightmost AT-URI / HTTP-URI in the raw command.
    #      Catches wrapper-escalation where the caller swaps
    #      ``social-cli like at://X`` for ``/bin/bash -c '… node cli.js
    #      dispatch at://X'``: the executable + verb differ so the prefix
    #      key misses, but the URI target is constant across all levels.
    #
    # Scope: per-channel.  Jobs on other channels don't block each other.
    #
    # chainlink #193: emit algedonic event on refusal so the operator can
    # audit "how many refused respawns happened" without reading turn
    # transcripts.  The tool return-string is the agent-visible signal;
    # the event is the operator/introspection-visible audit trail.
    if channel_id is not None:
        new_intent = _intent_prefix(command)
        new_suffix = _intent_suffix_key(command)
        for running_job in _REGISTRY.running_jobs():
            if running_job.channel_id != channel_id:
                continue
            prefix_match = _intent_prefix(running_job.command) == new_intent
            suffix_match = (
                new_suffix is not None
                and new_suffix == _intent_suffix_key(running_job.command)
            )
            if prefix_match or suffix_match:
                match_kind = "prefix" if prefix_match else "uri-target"
                # chainlink #193: best-effort event so operator audit doesn't
                # require reading turn transcripts.  ``safe_log_event`` silences
                # logger-not-initialized in test harnesses / bench runners.
                from ..event_logger import safe_log_event as _safe_log  # lazy — avoids top-level cycle
                await _safe_log(
                    "bash_async_refused_same_intent",
                    channel_id=channel_id,
                    running_job_id=running_job.job_id,
                    new_command=command[:200],
                    intent_prefix=new_intent,
                )
                return (
                    f"bash_async refused: a job with the same intent is already "
                    f"running (job_id={running_job.job_id!r}, pid={running_job.pid}, "
                    f"match={match_kind!r}). "
                    f"Check its status with "
                    f"bash_job_output(job_id={running_job.job_id!r}) before spawning "
                    f"a retry.  To see all in-flight jobs: bash_jobs_list()."
                )

    try:
        from ._shell_env import direct_exec_env, login_shell_command
        argv = (
            mimir_direct_argv
            if mimir_direct_argv is not None
            else ["bash", "-lc", login_shell_command(command)]
        )
        spawn_kwargs: dict[str, Any] = {
            "argv": argv,
            "channel_id": channel_id,
            "on_complete": _ON_COMPLETE,
        }
        if mimir_direct_argv is not None:
            spawn_kwargs["env_overlay"] = direct_exec_env()
        job = _REGISTRY.spawn(
            command,  # original (clean) command recorded for display
            **spawn_kwargs,
        )
        job.ifc_labels = getattr(ctx, "ifc_labels", None)
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
    # Exposed for testing:
    "_intent_prefix",
    "_intent_suffix_key",
    "_unwrap_shell_wrapper",
    "_take_last_chain_segment",
)
