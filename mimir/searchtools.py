"""SDK tools stub — post-cutover (2026-05-14).

Tool definitions migrated to mimir/deepagent_poc/extra_tools.py
(``file_search``) using the langchain_core @tool decorator. This
module is kept as a no-op for legacy import paths.
"""
from __future__ import annotations


def build_search_tools(*args, **kwargs):
    return []


def search_tool_names() -> list[str]:
    return ["file_search", "rebuild_index"]
