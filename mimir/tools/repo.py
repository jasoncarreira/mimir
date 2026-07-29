"""Model-facing wrappers for the closed, scope-bound Git API."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function

from ..models import AuthContext, RepoPRActionScope, RepoReviewState
from ..pr_checkout_lease import cleanup_pr_checkout_lease, create_pr_checkout_lease
from ..repo_tools import (
    GitCommit,
    GitDiff,
    GitFetch,
    GitMerge,
    GitMergeAbort,
    GitPush,
    GitRebase,
    GitRebaseAbort,
    GitRefusal,
    GitRevert,
    GitRevertAbort,
    GitStage,
    GitStatus,
    GitUnmerged,
    RepoGitTools,
)


def _state(runtime: ToolRuntime[AuthContext] | None) -> RepoReviewState:
    context = getattr(runtime, "context", None)
    state = getattr(context, "repo_review_state", None)
    scope = getattr(context, "repo_pr_action_scope", None)
    if (
        not isinstance(state, RepoReviewState)
        or not isinstance(scope, RepoPRActionScope)
        or state.action_scope.scope_id != scope.scope_id
    ):
        raise ToolException("repository operation rejected: no immutable review scope")
    return state


def _execute(runtime: ToolRuntime[AuthContext] | None, operation: Any) -> dict[str, Any]:
    try:
        return asdict(RepoGitTools(_state(runtime)).execute(operation))
    except (GitRefusal, RuntimeError, ValueError) as exc:
        code = getattr(exc, "code", "repository_operation_failed")
        raise ToolException(f"repository operation rejected ({code}): {exc}") from exc


@tool
def repo_checkout(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Create the exact checkout lease bound to this turn's immutable PR scope."""
    state = _state(runtime)
    try:
        lease = create_pr_checkout_lease(
            state.action_scope,
            owner=state.action_scope.principal,
            review_state=state,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolException(f"repository checkout rejected: {exc}") from exc
    return {
        "status": "checked_out",
        "path": str(lease.path),
        "scope_id": lease.scope_id,
        "head_sha": lease.head_sha,
    }


@tool
def repo_cleanup(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Revoke and remove this turn's exact active checkout lease."""
    state = _state(runtime)
    lease = state.checkout_lease
    if lease is None:
        raise ToolException("repository cleanup rejected: no active checkout lease")
    try:
        removed = cleanup_pr_checkout_lease(lease, review_state=state)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolException(f"repository cleanup rejected: {exc}") from exc
    return {"status": "cleaned", "removed": removed, "scope_id": state.action_scope.scope_id}


@tool
def repo_fetch(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Fetch only the immutable head and base refs bound to this turn."""
    return _execute(runtime, GitFetch())


@tool
def repo_status(
    include_untracked: bool = True,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read porcelain status from the active bound checkout."""
    return _execute(runtime, GitStatus(include_untracked))


@tool
def repo_diff(
    mode: Literal["working", "staged", "base"] = "working",
    paths: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read a bounded working, staged, or base diff from the bound checkout."""
    return _execute(runtime, GitDiff(mode, paths))


@tool
def repo_unmerged(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Read unmerged-index records from the bound checkout."""
    return _execute(runtime, GitUnmerged())


@tool
def repo_stage(
    paths: tuple[str, ...],
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage only explicit repository-relative paths in the bound checkout."""
    return _execute(runtime, GitStage(paths))


@tool
def repo_commit(
    paths: tuple[str, ...],
    message: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage explicit paths and commit them with server-owned Git identity."""
    return _execute(runtime, GitCommit(paths, message))


@tool
def repo_merge(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Merge the immutable observed base commit into the bound checkout."""
    return _execute(runtime, GitMerge())


@tool
def repo_merge_abort(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Abort an in-progress merge in the bound checkout."""
    return _execute(runtime, GitMergeAbort())


@tool
def repo_rebase(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Rebase the bound head onto the immutable observed base commit."""
    return _execute(runtime, GitRebase())


@tool
def repo_rebase_abort(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Abort an in-progress rebase in the bound checkout."""
    return _execute(runtime, GitRebaseAbort())


@tool
def repo_revert(
    commit: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Revert one full commit ID proven to be within bound head minus base."""
    return _execute(runtime, GitRevert(commit))


@tool
def repo_revert_abort(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Abort an in-progress revert in the bound checkout."""
    return _execute(runtime, GitRevertAbort())


@tool
def repo_push(runtime: ToolRuntime[AuthContext] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Push HEAD to the one destination ref in the immutable PR scope."""
    return _execute(runtime, GitPush())


def _bind_injected_runtime(repo_tool: StructuredTool) -> StructuredTool:
    if repo_tool.func is None:
        raise RuntimeError(f"repository tool {repo_tool.name!r} has no sync callable")
    repo_tool.func.__annotations__["runtime"] = ToolRuntime
    repo_tool.args_schema = create_schema_from_function(
        repo_tool.name, repo_tool.func, filter_args=(), include_injected=True,
    )
    repo_tool.__dict__.pop("_injected_args_keys", None)
    return repo_tool


REPO_TOOLS = tuple(_bind_injected_runtime(repo_tool) for repo_tool in (
    repo_checkout,
    repo_cleanup,
    repo_fetch,
    repo_status,
    repo_diff,
    repo_unmerged,
    repo_stage,
    repo_commit,
    repo_merge,
    repo_merge_abort,
    repo_rebase,
    repo_rebase_abort,
    repo_revert,
    repo_revert_abort,
    repo_push,
))
