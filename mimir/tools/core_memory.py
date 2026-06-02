"""Agent tools for core-memory change proposals (chainlink #337/#339).

The agent cannot write live ``memory/core/*`` (the write guard blocks it). To
change core memory it opens a *proposal* — a throwaway ``git worktree`` under
``scratch/`` — edits the core files there with its normal Read/Edit/Write
tools (add, edit, delete, move, any number of files), then submits, which
commits + pushes + opens a PR. The operator reviews and merges on GitHub; live
core memory updates only after the merge. **Merge is the approval event.**

Reflection does NOT use these — it stays in the suggestion lane
(``state/proposed-changes.md``). Core-memory proposals are deliberate,
agent- or operator-initiated.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain_core.tools import tool

from ..core_memory_pr import (
    abandon_proposal,
    finalize_proposal,
    list_open_proposals,
    open_proposal,
)


def _home() -> Path | None:
    home_env = os.environ.get("MIMIR_HOME")
    return Path(home_env) if home_env else None


@tool
async def open_core_memory_proposal() -> str:
    """Open a proposal to change core memory (memory/core/*).

    Core memory (identity, non-goals, action boundaries, learned behaviors,
    filing rules, …) is protected — you CANNOT edit memory/core/* in place.
    Call this to start a change. It creates an isolated working copy (a git
    worktree under scratch/) and returns the path. Then edit the core files
    under ``<path>/memory/core/`` with your normal file tools — add, change,
    delete, or move files freely; it's a sandbox, nothing is live yet. When
    you're done, call ``submit_core_memory_proposal`` to open a PR for the
    operator to review. Only one proposal can be open at a time
    (``abandon_core_memory_proposal`` discards one).

    Returns the path to edit, or an explanation if a proposal can't be opened.
    """
    home = _home()
    if home is None:
        return "open_core_memory_proposal failed: MIMIR_HOME not set — surface to the operator."
    result = await asyncio.to_thread(open_proposal, home)
    if result.ok and result.worktree is not None:
        rel = result.worktree.relative_to(home.resolve())
        return (
            f"Opened core-memory proposal `{result.branch}`.\n"
            f"Edit the core files under `{rel}/memory/core/` with your normal "
            f"file tools (add/edit/delete/move as needed) — this is an isolated "
            f"sandbox, live core memory is untouched.\n"
            f"When done, call submit_core_memory_proposal(title, rationale) to "
            f"open the PR for the operator to review and merge."
        )
    if result.reason == "exists":
        rel = result.worktree.relative_to(home.resolve()) if result.worktree else "?"
        return (
            f"A core-memory proposal is already open (`{result.branch}` at "
            f"`{rel}`). Edit it and submit, or call "
            f"abandon_core_memory_proposal first."
        )
    if result.reason == "no_remote":
        return (
            "open_core_memory_proposal: the home repo has no git remote, so a "
            "PR can't be opened. Core memory is seeded at setup, not via PR, "
            "until a remote exists. Surface to the operator."
        )
    return f"open_core_memory_proposal failed ({result.reason}): {result.detail or ''}"


@tool
async def submit_core_memory_proposal(title: str, rationale: str) -> str:
    """Submit the open core-memory proposal: open a PR for operator approval.

    Commits the changes you made under the proposal's ``memory/core/`` (only
    core files are included), pushes the branch, and opens a PR. Returns the
    PR URL — give it to the operator in the channel and ask them to review and
    merge; the change takes effect in live core memory only after they merge.

    Args:
        title: Short PR title summarizing the change.
        rationale: Why the change is warranted — goes in the PR body and the
            commit message; this is what the operator reviews against.
    """
    home = _home()
    if home is None:
        return "submit_core_memory_proposal failed: MIMIR_HOME not set — surface to the operator."
    if not (title and rationale):
        return "submit_core_memory_proposal failed: title and rationale are both required."
    result = await asyncio.to_thread(
        finalize_proposal, home, title=title, rationale=rationale
    )
    if result.ok and result.pr_url:
        return (
            f"Opened a core-memory PR: {result.pr_url}\n"
            "Give the operator this URL and ask them to review and merge. "
            "Nothing changed in live core memory yet — it applies only after "
            "they merge."
        )
    if result.ok:  # pushed but no PR opened (gh unavailable/failed)
        return (
            f"Pushed branch {result.branch}, but couldn't open the PR "
            f"automatically ({result.detail}). Ask the operator to open a PR "
            f"from that branch."
        )
    if result.reason == "no_open":
        return (
            "submit_core_memory_proposal: no proposal is open. Call "
            "open_core_memory_proposal first, then edit the core files."
        )
    if result.reason == "no_changes":
        return (
            "submit_core_memory_proposal: you haven't changed anything under "
            "the proposal's memory/core/ yet — edit a file first, or call "
            "abandon_core_memory_proposal."
        )
    if result.reason == "secret":
        return f"submit_core_memory_proposal blocked: {result.detail}"
    return f"submit_core_memory_proposal failed ({result.reason}): {result.detail or ''}"


@tool
async def abandon_core_memory_proposal() -> str:
    """Discard the open core-memory proposal without opening a PR.

    Removes the proposal's working copy and branch. Use this if you opened a
    proposal but decided not to propose the change after all.
    """
    home = _home()
    if home is None:
        return "abandon_core_memory_proposal failed: MIMIR_HOME not set — surface to the operator."
    open_now = await asyncio.to_thread(list_open_proposals, home)
    removed = await asyncio.to_thread(abandon_proposal, home)
    if removed:
        branch = open_now[0][0] if open_now else "?"
        return f"Abandoned core-memory proposal `{branch}`."
    return "abandon_core_memory_proposal: nothing to abandon (no proposal open)."


__all__ = (
    "open_core_memory_proposal",
    "submit_core_memory_proposal",
    "abandon_core_memory_proposal",
)
