"""MCP tool registry bridge.

``mimir.mcp_client.MCPManager.start_servers`` returns a list of
``StructuredTool`` instances bridged from remote MCP servers. They
arrive after ``server.py:_on_startup`` initializes the manager — but
the agent's tool list is assembled lazily in ``Agent._build_agent_if_needed``.

This module is the handoff: ``server.py`` calls ``set_mcp_tools`` once
MCP servers are up; ``registry.all_mimir_tools`` calls ``get_mcp_tools``
when assembling the agent surface. Empty list when MCP is unconfigured
or every server failed to start — never blocks the agent.

Module-level state is process-scoped + idempotent, matching the
pattern used for ``set_indexer``, ``set_dispatcher``, etc.
"""

from __future__ import annotations

from typing import Any

_mcp_tools: list[Any] = []


def set_mcp_tools(tools: list[Any]) -> None:
    """Replace the cached MCP tool list. Called once per startup."""
    global _mcp_tools
    _mcp_tools = list(tools)


def get_mcp_tools() -> list[Any]:
    """Return the currently registered MCP tools (empty if none)."""
    return list(_mcp_tools)


def clear_mcp_tools() -> None:
    """Reset the registry. Used by tests; harmless otherwise."""
    global _mcp_tools
    _mcp_tools = []
    from mimir.mcp_client import clear_provenance_registry
    clear_provenance_registry()
