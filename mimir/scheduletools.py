"""SDK tools stub — post-cutover. Tools migrated to
mimir/deepagent_poc/all_tools.py."""
from __future__ import annotations


def build_schedule_tools(*args, **kwargs):
    return []


def schedule_tool_names() -> list[str]:
    return ["list_schedules", "add_schedule", "remove_schedule", "reload_pollers"]
