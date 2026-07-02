"""Best-effort regex screen for prohibited bash patterns.

Supplements the text-based policy in memory/core/06-action-boundaries.md
with a substring/regex check on the bash tool's ``command`` argument
*before* it reaches the shell. This is not a sandbox and not a
security boundary — a determined caller wraps the command in a script
file, base64-decodes a payload, uses git aliases, sets a different
working directory, or simply renames the binary, and the regex
doesn't see it. Treat it as a guardrail against accidents and a
deterrent against trivial bypass attempts, not as enforcement.

Why it's still useful:
- Most accidents look like the obvious form (``git push --force main``
  typed directly), and screening those out closes off the cheap
  failure mode.
- The block message points the agent at the policy doc, which
  surfaces the prohibition in the conversation's context.
- New operators reading the codebase see one concrete reference
  point for "what mimir won't let the agent do."

If you need an actual security boundary, sandbox the agent process
(seccomp/AppArmor/container caps) — not this module.

Currently screened:
- git push --force / --force-with-lease / -f to main or master
- references to compose.env (operator-managed, must not be touched
  from inside the agent process)

Wired into BudgetGateMiddleware, so the screen applies to all
bash/shell tool calls (shell_exec, bash_async, bash_exec, execute, Bash)
regardless of provider.
"""

from __future__ import annotations

import re
from typing import NamedTuple


_BASH_TOOL_NAMES: frozenset[str] = frozenset({
    # claude-code's native shell built-in surfaces as "Bash" (capital B)
    # when registered through deepagents. Must be in this set or the guard
    # short-circuits and the call is forwarded to the handler unchecked.
    "Bash",
    "shell_exec",
    "bash_async",
    "bash_exec",
    "execute",
    "aexecute",
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
    # Block bash reads/writes of ``compose.env``. It's operator-managed and
    # holds real secrets (API keys, tokens) plus the agent's own runtime
    # config (model, flags) — editing it from the in-container shell is both a
    # secret-exposure and a self-modification vector, so it's off-limits. The
    # operator edits compose.env from the host, which doesn't go through this
    # guard.
    #
    # Pattern matches any reference to ``compose.env`` in the command. Coarse
    # on purpose: ``cat compose.env`` (reading) gets blocked too, but the
    # agent has no legitimate reason to read it (no operator secrets are kept
    # anywhere the agent needs to look up). False positives are acceptable;
    # false negatives are not.
    _Prohibition(
        re.compile(r"\bcompose\.env\b"),
        "compose.env reference (operator-managed; agent must not read or write)",
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
                f"§Git/repo. Refused by mimir's destructive-action "
                f"guardrail (an accident deterrent — see "
                f"prohibited_action_guard.py). Don't work around it; if "
                f"you believe this is an error, escalate to the operator."
            )
    return None


def is_bash_tool(tool_name: str) -> bool:
    """True if tool_name is one of the tracked bash/shell execution tools."""
    if tool_name in _BASH_TOOL_NAMES:
        return True
    # Claude Code / MCP bridge names are not stable across adapters:
    # ``mcp__mimir__shell_exec`` and ``mcp_mimir_shell_exec`` have both
    # appeared. Match the final component so a normalized MCP shell alias
    # still gets screened before execution.
    return any(
        tool_name.endswith(f"__{name}") or tool_name.endswith(f"_{name}")
        for name in (
            "shell_exec",
            "bash_async",
            "bash_exec",
            "execute",
            "aexecute",
            "Bash",
            "bash",
        )
    )
