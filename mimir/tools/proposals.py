"""Agent tools for change proposals to protected files (chainlink #337/#339/#344).

The agent cannot write live ``memory/core/*`` (the write guard blocks it at
runtime) or ``prompts/*`` (not a writable dir). To change either, it opens a
*proposal* — a throwaway ``git worktree`` under ``scratch/`` — edits the files
there with its normal Read/Edit/Write tools (add, edit, delete, move, any
number of files across both surfaces), then submits, which commits + pushes +
opens one PR. The operator reviews and merges on GitHub; the live files update
only after the merge. **Merge is the approval event.**

Reflection and other agent workflows use these for protected-surface
changes. ``state/proposed-changes.md`` is legacy/migration-only; protected
``memory/core/*`` and ``prompts/*`` changes should become proposal PRs through
this tool flow. Change proposals are deliberate, agent- or operator-initiated.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool

from ..models import AuthContext, PollerProposalState
from ..proposals import (
    PollerProposalScope,
    OpenResult,
    ProposalResult,
    poller_branch_name,
    poller_worktree_path,
    abandon_proposal as _abandon_proposal,
    finalize_proposal as _finalize_proposal,
    list_open_proposals,
    normalize_lane,
    open_proposal as _open_proposal,
)
from ..event_logger import log_event
from .refusals import ToolPolicyRefusal
from .repo import _bind_injected_runtime


def _home() -> Path | None:
    home_env = os.environ.get("MIMIR_HOME")
    return Path(home_env) if home_env else None


class ProposalSubmissionError(ToolException):
    """Typed failure signal for proposal submission in either lane."""

    def __init__(self, message: str, *, reason: str, lane: str):
        super().__init__(message)
        self.reason = reason
        self.lane = lane


def _poller_context(
    runtime: ToolRuntime[AuthContext] | None, lane: str, operation: str,
) -> AuthContext | None:
    from ..access_control import get_trusted_service_from_auth_context

    lane = (lane or "agent").strip().lower()
    context = runtime.context if isinstance(runtime, ToolRuntime) else None
    if runtime is not None and not isinstance(context, AuthContext):
        raise ToolPolicyRefusal("proposal rejected: invalid trusted runtime context")
    service = get_trusted_service_from_auth_context(context)
    if context is not None and context.is_service and service is None:
        raise ToolPolicyRefusal("proposal rejected: untrusted service context")
    if context is not None and service is None and (
        context.trigger == "poller" or (context.canonical_principal or "").startswith("poller:")
    ):
        raise ToolPolicyRefusal("proposal rejected: untrusted poller context")
    if service is not None and (service.trigger == "poller" or service.canonical.startswith("poller:")):
        if service.authority_profile != "research" or not service.has_capability(operation):
            raise ToolPolicyRefusal("proposal rejected: research poller capability required")
        if lane not in ("agent", "poller"):
            raise ToolPolicyRefusal("proposal rejected: pollers cannot switch proposal lanes")
        return context
    if lane == "poller":
        raise ToolPolicyRefusal("proposal rejected: trusted research poller runtime required")
    return None


def _run_poller(
    context: AuthContext, home: Path, operation: str, *,
    source: str = "", title: str = "", rationale: str = "",
) -> OpenResult | ProposalResult | bool:
    from .._context import get_current_turn
    from ..access_control import get_trusted_service_from_auth_context

    service = get_trusted_service_from_auth_context(context)
    state = context.poller_proposal_state
    if service is None or not isinstance(state, PollerProposalState):
        raise ToolPolicyRefusal("proposal rejected: invalid poller state")
    if not state._operation_lock.acquire(blocking=False):
        raise ToolPolicyRefusal("proposal rejected: another proposal operation is in progress")
    try:
        scope = state.scope
        if scope is not None and (
            not isinstance(scope, PollerProposalScope)
            or scope.owner != service.canonical
            or scope.origin_ref != context.origin_ref
            or state.worktree != poller_worktree_path(home, scope)
        ):
            raise ToolPolicyRefusal("proposal rejected: state ownership or worktree mismatch")
        if operation == "open_proposal":
            turn = get_current_turn()
            if turn is None or turn.auth_context is not context or not turn.turn_id:
                raise ToolPolicyRefusal("proposal rejected: exact runtime turn required")
            try:
                requested = PollerProposalScope(service.canonical, turn.turn_id, source, context.origin_ref)
            except (TypeError, ValueError) as exc:
                raise ToolPolicyRefusal(f"proposal rejected: {exc}") from exc
            if scope is not None and scope != requested:
                raise ToolPolicyRefusal("proposal rejected: existing scope/source cannot be overwritten")
            scope = requested
            result = _open_proposal(home, lane="poller", poller=scope)
            if result.ok or result.reason == "exists":
                expected = poller_worktree_path(home, scope)
                if (
                    result.worktree != expected or result.branch != poller_branch_name(scope)
                    or not expected.is_dir() or expected.resolve() != expected
                ):
                    raise ToolPolicyRefusal("proposal rejected: returned worktree does not match scope")
                state.scope = scope
                state.worktree = expected
                state.active = True
            return result
        if state.active is not True or scope is None:
            raise ProposalSubmissionError(
                f"{operation}: no `poller` proposal is open (no_open).", reason="no_open", lane="poller",
            )
        expected = poller_worktree_path(home, scope)
        if expected.resolve() != expected:
            raise ToolPolicyRefusal("proposal rejected: worktree path changed")
        try:
            if operation == "submit_proposal":
                return _finalize_proposal(home, title=title, rationale=rationale, lane="poller", poller=scope)
            return _abandon_proposal(home, lane="poller", poller=scope)
        finally:
            if not expected.exists() and not expected.is_symlink():
                state.deactivate()
    finally:
        state._operation_lock.release()


@tool
async def open_proposal(
    lane: str = "agent", source: str = "",
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
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
    can be open per lane (``abandon_proposal`` discards one). Supported lanes are
    ``agent`` (default) and ``upgrade`` (for version-triggered default syncs).

    Research pollers first write drafts in their own state directory, then use
    this tool for a wiki-only proposal. Never edit live state/wiki. Their lane
    is automatically poller, including when lane is omitted. Supply the paper
    ID or URL as source; it is untrusted attribution, not verified provenance.

    Args:
        lane: Proposal lane to open. Defaults to ``agent``.
        source: Paper ID or URL; required and nonblank for research pollers.

    Returns the path to edit, or an explanation if a proposal can't be opened.
    """
    context = _poller_context(runtime, lane, "open_proposal")
    try:
        lane = normalize_lane(lane)
    except ValueError as exc:
        return f"open_proposal failed ({exc})"
    home = _home()
    if home is None:
        return "open_proposal failed: MIMIR_HOME not set — surface to the operator."
    if context is not None:
        lane = "poller"
        result = await asyncio.to_thread(_run_poller, context, home, "open_proposal", source=source)
        if result.ok or result.reason == "exists":
            rel = result.worktree.relative_to(home.resolve())
            return (
                f"{'Opened' if result.ok else 'Already open'} `poller` wiki proposal `{result.branch}`.\n"
                f"Edit only `{rel}/state/wiki/`, never the live wiki. "
                "Call submit_proposal(title, rationale) for operator review and merge."
            )
    else:
        result = await asyncio.to_thread(_open_proposal, home, lane=lane)
    if result.ok and result.worktree is not None:
        rel = result.worktree.relative_to(home.resolve())
        return (
            f"Opened `{lane}` change proposal `{result.branch}`.\n"
            f"Edit the files under `{rel}/memory/core/` and/or `{rel}/prompts/` "
            f"with your normal file tools (add/edit/delete/move as needed) — this "
            f"is an isolated sandbox, the live files are untouched.\n"
            f"When done, call submit_proposal(title, rationale, lane={lane!r}) to open the PR "
            f"for the operator to review and merge."
        )
    if result.reason == "exists":
        rel = result.worktree.relative_to(home.resolve()) if result.worktree else "?"
        return (
            f"A `{lane}` change proposal is already open (`{result.branch}` at `{rel}`). "
            f"Edit it and submit, or call abandon_proposal(lane={lane!r}) first."
        )
    if result.reason == "no_remote":
        return (
            "open_proposal: the home repo has no git remote, so a PR can't be "
            "opened. memory/core and prompts are seeded at setup, not via PR, "
            "until a remote exists. Surface to the operator."
        )
    return f"open_proposal failed ({result.reason}): {result.detail or ''}"


