"""Production tool surface for the deepagents-backed mimir Agent.

Each tool is a langchain ``@tool``; module-level setters inject the
dependencies the tool needs (Indexer for file_search, Dispatcher for
send_message, ChannelRegistry, Scheduler, CommitmentsStore, spawn
config). ``server.py:build_app`` calls each setter once after
constructing the corresponding dependency; the Agent then passes
``all_mimir_tools()`` to ``deepagents.create_deep_agent``.

The tools split across four modules for readability, but the public
API is the flat ``mimir.tools`` namespace.
"""

from .memory import memory_query, set_memory_client
from .store import memory_store
from .extra import (
    file_search,
    mimir_get_turn,
    set_indexer,
    set_shell_allowlist,
    set_turns_log_path,
    shell_exec,
)
from .registry import (
    add_schedule,
    all_mimir_tools,
    commitment_complete,
    commitment_dismiss,
    commitment_list,
    commitment_snooze,
    fetch_channel_history,
    list_schedules,
    react,
    reload_pollers,
    remove_schedule,
    send_message,
    set_channel_registry,
    set_commitments_store,
    set_current_channel_id,
    set_dispatcher,
    set_scheduler,
    set_spawn_config,
    spawn_claude_code,
)

__all__ = [
    # Core tools (callable by the agent)
    "memory_query",
    "memory_store",
    "file_search",
    "mimir_get_turn",
    "shell_exec",
    "send_message",
    "react",
    "fetch_channel_history",
    "list_schedules",
    "add_schedule",
    "remove_schedule",
    "reload_pollers",
    "commitment_complete",
    "commitment_snooze",
    "commitment_dismiss",
    "commitment_list",
    "spawn_claude_code",
    # Dep-injection setters (called by server.py:build_app)
    "set_memory_client",
    "set_indexer",
    "set_turns_log_path",
    "set_shell_allowlist",
    "set_channel_registry",
    "set_dispatcher",
    "set_scheduler",
    "set_commitments_store",
    "set_spawn_config",
    "set_current_channel_id",
    # Aggregate
    "all_mimir_tools",
]
