"""SDK tools stub — post-cutover. ``spawn_claude_code`` migrated to
mimir/deepagent_poc/all_tools.py. Subagent shape now relies on
deepagents' built-in ``task`` tool for context quarantine."""
from __future__ import annotations


def build_spawn_tool(*args, **kwargs):
    return []


def spawn_tool_names() -> list[str]:
    return ["spawn_claude_code"]
