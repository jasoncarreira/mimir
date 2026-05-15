"""SDK tools stub — post-cutover. Tools migrated to
mimir/deepagent_poc/all_tools.py."""
from __future__ import annotations


def build_commitment_tools(*args, **kwargs):
    return []


def commitment_tool_names() -> list[str]:
    return [
        "commitment_complete", "commitment_snooze",
        "commitment_dismiss", "commitment_list",
    ]
