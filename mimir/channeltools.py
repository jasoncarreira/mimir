"""SDK tools stub — post-cutover. Tools migrated to
mimir/deepagent_poc/all_tools.py."""
from __future__ import annotations


def build_channel_tools(*args, **kwargs):
    return []


def channel_tool_names() -> list[str]:
    return ["send_message", "react", "fetch_channel_history"]
