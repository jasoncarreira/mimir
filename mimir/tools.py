"""Tool registration — post-cutover stub.

Pre-cutover this module built an in-process MCP server via
``create_sdk_mcp_server`` aggregating tools from sagatools /
searchtools / turntools / scheduletools / shelltools / channeltools /
committools / spawn. Post-cutover, deepagents takes its tool list
directly via ``create_deep_agent(..., tools=[...])`` from
mimir/deepagent_poc/all_tools.py.

This module is retained as a stub for legacy callers that import
``build_mcp_server`` / ``allowed_tool_names`` — both return empty.
"""
from __future__ import annotations

from typing import Any


def build_mcp_server(*args, **kwargs) -> Any:
    """No-op post-cutover; deepagents handles tool registration."""
    return None


def allowed_tool_names(*args, **kwargs) -> list[str]:
    """The full list of agent-callable tool names. Pre-cutover this
    aggregated from the build_*_tools modules; post-cutover, callers
    that want the actual tool list should import from
    mimir.deepagent_poc.all_tools.
    """
    return [
        # Memory
        "memory_query", "memory_store",
        # Indexer
        "file_search",
        # Turn history
        "mimir_get_turn",
        # Shell
        "shell_exec",
        # Channels
        "send_message", "react", "fetch_channel_history",
        # Scheduler
        "list_schedules", "add_schedule", "remove_schedule", "reload_pollers",
        # Commitments
        "commitment_complete", "commitment_snooze",
        "commitment_dismiss", "commitment_list",
        # Spawn
        "spawn_claude_code",
    ]
