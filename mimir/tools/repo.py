"""Model-facing wrappers for the closed, scope-bound Git API."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function

from ..models import AuthContext, RepoReviewState
from ..pr_checkout_lease import acquire_pr_checkout_lease, cleanup_pr_checkout_lease
from ..project_tests import ProjectTestRefusal, RepoProjectTests
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
    _redact_git_output,
)
from .refusals import ToolPolicyRefusal


log = logging.getLogger(__name__)


_GIT_EXECUTION_REFUSAL_CODES = frozenset({
    "git_failed",
    "invalid_git_output",
    "output_limit",
    "push_not_applied",
    "timeout",
})
_GIT_BINDING_REFUSAL_CODES = frozenset({
    "cross_pr_checkout",
    "inactive_checkout",
    "invalid_checkout",
    "invalid_scope",
})
_REPOSITORY_AUTHORIZATION_REFUSED = "repository_authorization_refused"
_REPOSITORY_BINDING_INVALID = "repository_binding_invalid"
_REPOSITORY_GIT_FAILED = "repository_git_failed"
_PROJECT_TEST_EXECUTION_REFUSAL_CODES = frozenset({
    "test_execution_failed",
    "test_timeout",
    "tests_failed",
})


def _tool_refusal(
    message: str,
    exc: BaseException,
    *,
    code: str | None = None,
    execution_codes: frozenset[str] = frozenset(),
) -> ToolException:
    """Preserve pre-execution policy refusals without downgrading execution faults."""
    if isinstance(exc, ToolPolicyRefusal) or (
        code is not None and code not in execution_codes
    ):
        return ToolPolicyRefusal(message)
    return ToolException(message)


def _state(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    from .forge import resolve_review_state

    try:
        return resolve_review_state(runtime, repository, pull_request)
    except ToolException as exc:
        detail = str(exc).removeprefix("pull-request operation rejected: ")
        message = f"repository operation rejected: {detail}"
        raise _tool_refusal(message, exc) from exc


def _enforcement_enabled(
    runtime: ToolRuntime[AuthContext] | None,
    *,
    repository: str,
    pull_request: int,
) -> bool:
    """Resolve the central flag, failing closed and reporting unknown state."""
    context = getattr(runtime, "context", None) if runtime is not None else None
    enforcement = getattr(context, "enforcement_enabled", None)
    if isinstance(enforcement, bool):
        return enforcement

    try:
        from ..event_logger import log_event_sync

        log_event_sync(
            "repo_enforcement_state_unknown",
            repository=repository,
            pull_request=pull_request,
            fallback_enforcement=True,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not alter the decision
        log.warning(
            "repo_enforcement_state_unknown: repository=%s pull_request=%s; "
            "failing closed (event logging failed: %s)",
            repository,
            pull_request,
            exc,
        )
    return True


def _execute(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
    operation: Any,
) -> dict[str, Any]:
    try:
        state = _state(runtime, repository, pull_request)
        return asdict(
            RepoGitTools(
                state,
                enforce=_enforcement_enabled(
                    runtime,
                    repository=repository,
                    pull_request=pull_request,
                ),
            ).execute(operation)
        )
    except (GitRefusal, ToolException, RuntimeError, ValueError) as exc:
        cause_code = getattr(exc, "code", None)
        if isinstance(exc, ToolPolicyRefusal) or (
            isinstance(exc, GitRefusal)
            and cause_code not in _GIT_EXECUTION_REFUSAL_CODES | _GIT_BINDING_REFUSAL_CODES
        ):
            code = _REPOSITORY_AUTHORIZATION_REFUSED
        elif isinstance(exc, ValueError) or cause_code in _GIT_BINDING_REFUSAL_CODES or (
            isinstance(exc, ToolException) and not isinstance(exc, GitRefusal)
        ):
            code = _REPOSITORY_BINDING_INVALID
        else:
            code = _REPOSITORY_GIT_FAILED
        cause = f" [{cause_code}]" if cause_code else ""
        detail = _redact_git_output(str(exc))
        message = f"repository operation rejected ({code}){cause}: {detail}"
        raise _tool_refusal(
            message,
            exc,
            code=code,
            execution_codes=frozenset({_REPOSITORY_GIT_FAILED}),
        ) from None


@tool
def repo_checkout(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Create the exact checkout lease bound to this turn's immutable PR scope."""
    from .forge import remediation_checkout_preflight

    context = getattr(runtime, "context", None) if runtime is not None else None
    state, stopped = remediation_checkout_preflight(context, repository, pull_request)
    if stopped is not None:
        return {"status": "stopped", "message": stopped}
    if state is None:
        raise ToolPolicyRefusal("repository checkout rejected: no authorized pull request state")
    try:
        lease, candidates = acquire_pr_checkout_lease(
            state.action_scope,
            owner=state.action_scope.principal,
            review_state=state,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolPolicyRefusal(f"repository checkout rejected: {exc}") from exc
    return {
        "status": "resumed" if candidates else "checked_out",
        "path": str(lease.path),
        "scope_id": lease.scope_id,
        "head_sha": lease.head_sha,
        "candidate_commits": candidates,
    }


@tool
def repo_cleanup(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Revoke and remove this turn's exact active checkout lease."""
    state = _state(runtime, repository, pull_request)
    lease = state.checkout_lease
    if lease is None:
        raise ToolPolicyRefusal("repository cleanup rejected: no active checkout lease")
    try:
        removed = cleanup_pr_checkout_lease(lease, review_state=state)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolPolicyRefusal(f"repository cleanup rejected: {exc}") from exc
    return {"status": "cleaned", "removed": removed, "scope_id": state.action_scope.scope_id}


@tool
def repo_fetch(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Fetch only the immutable head and base refs bound to this turn."""
    return _execute(runtime, repository, pull_request, GitFetch())


@tool
def repo_status(
    repository: str,
    pull_request: int,
    include_untracked: bool = True,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read porcelain status from the active bound checkout."""
    return _execute(runtime, repository, pull_request, GitStatus(include_untracked))


@tool
async def repo_test(
    repository: str,
    pull_request: int,
    selectors: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run the deployment-configured tests in the active bound PR checkout."""
    try:
        return asdict(
            await RepoProjectTests(_state(runtime, repository, pull_request)).execute(selectors)
        )
    except (ProjectTestRefusal, RuntimeError, ValueError) as exc:
        code = getattr(exc, "code", "project_test_failed")
        message = f"project test rejected ({code}): {exc}"
        raise _tool_refusal(
            message,
            exc,
            code=code,
            execution_codes=_PROJECT_TEST_EXECUTION_REFUSAL_CODES,
        ) from exc


@tool
def repo_diff(
    repository: str,
    pull_request: int,
    mode: Literal["working", "staged", "base"] = "working",
    paths: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read a bounded working, staged, or base diff from the bound checkout."""
    return _execute(runtime, repository, pull_request, GitDiff(mode, paths))


@tool
def repo_unmerged(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read unmerged-index records from the bound checkout."""
    return _execute(runtime, repository, pull_request, GitUnmerged())


@tool
def repo_stage(
    repository: str,
    pull_request: int,
    paths: tuple[str, ...],
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage only explicit repository-relative paths in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitStage(paths))


@tool
def repo_commit(
    repository: str,
    pull_request: int,
    paths: tuple[str, ...],
    message: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage explicit paths and commit them with server-owned Git identity."""
    return _execute(runtime, repository, pull_request, GitCommit(paths, message))


@tool
def repo_merge(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Merge the immutable observed base commit into the bound checkout."""
    return _execute(runtime, repository, pull_request, GitMerge())


@tool
def repo_merge_abort(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Abort an in-progress merge in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitMergeAbort())


@tool
def repo_rebase(
    repository: str,
    pull_request: int,
    base_property: str = "",
    base_verification: str = "",
    head_property: str = "",
    head_verification: str = "",
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Start a bound rebase, or continue it with two-sided preservation evidence."""
    return _execute(runtime, repository, pull_request, GitRebase(
        base_property, base_verification, head_property, head_verification,
    ))


@tool
def repo_rebase_abort(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Abort an in-progress rebase in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitRebaseAbort())


@tool
def repo_revert(
    repository: str,
    pull_request: int,
    commit: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Revert one full commit ID proven to be within bound head minus base."""
    return _execute(runtime, repository, pull_request, GitRevert(commit))


@tool
def repo_revert_abort(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Abort an in-progress revert in the bound checkout."""
    return _execute(runtime, repository, pull_request, GitRevertAbort())


@tool
def repo_push(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Push HEAD to the one destination ref in the immutable PR scope."""
    return _execute(runtime, repository, pull_request, GitPush())


def _bind_injected_runtime(repo_tool: StructuredTool) -> StructuredTool:
    callable_ = repo_tool.func or repo_tool.coroutine
    if callable_ is None:
        raise RuntimeError(f"repository tool {repo_tool.name!r} has no callable")
    callable_.__annotations__["runtime"] = ToolRuntime
    repo_tool.args_schema = create_schema_from_function(
        repo_tool.name, callable_, filter_args=(), include_injected=True,
    )
    repo_tool.__dict__.pop("_injected_args_keys", None)
    return repo_tool


REPO_TOOLS = tuple(_bind_injected_runtime(repo_tool) for repo_tool in (
    repo_checkout,
    repo_cleanup,
    repo_fetch,
    repo_status,
    repo_test,
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
