"""Code-level enforcement for the 'prohibited' zone of mimir's action boundaries.

Supplements the text-based policy in memory/core/06-action-boundaries.md
with harness-level interception for the most consequential bash patterns.
Not comprehensive (full enforcement requires an LLM), but provides
verifiable code-level protection for the hardest prohibitions and gives
new operators a concrete reference point.

Currently enforced:
- git push --force / --force-with-lease / -f to main or master

Wired into BudgetGateMiddleware, so it applies to all bash/shell tool
calls (shell_exec, bash_async, bash_exec) regardless of provider.
"""

from __future__ import annotations

import re
from typing import NamedTuple


_BASH_TOOL_NAMES: frozenset[str] = frozenset({
    "shell_exec",
    "bash_async",
    "bash_exec",
    "mcp__mimir__shell_exec",
    "mcp__mimir__bash_async",
})


class _Prohibition(NamedTuple):
    pattern: re.Pattern[str]
    message: str


# Each pattern is anchored to detect the git push command with a
# force flag targeting main or master. All three arg orderings are
# covered: --force ... branch, branch ... --force, and -f variants.
_PROHIBITIONS: list[_Prohibition] = [
    _Prohibition(
        re.compile(
            r"\bgit\s+push\b.*?--force(?:-with-lease)?\b.*?\b(?:main|master)\b",
            re.I | re.DOTALL,
        ),
        "git push --force[--with-lease] to main/master",
    ),
    _Prohibition(
        re.compile(
            r"\bgit\s+push\b.*?-f\b.*?\b(?:main|master)\b",
            re.I | re.DOTALL,
        ),
        "git push -f to main/master",
    ),
    _Prohibition(
        re.compile(
            r"\bgit\s+push\b.*?\b(?:main|master)\b.*?(?:--force(?:-with-lease)?|-f)\b",
            re.I | re.DOTALL,
        ),
        "git push --force[--with-lease]/-f to main/master (reversed-arg form)",
    ),
]

_BLOCK_PREFIX = "PROHIBITED_ACTION"


def check_prohibited_bash(command: str) -> str | None:
    """Return an error string if `command` matches a prohibited pattern.

    Returns None if no prohibited pattern matches (command is allowed).
    The returned string is suitable for a ToolMessage content field.
    """
    for prohibition in _PROHIBITIONS:
        if prohibition.pattern.search(command):
            return (
                f"{_BLOCK_PREFIX}: {prohibition.message} is in the "
                f"'prohibited' zone per memory/core/06-action-boundaries.md "
                f"§Git/repo. This action is blocked at the harness level. "
                f"If you believe this is an error, escalate to the operator."
            )
    return None


def is_bash_tool(tool_name: str) -> bool:
    """True if tool_name is one of the tracked bash/shell execution tools."""
    return tool_name in _BASH_TOOL_NAMES
