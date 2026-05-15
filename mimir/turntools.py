"""SDK tools stub — post-cutover. ``mimir_get_turn`` migrated to
mimir/deepagent_poc/extra_tools.py."""
from __future__ import annotations


def build_turn_tools(*args, **kwargs):
    return []


def turn_tool_names() -> list[str]:
    return ["mimir_get_turn"]