@tool
async def submit_proposal(
    title: str, rationale: str, lane: str = "agent",
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Submit the open change proposal: open a PR for operator approval.

    Commits the changes you made under the proposal's ``memory/core/`` and
    ``prompts/`` (only those surfaces are included), pushes the branch, and
    opens one PR. Returns the PR URL — give it to the operator in the channel
    and ask them to review and merge; the change takes effect only after they
    merge.

    Research pollers submit only their own active wiki proposal, retaining the
    untrusted source supplied at open. Live wiki edits are never permitted.

    Args:
        title: Short PR title summarizing the change.
        rationale: Why the change is warranted — goes in the PR body and the
            commit message; this is what the operator reviews against.
        lane: Proposal lane to submit. Defaults to ``agent``.
    """
    context = _poller_context(runtime, lane, "submit_proposal")
    if context is not None:
        lane = "poller"
    try:
        lane = normalize_lane(lane)
    except ValueError as exc:
        return f"submit_proposal failed ({exc})"
    home = _home()
    if home is None:
        raise ProposalSubmissionError(
            "submit_proposal failed: MIMIR_HOME not set — surface to the operator.",
            reason="missing_home",
            lane=lane,
        )
    if not (title and rationale):
        raise ProposalSubmissionError(
            "submit_proposal failed: title and rationale are both required.",
            reason="invalid_arguments",
            lane=lane,
        )
    if context is not None:
        result = await asyncio.to_thread(
            _run_poller, context, home, "submit_proposal", title=title, rationale=rationale,
        )
    else:
        result = await asyncio.to_thread(
            _finalize_proposal, home, title=title, rationale=rationale, lane=lane
        )
    if result.ok and result.pr_url:
        # Positive feedback signal (chainlink #337/#339/#344): surfaces in the
        # prompt's feedback block and supersedes the open-proposal nudge (which
        # auto-clears now that the worktree is gone).
        await log_event("proposal_pr_opened", pr_url=result.pr_url, branch=result.branch, lane=lane)
        return (
            f"Opened a change-proposal PR: {result.pr_url}\n"
            "Give the operator this URL and ask them to review and merge. "
            "Nothing changed in the live files yet — it applies only after they "
            "merge."
        )
    if result.reason == "no_open":
        message = (
            f"submit_proposal: no `{lane}` proposal is open. Call open_proposal(lane={lane!r}) first, "
            "then edit the files."
        )
    elif result.reason == "no_changes":
        message = (
            "submit_proposal: you haven't changed anything under the proposal's "
            + ("state/wiki/" if context is not None else "memory/core/ or prompts/")
            + " yet — edit a file first, or call "
            "abandon_proposal."
        )
    elif result.reason == "secret":
        message = f"submit_proposal blocked: {result.detail}"
    elif result.reason == "pr_open" and result.pushed:
        message = (
            f"submit_proposal failed after pushing branch {result.branch}: "
            f"{result.detail or 'pull request was not opened'}"
        )
    else:
        message = f"submit_proposal failed ({result.reason}): {result.detail or ''}"
    raise ProposalSubmissionError(
        message,
        reason=result.reason or "unknown",
        lane=lane,
    )


@tool
async def abandon_proposal(
    lane: str = "agent",
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Discard the open change proposal without opening a PR.

    Removes the proposal's working copy and branch. Use this if you opened a
    proposal but decided not to propose the change after all.
    Research pollers can discard only their own active wiki proposal.

    Args:
        lane: Proposal lane to abandon. Defaults to ``agent``.
    """
    context = _poller_context(runtime, lane, "abandon_proposal")
    if context is not None:
        lane = "poller"
    try:
        lane = normalize_lane(lane)
    except ValueError as exc:
        return f"abandon_proposal failed ({exc})"
    home = _home()
    if home is None:
        return "abandon_proposal failed: MIMIR_HOME not set — surface to the operator."
    if context is not None:
        removed = await asyncio.to_thread(_run_poller, context, home, "abandon_proposal")
        branch = poller_branch_name(context.poller_proposal_state.scope)
        return (f"Abandoned `poller` change proposal `{branch}`." if removed else
                "abandon_proposal: nothing to abandon (no `poller` proposal open).")
    open_now = await asyncio.to_thread(list_open_proposals, home, lane=lane)
    removed = await asyncio.to_thread(_abandon_proposal, home, lane=lane)
    if removed:
        branch = open_now[0][0] if open_now else "?"
        return f"Abandoned `{lane}` change proposal `{branch}`."
    return f"abandon_proposal: nothing to abandon (no `{lane}` proposal open)."


for _proposal_tool in (open_proposal, submit_proposal, abandon_proposal):
    _bind_injected_runtime(_proposal_tool)


__all__ = (
    "open_proposal",
    "submit_proposal",
    "ProposalSubmissionError",
    "abandon_proposal",
)
