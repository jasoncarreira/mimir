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
    shell_jobs: ShellJobRegistry | None = None,
    on_shell_job_complete: Any | None = None,
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
    return create_sdk_mcp_server(name="mimir", version="0.1.0", tools=tools)


def allowed_tool_names(
    include_search: bool = True,
    include_saga: bool = True,
    include_scheduler: bool = True,
    include_channels: bool = True,
    include_turns: bool = True,
    include_shell: bool = True,
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
    return names
