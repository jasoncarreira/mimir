"""SDK hooks that wrap built-in Read/Write/Edit/Bash/Glob (SPEC §7.3).

Why hooks instead of custom MCP tools for the file-op surface:
- The SDK's Read/Write/Edit/Bash are battle-tested in Claude Code and the
  model is fluent in their interface (no per-turn ToolSearch round-trip).
- Hooks let us layer mimir-specific concerns on top:
    * **Path confinement** — PreToolUse denies any tool call whose path arg
      escapes ``<home>``. The deny reason surfaces to the model verbatim.
    * **Reindex** — PostToolUse on Write/Edit enqueues an incremental search
      reindex of the affected file (SPEC §6.3).

Tradeoff vs. the prior MCP file-op tools:
- We give up cross-process ``flock`` semantics (the actual write happens in
  the CLI subprocess; mimir's process can't transactionally wrap it). For
  the single-mimir-per-deployment topology the benchmark and Slack/Discord
  use, the dispatcher's per-channel queue plus the global concurrency cap
  provide the serialization we actually need. Note this in the spec when
  multi-process deployments come up.
- The "old_string not unique" check was custom; SDK Edit's native error is
  good enough ("Found N matches" vs. our "old_string is not unique (N matches)").
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import HookContext

from ._paths import PathOutsideHomeError, resolve_home_path

log = logging.getLogger(__name__)


# Tool names the model sees (CLI presets). Kept as strings to avoid hard
# dependence on SDK-internal symbols and to make the matchers obvious.
PATH_GUARDED_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "MultiEdit", "NotebookEdit"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Delivery primitives that bypass the per-turn tool-call budget. The budget
# exists to cap retrieval/search panic loops; if we counted send_message and
# react against it, an over-budget agent would have no exit hatch — the
# deny message tells it to "answer NOW via send_message," which would itself
# be denied. So we don't increment OR check for these, leaving the agent
# always able to deliver its reply.
BUDGET_EXEMPT_TOOLS = {"mcp__mimir__send_message", "mcp__mimir__react"}


# Argument name(s) that carry a filesystem path, per tool. Hooks only validate
# the keys the SDK actually populates — extras are ignored. Web tools
# (WebSearch, WebFetch) and Bash are intentionally absent from this map —
# Bash inherits cwd-confinement; web tools work on URLs, not paths.
_PATH_KEYS = {
    "Read": ["file_path"],
    "Write": ["file_path"],
    "Edit": ["file_path"],
    "MultiEdit": ["file_path"],
    "NotebookEdit": ["notebook_path"],
    "Glob": ["path"],     # Glob's path is the optional search root, not the pattern
    "Grep": ["path"],     # Same — search root, optional
}


def _extract_target_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Pick the path-style argument(s) the tool will operate on."""
    out: list[str] = []
    for key in _PATH_KEYS.get(tool_name, []):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    return out


def _to_relative(home: Path, raw: str) -> str:
    """Best-effort relative path from an absolute or relative arg, for the
    reindex hook. Returns '' if the path can't be made relative to home."""
    p = Path(raw)
    if p.is_absolute():
        try:
            return p.relative_to(home.resolve()).as_posix()
        except ValueError:
            return ""
    return p.as_posix()


def make_pre_tool_use_hook(home: Path) -> Callable[..., Awaitable[dict[str, Any]]]:
    """PreToolUse hook: deny any path arg that escapes ``<home>``.

    Permission rules in ClaudeAgentOptions can also constrain the toolset by
    directory, but a hook keeps the deny message human-readable and lets us
    log the rejection to events.jsonl for introspection.
    """
    home_resolved = home.resolve()

    async def pre_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")

        # ── Tool-call budget (FUTURE_WORK / addresses 308-call panic loops) ──
        # Counted across all tools EXCEPT the delivery primitives in
        # BUDGET_EXEMPT_TOOLS — the agent must always be able to send/react
        # so it has an exit hatch from the budget. The deny message points
        # the agent at send_message; if send_message itself were denied, the
        # agent would be wedged.
        from ._context import get_current_turn
        from .event_logger import log_event

        ctx = get_current_turn()
        if (
            ctx is not None
            and ctx.tool_call_budget > 0
            and tool_name not in BUDGET_EXEMPT_TOOLS
        ):
            ctx.tool_call_count += 1
            budget = ctx.tool_call_budget
            count = ctx.tool_call_count
            if count > budget:
                await log_event(
                    "tool_call_denied",
                    tool=tool_name,
                    reason="tool_call_budget_exceeded",
                    count=count,
                    budget=budget,
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Tool-call budget exhausted ({count}/{budget} for "
                            f"this turn). Stop searching — answer NOW with what "
                            f"you've already found via mcp__mimir__send_message. "
                            f"If you don't have the answer, say 'I don't have "
                            f"that information stored.' Further searches won't "
                            f"surface information that wasn't filed."
                        ),
                    }
                }
            soft_threshold = max(1, int(budget * 0.7))
            if count == soft_threshold:
                # Pass-through allow, but the model sees the warning in the
                # next turn's events.jsonl + the description carries through
                # via the additionalContext mechanism.
                await log_event(
                    "tool_call_budget_warning",
                    tool=tool_name,
                    count=count,
                    budget=budget,
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": (
                            f"Heads up: tool-call budget at {count}/{budget} "
                            f"for this turn. Wrap up your search and answer "
                            f"with what you have."
                        ),
                    }
                }

        # ── Path confinement (existing behavior) ─────────────────────────────
        if tool_name not in PATH_GUARDED_TOOLS:
            return {}
        tool_input = input_data.get("tool_input") or {}
        for path_arg in _extract_target_paths(tool_name, tool_input):
            try:
                resolve_home_path(home_resolved, path_arg)
            except PathOutsideHomeError as exc:
                await log_event(
                    "tool_call_denied",
                    tool=tool_name,
                    reason="path_outside_home",
                    path=path_arg,
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"{tool_name} blocked: {exc}. All paths must be "
                            f"relative to the agent home ({home_resolved})."
                        ),
                    }
                }
        return {}

    return pre_tool_use


def make_post_tool_use_hook(
    home: Path,
    reindex: Callable[[str], Awaitable[None]] | None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """PostToolUse hook: incremental reindex after Write/Edit succeeds.

    The hook runs only if the indexer is wired in. Failures in the reindex
    path log to events.jsonl but never propagate back to the agent — a stale
    index is preferable to a tool-call error caused by indexing.
    """
    home_resolved = home.resolve()

    async def post_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        if reindex is None:
            return {}
        tool_name = input_data.get("tool_name", "")
        if tool_name not in WRITE_TOOLS:
            return {}
        tool_input = input_data.get("tool_input") or {}
        # If the tool errored, the SDK still fires PostToolUse; we skip in
        # that case to avoid trying to reindex a file that failed to write.
        response = input_data.get("tool_response")
        if isinstance(response, dict) and response.get("is_error"):
            return {}
        for path_arg in _extract_target_paths(tool_name, tool_input):
            rel = _to_relative(home_resolved, path_arg)
            if not rel:
                continue
            try:
                await reindex(rel)
            except Exception as exc:  # noqa: BLE001
                from .event_logger import log_event

                await log_event(
                    "reindex_hook_error",
                    tool=tool_name,
                    rel_path=rel,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return {}

    return post_tool_use
