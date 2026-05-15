"""SDK hooks stub — post-cutover.

Pre-cutover this module wrapped Read/Write/Edit/Bash/Glob SDK-preset
tools with HookContext-based gates (path scoping, command allowlists,
post-write FAISS-reindex). Post-cutover, deepagents has built-in
filesystem tools (ls/read_file/write_file/edit_file/glob/grep) and a
FilesystemPermission middleware that subsumes the same surface.

This module is retained as a no-op stub for legacy import paths.
The middleware wiring lives in mimir/agent.py:_build_agent_if_needed.
"""
from __future__ import annotations

from typing import Any


def build_hook_matchers(*args, **kwargs) -> list:
    """Pre-cutover this returned HookMatcher chains for SDK presets.
    Post-cutover, deepagents handles permissions via
    FilesystemPermission middleware (set in agent.py)."""
    return []


# Legacy class names referenced by tests — provide no-op stubs.
class SubagentLifecycleHook:
    def __init__(self, *args, **kwargs):
        pass


class CancelTypingHook:
    def __init__(self, *args, **kwargs):
        pass
