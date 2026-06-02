"""Agent tools for change proposals to protected files (chainlink #337/#339/#344).

The agent cannot write live ``memory/core/*`` (the write guard blocks it at
runtime) or ``prompts/*`` (not a writable dir). To change either, it opens a
*proposal* — a throwaway ``git worktree`` under ``scratch/`` — edits the files
there with its normal Read/Edit/Write tools (add, edit, delete, move, any
number of files across both surfaces), then submits, which commits + pushes +
opens one PR. The operator reviews and merges on GitHub; the live files update
only after the merge. **Merge is the approval event.**

Reflection does NOT use these — it stays in the suggestion lane
(``state/proposed-changes.md``). Change proposals are deliberate, agent- or
operator-initiated.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain_core.tools import tool

from ..proposals import (
    abandon_proposal as _abandon_proposal,
    finalize_proposal as _finalize_proposal,
    list_open_proposals,
    open_proposal as _open_proposal,
)
from ..event_logger import log_event


def _home() -> Path | None:
    home_env = os.environ.get("MIMIR_HOME")
    return Path(home_env) if home_env else None


@tool
async def open_proposal() -> str:
    """Open a proposal to change protected files (memory/core/* and prompts/*).

    Core memory (identity, non-goals, action boundaries, learned behaviors,
    filing rules, …) and the prompt templates under prompts/ are protected —
    you CANNOT edit memory/core/* or prompts/* in place. Call this to start a
    change. It creates an isolated working copy (a git worktree under scratch/)
    and returns the path. Then edit the files under ``<path>/memory/core/`` and
    ``<path>/prompts/`` with your normal file tools — add, change, delete, or
    move files freely across both; it's a sandbox, nothing is live yet. One
    proposal can touch both surfaces and becomes one PR. When you're done, call
    ``submit_proposal`` to open it for the operator to review. Only one proposal
    can be open at a time (``abandon_proposal`` discards one).

    Returns the path to edit, or an explanation if a proposal can't be opened.
    """
    home = _home()
    if home is None:
        return "open_proposal failed: MIMIR_HOME not set — surface to the operator."
    result = await asyncio.to_thread(_open_proposal, home)
    if result.ok and result.worktree is not None:
        rel = result.worktree.relative_to(home.resolve())
        return (
            f"Opened change proposal `{result.branch}`.\n"
            f"Edit the files under `{rel}/memory/core/` and/or `{rel}/prompts/` "
            f"with your normal file tools (add/edit/delete/move as needed) — this "
            f"is an isolated sandbox, the live files are untouched.\n"
            f"When done, call submit_proposal(title, rationale) to open the PR "
            f"for the operator to review and merge."
        )
    if result.reason == "exists":
        rel = result.worktree.relative_to(home.resolve()) if result.worktree else "?"
        return (
            f"A change proposal is already open (`{result.branch}` at `{rel}`). "
            f"Edit it and submit, or call abandon_proposal first."
        )
    if result.reason == "no_remote":
        return (
            "open_proposal: the home repo has no git remote, so a PR can't be "
            "opened. memory/core and prompts are seeded at setup, not via PR, "
            "until a remote exists. Surface to the operator."
        )
    return f"open_proposal failed ({result.reason}): {result.detail or ''}"


@tool
async def submit_proposal(title: str, rationale: str) -> str:
    """Submit the open change proposal: open a PR for operator approval.

    Commits the changes you made under the proposal's ``memory/core/`` and
    ``prompts/`` (only those surfaces are included), pushes the branch, and
    opens one PR. Returns the PR URL — give it to the operator in the channel
    and ask them to review and merge; the change takes effect only after they
    merge.

    Args:
        title: Short PR title summarizing the change.
        rationale: Why the change is warranted — goes in the PR body and the
            commit message; this is what the operator reviews against.
    """
    home = _home()
    if home is None:
        return "submit_proposal failed: MIMIR_HOME not set — surface to the operator."
    if not (title and rationale):
        return "submit_proposal failed: title and rationale are both required."
    result = await asyncio.to_thread(
        _finalize_proposal, home, title=title, rationale=rationale
    )
    if result.ok and result.pr_url:
        # Positive feedback signal (chainlink #337/#339/#344): surfaces in the
        # prompt's feedback block and supersedes the open-proposal nudge (which
        # auto-clears now that the worktree is gone).
        await log_event("proposal_pr_opened", pr_url=result.pr_url, branch=result.branch)
        return (
            f"Opened a change-proposal PR: {result.pr_url}\n"
            "Give the operator this URL and ask them to review and merge. "
            "Nothing changed in the live files yet — it applies only after they "
            "merge."
        )
    if result.ok:  # pushed but no PR opened (gh unavailable/failed)
        return (
            f"Pushed branch {result.branch}, but couldn't open the PR "
            f"automatically ({result.detail}). Ask the operator to open a PR "
            f"from that branch."
        )
    if result.reason == "no_open":
        return (
            "submit_proposal: no proposal is open. Call open_proposal first, "
            "then edit the files."
        )
    if result.reason == "no_changes":
        return (
            "submit_proposal: you haven't changed anything under the proposal's "
            "memory/core/ or prompts/ yet — edit a file first, or call "
            "abandon_proposal."
        )
    if result.reason == "secret":
        return f"submit_proposal blocked: {result.detail}"
    return f"submit_proposal failed ({result.reason}): {result.detail or ''}"


@tool
async def abandon_proposal() -> str:
    """Discard the open change proposal without opening a PR.

    Removes the proposal's working copy and branch. Use this if you opened a
    proposal but decided not to propose the change after all.
    """
    home = _home()
    if home is None:
        return "abandon_proposal failed: MIMIR_HOME not set — surface to the operator."
    open_now = await asyncio.to_thread(list_open_proposals, home)
    removed = await asyncio.to_thread(_abandon_proposal, home)
    if removed:
        branch = open_now[0][0] if open_now else "?"
        return f"Abandoned change proposal `{branch}`."
    return "abandon_proposal: nothing to abandon (no proposal open)."


__all__ = (
    "open_proposal",
    "submit_proposal",
    "abandon_proposal",
)
