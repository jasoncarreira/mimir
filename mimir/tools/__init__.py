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

from .memory import memory_get, memory_query, set_memory_client
from .chainlink import chainlink_issue_show
from .store import memory_store
from .memory_propose import memory_propose
from .operator_alert import operator_alert, set_operator_alert_dependencies
from .extra import (
    file_search,
    get_turn,
    mimir_get_turn,
    rebuild_index,
    set_index_generator,
    set_indexer,
    set_turns_log_path,
    shell_exec,
)
from .registry import (
    add_schedule,
    all_mimir_tools,
    approve_declassification,
    commitment_complete,
    commitment_propose_closure,
    commitment_dismiss,
    commitment_list,
    commitment_snooze,
    fetch_channel_history,
    list_channels,
    list_schedules,
    react,
    request_operator_approval,
    reload_pollers,
    remove_schedule,
    send_message,
    set_poller_overrides,
    set_schedule_priority,
    set_schedule_cron,
    set_channel_registry,
    set_commitments_store,
    set_current_channel_id,
    set_dispatcher,
    set_identity_resolver,
    set_scheduler,
    set_spawn_config,
    set_arbiter,
)
from .web import (
    fetch_url,
    set_home as set_web_home,
    web_search,
    web_tools_enabled,
)
from .mcp import (
    clear_mcp_tools,
    get_mcp_tools,
    set_mcp_tools,
)
from .shell_async import (
    bash_async,
    bash_job_output,
    bash_jobs_list,
    set_shell_job_registry,
)
from .saga_ops import (
    saga_end_session,
    saga_feedback,
    saga_forget,
    saga_mark_contributions,
    saga_record_skill_learning,
)
from .proposals import proposal_diff

__all__ = [
    # Core tools (callable by the agent)
    "memory_query",
    "chainlink_issue_show",
    "memory_get",
    "memory_store",
    "memory_propose",
    "file_search",
    "rebuild_index",
    "mimir_get_turn",
    "get_turn",
    "shell_exec",
    "send_message",
    "operator_alert",
    "react",
    "fetch_channel_history",
    "list_channels",
    "list_schedules",
    "add_schedule",
    "approve_declassification",
    "request_operator_approval",
    "set_schedule_priority",
    "set_schedule_cron",
    "remove_schedule",
    "set_poller_overrides",
    "reload_pollers",
    "commitment_complete",
    "commitment_propose_closure",
    "commitment_snooze",
    "commitment_dismiss",
    "commitment_list",
    # Web tools (gated on provider; see web_tools_enabled)
    "web_search",
    "fetch_url",
    "web_tools_enabled",
    "set_web_home",
    # MCP tool bridge (populated at server startup)
    "set_mcp_tools",
    "get_mcp_tools",
    "clear_mcp_tools",
    # Async-shell tools (long-running jobs, wake-up via shell_job_complete)
    "bash_async",
    "bash_jobs_list",
    "bash_job_output",
    "set_shell_job_registry",
    # SAGA agent-callable ops (read+write covered by memory_query/store)
    "saga_feedback",
    "saga_mark_contributions",
    "saga_end_session",
    "saga_forget",
    "saga_record_skill_learning",
    "proposal_diff",
    # Dep-injection setters (called by server.py:build_app)
    "set_memory_client",
    "set_indexer",
    "set_index_generator",
    "set_turns_log_path",
    "set_channel_registry",
    "set_operator_alert_dependencies",
    "set_identity_resolver",
    "set_dispatcher",
    "set_scheduler",
    "set_commitments_store",
    "set_spawn_config",
    "set_current_channel_id",
    "set_arbiter",
    # Aggregate
    "all_mimir_tools",
]
