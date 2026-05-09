"""Tool registration.

The model sees a hybrid surface:
- **SDK presets** (passed via ``ClaudeAgentOptions.tools``) — Read, Write,
  Edit, Bash, Glob. The CLI subprocess executes them; mimir wraps with hooks
  for path confinement (PreToolUse) and incremental reindex (PostToolUse).
  See ``mimir.hooks``.
- **MCP tools** (in-process via ``create_sdk_mcp_server``) — things with no
  SDK preset:
    * echo (smoke)
    * file_search, rebuild_index
    * get_turn (synthesis-turn helper — fetch turn output+events)
    * saga_query, saga_store, saga_feedback, saga_mark_contributions, saga_end_session
    * list_schedules, add_schedule, remove_schedule
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from .channel_registry import ChannelRegistry
from .channeltools import build_channel_tools, channel_tool_names
from .history import MessageBuffer
from .saga_client import SagaClient
from .sagatools import build_saga_tools, saga_tool_names
from .scheduler import Scheduler
from .session_boundary_log import SessionBoundaryLog
from .scheduletools import build_schedule_tools, schedule_tool_names
from .search import Indexer
from .searchtools import build_search_tools, search_tool_names
from .shell_jobs import ShellJob, ShellJobRegistry
from .shelltools import build_shell_tools, shell_tool_names
from .spawn import build_spawn_tool, spawn_tool_names
from .turn_logger import TurnLogger
from .turntools import build_turn_tools, turn_tool_names

# Built-in SDK preset tools we enable. Hooks (mimir.hooks) layer mimir-specific
# concerns on top — path confinement, post-write reindex.
#
# Web tools (WebSearch, WebFetch) need no hooks — URLs are inherently outside
# the path-confinement story. Grep complements Glob for in-home text search.
# Task is the SDK's subagent-dispatch tool; with it the model can invoke the
# .md-defined subagents under <home>/.claude/agents/ (climber/researcher/critic).
# MultiEdit's value is atomicity: a memory restructure that touches three
# spots in one file lands all-or-nothing instead of partial-on-failure, and
# fires PostToolUse / reindex once instead of three times.
#
# Deliberately NOT enabled:
# - NotebookEdit — mimir's home is markdown, not Jupyter. Toggle on if needed.
# MultiEdit appears in the bundled CLI binary's tool name table but the CLI
# rejects it at runtime with "No such tool available: MultiEdit" — it isn't
# actually wired into the Agent SDK's tool registration today (verified
# 2026-04-26 against bench-mimir runs). When/if it becomes available, add it
# back here AND restore the recommendation in
# benchmark/prompts/mimir/learned_behaviors.md.
SDK_PRESET_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "Task",
]


# ─── Embedded-XML-parameter detection (chainlink #21) ────────────────
#
# When the model malforms an MCP tool call by smuggling XML-style
# `<parameter name="...">` syntax inside a JSON string field, the
# underlying mcp library's input-schema validator returns a misleading
# "Input validation error: 'X' is a required property" error — the
# real bug (XML in a string) is invisible, and the model often burns
# retries on the same shape (turn 9c8921ea286c on 2026-05-06 retried
# 6 times before giving up and skipping synthesis).
#
# Fix: wrap the CallToolRequest handler after create_sdk_mcp_server
# returns. When the handler emits an "Input validation error", scan
# the request's string-typed args for the giveaway pattern; if found,
# append a hint pointing at the actual cause. Cheap (regex over a
# typically-small args dict), only fires on the validation-error
# branch, no false positives on successful calls or non-validation
# tool errors.

# Match the tool-call XML's parameter-opening pattern. Closing tags
# alone (``</summary>``, ``</topics_discussed>``) were considered but
# false-positive on generic HTML (``</a>``, ``</span>``) — the
# load-bearing signal that this is a malformed XML-style tool call
# specifically (not just prose with angle brackets) is the
# ``<parameter name="...">`` opening, which is unique to the
# Anthropic XML tool-call format.
_XML_PARAM_RE = re.compile(r'<parameter\s+name="')

_XML_HINT = (
    "\n\nHint: one of the string args contains embedded XML parameter "
    "syntax (``<parameter name=\"...\">``). Pass each parameter as a "
    "separate JSON field in ``args`` — not as XML tags inside another "
    "string. The XML tool-call format and the JSON args format are "
    "different surfaces; mixing them silently drops the embedded params "
    "and leaves them as literal text in the surrounding string."
)


def _detect_embedded_xml(arguments: dict[str, Any]) -> bool:
    """True when any string-typed value in ``arguments`` (or in a
    nested list/dict of strings) contains the embedded-XML param
    pattern. Conservative: only matches the param-tag shape, not
    generic angle-bracket text."""
    if not isinstance(arguments, dict):
        return False

    def _scan(value: Any) -> bool:
        if isinstance(value, str):
            return bool(_XML_PARAM_RE.search(value))
        if isinstance(value, list):
            return any(_scan(v) for v in value)
        if isinstance(value, dict):
            return any(_scan(v) for v in value.values())
        return False

    return any(_scan(v) for v in arguments.values())


def _install_xml_hint_wrapper(server: Any) -> None:
    """Wrap the in-process MCP server's CallToolRequest handler so
    that ``Input validation error: ...`` results get an embedded-XML
    hint appended when the original arguments contained the giveaway
    pattern. Idempotent against re-installation: tags the wrapper
    so we don't double-wrap on repeated calls.

    No-op (logs only) if the mcp library's internal shapes shift —
    the wrapper is observability/DX, not load-bearing, so it must
    never break tool dispatch."""
    try:
        from mcp import types as mcp_types
    except Exception:  # noqa: BLE001 - tolerate any import-shape drift
        return

    handlers = getattr(server, "request_handlers", None)
    if not isinstance(handlers, dict):
        return
    original = handlers.get(mcp_types.CallToolRequest)
    if original is None or getattr(original, "_mimir_xml_hint", False):
        return

    async def wrapped(req: Any) -> Any:
        result = await original(req)
        try:
            cr = getattr(result, "root", None)
            if cr is None or not getattr(cr, "isError", False):
                return result
            content = list(getattr(cr, "content", []) or [])
            if not content:
                return result
            first = content[0]
            text = getattr(first, "text", None)
            if not isinstance(text, str) or not text.startswith(
                "Input validation error:"
            ):
                return result
            args = (getattr(req.params, "arguments", None) or {})
            if not _detect_embedded_xml(args):
                return result
            # Mutate the existing TextContent's text so the rest of
            # the result (other content blocks, isError, structured)
            # is preserved verbatim.
            try:
                first.text = text + _XML_HINT
            except Exception:  # noqa: BLE001
                # Pydantic v2 model mutation can be blocked by
                # frozen=True; rebuild the content block instead.
                from mcp.types import TextContent as _TC

                content[0] = _TC(type="text", text=text + _XML_HINT)
                cr.content = content
        except Exception:  # noqa: BLE001
            # Any unexpected shape — return the original result
            # unchanged. DX polish must never break tool dispatch.
            return result
        return result

    wrapped._mimir_xml_hint = True  # type: ignore[attr-defined]
    handlers[mcp_types.CallToolRequest] = wrapped


@tool("echo", "Echo a string back. Useful for smoke tests.", {"text": str})
async def echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": args.get("text", "")}]}


def build_mcp_server(
    home: Path,
    indexer: Indexer | None = None,
    saga_client: SagaClient | None = None,
    scheduler: Scheduler | None = None,
    channel_registry: ChannelRegistry | None = None,
    message_buffer: MessageBuffer | None = None,
    session_boundary_log: SessionBoundaryLog | None = None,
    turns_log: Path | None = None,
    turn_logger: TurnLogger | None = None,
    shell_jobs: ShellJobRegistry | None = None,
    on_shell_job_complete: Any | None = None,
    schedule_from_thread: Any | None = None,
    mimir_home: Path | None = None,
) -> McpSdkServerConfig:
    """Bundle the in-process MCP tools (everything with no SDK preset)."""
    tools = [echo]
    if indexer is not None:
        tools += build_search_tools(indexer)
    if turns_log is not None:
        tools += build_turn_tools(turns_log)
    if saga_client is not None:
        tools += build_saga_tools(saga_client, session_boundary_log=session_boundary_log)
    if scheduler is not None:
        tools += build_schedule_tools(scheduler)
    if shell_jobs is not None:
        tools += build_shell_tools(shell_jobs, on_complete=on_shell_job_complete)
    # spawn_claude_code (chainlink #60): registered when both shell_jobs
    # and the cross-thread schedule bridge are available. The spawn tool
    # uses the same registry as bash_async (so the spawn appears in
    # bash_jobs_list), but its completion path does extra accounting
    # before chaining to ``on_shell_job_complete`` for the wake-up.
    if (
        shell_jobs is not None
        and schedule_from_thread is not None
        and mimir_home is not None
    ):
        tools += build_spawn_tool(
            registry=shell_jobs,
            turn_logger=turn_logger,
            mimir_home=mimir_home,
            spawns_dir=mimir_home / "state" / "spawns",
            schedule_from_thread=schedule_from_thread,
            chain_on_complete=on_shell_job_complete,
        )
    if channel_registry is not None:
        # send_message fires SAGA mark_contributions when saga_client is
        # available; handles the credit pass at the actual reply boundary
        # rather than at end-of-turn (agent.py:_post_message_hook is the
        # fallback for non-send turns). When message_buffer is supplied, the
        # delivered text also writes to chat_history so the agent sees its
        # own prior replies in Recent activity.
        # ``home`` is threaded through so send_message can resolve
        # <send-file path="..."> directives against home/attachments/outbound.
        tools += build_channel_tools(
            channel_registry,
            saga_client=saga_client,
            message_buffer=message_buffer,
            home=home,
        )
    config = create_sdk_mcp_server(name="mimir", version="0.1.0", tools=tools)
    # chainlink #21: append embedded-XML-parameter hint to validation
    # errors. Wrap after create_sdk_mcp_server returns so we don't
    # touch the SDK's bookkeeping. McpSdkServerConfig is a TypedDict;
    # the live server lives under the ``instance`` key.
    instance: Any = None
    if isinstance(config, dict):
        instance = config.get("instance")
    else:  # pragma: no cover - guards against future dataclass migration
        instance = getattr(config, "instance", None)
    _install_xml_hint_wrapper(instance)
    return config


def allowed_tool_names(
    include_search: bool = True,
    include_saga: bool = True,
    include_scheduler: bool = True,
    include_channels: bool = True,
    include_turns: bool = True,
    include_shell: bool = True,
    include_spawn: bool = True,
) -> list[str]:
    """Names referenced in ``ClaudeAgentOptions.allowed_tools`` — both SDK
    preset names and ``mcp__mimir__*`` MCP tool names."""
    names = list(SDK_PRESET_TOOLS) + ["mcp__mimir__echo"]
    if include_search:
        names += search_tool_names()
    if include_turns:
        names += turn_tool_names()
    if include_saga:
        names += saga_tool_names()
    if include_scheduler:
        names += schedule_tool_names()
    if include_channels:
        names += channel_tool_names()
    if include_shell:
        names += shell_tool_names()
    if include_spawn:
        names += spawn_tool_names()
    return names
