"""Model-facing wrappers for the closed, scope-bound Git API."""

from __future__ import annotations

import logging
import base64
import hashlib
from dataclasses import asdict
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function

from ..models import AuthContext, RepoReviewState, RetainedFactoryScope, SourceLabel
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
    _validate_path,
    _redact_git_output,
    retained_factory_git_runner,
)
from .refusals import ToolPolicyRefusal


log = logging.getLogger(__name__)


_GIT_BINDING_REFUSAL_CODES = frozenset({
    "cross_pr_checkout",
    "inactive_checkout",
    "invalid_checkout",
    "invalid_scope",
})
_CONTENT_FREE_GIT_REFUSALS = {
    "inactive_checkout": frozenset({"an active matching checkout lease is required"}),
    "invalid_checkout": frozenset({
        "retained checkout is unavailable", "retained checkout identity is invalid",
        "checkout lease may not be a symlink", "checkout lease path is unavailable",
        "checkout lease escapes its server root",
    }),
    "invalid_scope": frozenset({
        "scope head is not a full commit id", "scope base is not a full commit id",
        "scope destination is not a branch ref", "scope base is not a branch ref",
        "scope remote is not server-bound origin",
    }),
    "scope_action_denied": frozenset({
        "retained factory scope does not grant this repository operation",
    }),
}
_REPOSITORY_AUTHORIZATION_REFUSED = "repository_authorization_refused"
_REPOSITORY_BINDING_INVALID = "repository_binding_invalid"
_REPOSITORY_GIT_FAILED = "repository_git_failed"
_FIXED_TEST_REFUSALS = {
    "test_snapshot_unavailable": frozenset({"project test snapshot is unavailable"}),
    "test_containment_unavailable": frozenset({"contained project test execution is unavailable"}),
    "test_snapshot_cleanup_failed": frozenset({"project test snapshot cleanup failed"}),
    "test_config_invalid": frozenset({
        "project test command is invalid", "invalid env unset directive",
        "test runner is missing",
        "project test command or environment contains a controller path",
    }),
    "test_snapshot_credentials_refused": frozenset({
        "project test snapshot contains credential-like material",
    }),
    "test_snapshot_embedded_repository": frozenset({
        "project test snapshot source contains an embedded Git repository",
    }),
    "inactive_checkout": frozenset({"the checkout has no current HEAD"}),
}
_SUMMARY_SELECTOR = re.compile(r"[A-Za-z0-9._/,:+=-]{1,256}", re.ASCII)
_SUMMARY_SUITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)
_SUMMARY_HEAD = re.compile(r"[0-9a-f]{40,64}", re.ASCII)


def _tool_refusal(
    message: str,
    exc: BaseException,
    *,
    execution_started: bool = True,
) -> ToolException:
    """Preserve pre-execution policy refusals without downgrading execution faults."""
    if isinstance(exc, ToolPolicyRefusal) and not execution_started:
        return ToolPolicyRefusal(message)
    if not execution_started and isinstance(exc, (GitRefusal, ProjectTestRefusal)):
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


def _retained_scope(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    issue: int,
) -> RetainedFactoryScope | None:
    context = getattr(runtime, "context", None) if runtime is not None else None
    scope = getattr(context, "retained_factory_scope", None)
    if not isinstance(scope, RetainedFactoryScope):
        return None
    if scope.repository != repository or scope.issue_id != issue:
        return None
    return scope


def _publish_retained_result(
    runtime: ToolRuntime[AuthContext] | None, scope: RetainedFactoryScope,
) -> None:
    from ..access_control import publish_protected_result

    context = getattr(runtime, "context", None) if runtime is not None else None
    principal = getattr(context, "canonical_principal", None)
    if getattr(context, "is_service", False) and principal:
        principal = f"service:{principal}"
    publish_protected_result((SourceLabel(
        principal=principal,
        domain="repository",
        resource_id=(
            f"{scope.repository}#issue/{scope.issue_id}"
            f"@retained/{scope.scope_id}"
        ),
        bridge_instance="worklink",
        sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool",
        integrity="trusted",
        integrity_effect="active_ingest",
    ),))


def _retained_result(
    runtime: ToolRuntime[AuthContext] | None,
    scope: RetainedFactoryScope,
    result: dict[str, Any],
) -> dict[str, Any]:
    _publish_retained_result(runtime, scope)
    result["provenance"] = {
        "kind": "retained_factory_scope",
        "scope_id": scope.scope_id,
        "repository": scope.repository,
        "issue_id": scope.issue_id,
        "run_id": scope.run_id,
    }
    return result


def _retained_refusal(operation: str) -> ToolPolicyRefusal:
    return ToolPolicyRefusal(
        f"repository operation rejected: retained factory scope does not grant {operation}"
    )


def _publish_attested_lease_result(
    runtime: ToolRuntime[AuthContext] | None,
    state: RepoReviewState,
) -> None:
    """Publish exact-scope provenance when the checkout remains author-attested."""
    from ..access_control import (
        log_pr_checkout_lease_attestation_refused, pr_checkout_lease_attestation,
    )

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return
    scope = state.action_scope
    from ..access_control import _github_repo_from_remote

    # The failed command may contain remote server text. Bind that text to our
    # origin and the exact allowed refs before inheriting the lease verdict.
    # The lease predicate itself pins the original head independently of the
    # base branch; local descendants retain their separate lineage checks.
    if (
        scope.head_remote != "origin"
        or scope.head_repo != scope.canonical_repo
        or _github_repo_from_remote(scope.canonical_origin) != scope.canonical_repo
        or not scope.destination_ref.startswith("refs/heads/")
        or scope.destination_ref != f"refs/heads/{scope.head_ref}"
        or scope.checkout_ref not in (None, f"refs/pull/{scope.pr_number}/head")
    ):
        return
    attestation = pr_checkout_lease_attestation(context, scope, state.checkout_lease)
    if not attestation.ok:
        log_pr_checkout_lease_attestation_refused(context, scope, attestation.reason)
        return
    _publish_attested_scope_result(runtime, scope, scope.observed_head_sha)


def _publish_attested_scope_result(
    runtime: ToolRuntime[AuthContext] | None, scope: Any, head_sha: str,
) -> None:
    """Publish the exact repository source shared by checkout and Git results."""
    from ..access_control import publish_protected_result

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return
    principal = getattr(context, "canonical_principal", None)
    if getattr(context, "is_service", False) and principal:
        principal = f"service:{principal}"
    publish_protected_result((SourceLabel(
        principal=principal,
        domain="repository",
        resource_id=f"{scope.canonical_repo}#pull/{scope.pr_number}@{head_sha}",
        bridge_instance="forge",
        sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool",
        integrity="trusted",
        integrity_effect="active_ingest",
    ),))


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
    git_tools: RepoGitTools | None = None
    state: RepoReviewState | None = None
    try:
        retained = _retained_scope(runtime, repository, pull_request)
        if retained is not None:
            if not isinstance(operation, (GitStatus, GitDiff, GitStage, GitCommit)):
                raise _retained_refusal(type(operation).__name__)
            home_value = os.environ.get("MIMIR_HOME", "").strip()
            if not home_value:
                raise ToolPolicyRefusal("repository operation rejected: MIMIR_HOME is unavailable")
            from ..worklink.retained_scope import retained_factory_effect_lease

            with retained_factory_effect_lease(Path(home_value), retained) as lease:
                if lease.scope is None:
                    raise ToolPolicyRefusal(
                        f"repository operation rejected: {lease.refusal_reason}"
                    )
                git_tools = RepoGitTools(
                    retained_scope=retained, runner=retained_factory_git_runner,
                )
                return _retained_result(
                    runtime, retained, asdict(git_tools.execute(operation)),
                )
        state = _state(runtime, repository, pull_request)
        git_tools = RepoGitTools(
            state,
            auth_context=getattr(runtime, "context", None) if runtime is not None else None,
            enforce=_enforcement_enabled(
                runtime,
                repository=repository,
                pull_request=pull_request,
            ),
        )
        previous_head = state.action_scope.observed_head_sha
        result = asdict(git_tools.execute(operation))
        if (
            isinstance(operation, GitPush)
            and result["ok"]
            and state.action_scope.observed_head_sha != previous_head
        ):
            context = getattr(runtime, "context", None) if runtime is not None else None
            if context is not None and context.ifc_state is not None:
                context.ifc_state.record_own_push(
                    state.action_scope.canonical_repo, state.action_scope.pr_number,
                    previous_head,
                )
        _publish_attested_lease_result(runtime, state)
        return result
    except (GitRefusal, ToolException, RuntimeError, ValueError) as exc:
        cause_code = getattr(exc, "code", None)
        execution_started = bool(
            (exc.execution_started if isinstance(exc, GitRefusal) else False)
            or (git_tools is not None and git_tools.execution_started)
        )
        if isinstance(exc, ToolPolicyRefusal):
            code = _REPOSITORY_AUTHORIZATION_REFUSED
        elif execution_started:
            code = _REPOSITORY_GIT_FAILED
        elif isinstance(exc, ValueError) or cause_code in _GIT_BINDING_REFUSAL_CODES or (
            isinstance(exc, ToolException) and not isinstance(exc, GitRefusal)
        ):
            code = _REPOSITORY_BINDING_INVALID
        elif isinstance(exc, GitRefusal):
            code = _REPOSITORY_AUTHORIZATION_REFUSED
        else:
            code = _REPOSITORY_GIT_FAILED
        cause = f" [{cause_code}]" if cause_code else ""
        detail = _redact_git_output(str(exc))
        message = f"repository operation rejected ({code}){cause}: {detail}"
        if execution_started and state is not None:
            _publish_attested_lease_result(runtime, state)
        elif (
            isinstance(exc, GitRefusal)
            and not execution_started
            and str(exc) in _CONTENT_FREE_GIT_REFUSALS.get(exc.code, ())
        ):
            from ..access_control import publish_protected_result

            publish_protected_result((), content_free_error=message)
        raise _tool_refusal(
            message,
            exc,
            execution_started=execution_started,
        ) from None


@tool
def repo_checkout(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Create the exact checkout lease bound to this turn's immutable PR scope."""
    if _retained_scope(runtime, repository, pull_request) is not None:
        raise _retained_refusal("repo_checkout")
    from .forge import _call, _client, _pr_content_authors, remediation_checkout_preflight

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
        detail = _redact_git_output(str(exc))
        _publish_attested_lease_result(runtime, state)
        raise ToolException(f"repository checkout rejected: {detail}") from exc
    scope = state.action_scope
    # Record only turn-local authority, never trust in checkout-controlled metadata.
    # Clear an earlier verdict before fetching metadata, so failures stay closed.
    context.ifc_state.pr_checkout_author_trust[scope.scope_id] = None
    context.ifc_state.pr_checkout_author_trust_cause[scope.scope_id] = "author_unresolved"
    client = _client(scope)
    from .forge import _BOT_LOGIN, _author_verdict

    if callable(getattr(client, "author_is_trusted", None)) or (
        isinstance(scope.pull_request_author, str) and _BOT_LOGIN.fullmatch(scope.pull_request_author)
    ):
        authors, _ = _call(lambda: _pr_content_authors(client, scope))
        if authors == (scope.pull_request_author,) and scope.pull_request_author:
            from ..pollers import _github_author_trust_cause

            _github_author_trust_cause.set(None)
            verdict = _author_verdict(context, scope, scope.pull_request_author, client)
            context.ifc_state.pr_checkout_author_trust[scope.scope_id] = verdict
            if verdict is None:
                from ..pollers import github_author_trust_cause

                context.ifc_state.pr_checkout_author_trust_cause[scope.scope_id] = (
                    github_author_trust_cause() or "author_unresolved"
                )
            else:
                context.ifc_state.pr_checkout_author_trust_cause.pop(scope.scope_id, None)
    _publish_attested_lease_result(runtime, state)
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
    if _retained_scope(runtime, repository, pull_request) is not None:
        raise _retained_refusal("repo_cleanup")
    state = _state(runtime, repository, pull_request)
    lease = state.checkout_lease
    if lease is None:
        raise ToolPolicyRefusal("repository cleanup rejected: no active checkout lease")
    _publish_attested_lease_result(runtime, state)
    try:
        removed = cleanup_pr_checkout_lease(lease, review_state=state)
    except (OSError, RuntimeError, ValueError) as exc:
        detail = _redact_git_output(str(exc))
        raise ToolException(f"repository cleanup rejected: {detail}") from exc
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
    """Read porcelain status from the active bound checkout.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id.
    """
    return _execute(runtime, repository, pull_request, GitStatus(include_untracked))


@tool
async def repo_test(
    repository: str,
    pull_request: int,
    selectors: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
    suite: str | None = None,
    include_output: bool = False,
) -> dict[str, Any]:
    """Run configured tests in a contained repository snapshot.

    Omit ``suite`` to infer it only when every selector matches one declared
    suite's prefixes or suffixes (no selectors: default). Otherwise pass
    ``suite`` explicitly, e.g. ``suite="python"`` or ``suite="frontend"``.
    On a retained Worklink remediation turn,
    ``repository`` is the retained run's repository and ``pull_request`` is the
    incident's Chainlink issue id.
    """
    try:
        state: RepoReviewState | None = None
        retained = _retained_scope(runtime, repository, pull_request)
        if retained is not None:
            home_value = os.environ.get("MIMIR_HOME", "").strip()
            if not home_value:
                raise ToolPolicyRefusal("project test rejected: MIMIR_HOME is unavailable")
            from ..worklink.retained_scope import retained_factory_effect_lease

            with retained_factory_effect_lease(Path(home_value), retained) as lease:
                if lease.scope is None:
                    raise ToolPolicyRefusal(f"project test rejected: {lease.refusal_reason}")
                result = asdict(
                    await RepoProjectTests(retained_scope=retained).execute(
                        selectors, suite=suite,
                    )
                )
                result.pop("failure_summary", None)
                if result["pytest_summary"] is None:
                    result.pop("pytest_summary")
                result = _retained_result(runtime, retained, result)
                result["remediation_guidance"] = _remediation_test_guidance(
                    result["code"], scoped=bool(selectors),
                )
                return result
        state = _state(runtime, repository, pull_request)
        result = asdict(
            await RepoProjectTests(state).execute(selectors, suite=suite)
        )
        failure_summary = result.pop("failure_summary", None)
        if result["pytest_summary"] is None:
            result.pop("pytest_summary")
        _publish_attested_lease_result(runtime, state)
        if (
            result["code"] == "tests_failed" and not include_output
            and failure_summary is not None
            and type(result["returncode"]) is int and result["returncode"] != 0
            and _SUMMARY_SUITE.fullmatch(result["suite"]) is not None
            and _SUMMARY_HEAD.fullmatch(state.action_scope.observed_head_sha) is not None
            and isinstance(selectors, tuple)
            and len(selectors) <= 32
            and all(isinstance(item, str) and _SUMMARY_SELECTOR.fullmatch(item) for item in selectors)
        ):
            return {
                "ok": False, "code": "tests_failed", "exit_code": result["returncode"],
                "suite": result["suite"], "selectors": list(selectors),
                "summary": {**failure_summary, "head": state.action_scope.observed_head_sha},
                "remediation_guidance": (
                    "The summary lists failing node ids. Prefer reading the lease's test source "
                    "and rerunning selected ids. include_output=true reveals bounded raw output."
                ),
            }
        result["remediation_guidance"] = _remediation_test_guidance(result["code"], scoped=bool(selectors))
        return result
    except (ProjectTestRefusal, RuntimeError, ValueError) as exc:
        if state is not None and (
            not isinstance(exc, ProjectTestRefusal) or exc.execution_started
        ) and not (
            isinstance(exc, ProjectTestRefusal) and exc.fixed_message
            and str(exc) in _FIXED_TEST_REFUSALS.get(exc.code, ())
        ):
            _publish_attested_lease_result(runtime, state)
        code = getattr(exc, "code", "project_test_failed")
        message = f"project test rejected ({code}): {exc}"
        message += "\n" + _remediation_test_guidance(code, scoped=bool(selectors))
        if (
            isinstance(exc, ProjectTestRefusal)
            and exc.fixed_message
            and str(exc) in _FIXED_TEST_REFUSALS.get(code, ())
        ):
            raise ToolPolicyRefusal(message) from exc
        raise _tool_refusal(
            message,
            exc,
            execution_started=(
                exc.execution_started
                if isinstance(exc, ProjectTestRefusal)
                else True
            ),
        ) from exc


def _remediation_test_guidance(code: str, *, scoped: bool) -> str:
    """Publication advice, not authorization or a replacement for CI checks."""
    publish = (
        "After scoped tests pass, commit, push the fix to the PR branch, and "
        "re-request review. In the PR comment state that the in-runner full suite "
        "did not complete (not failed) and CI is the validation surface. "
        "Do not wait for another contained full-suite run."
    )
    if scoped and code != "tests_passed":
        return "Hold publication: scoped tests must pass before pushing; this scoped run did not pass."
    # Overflow stops execution, so even a nonzero exit is incomplete evidence,
    # not an observed completed failing suite. Scoped runs remain blocked above.
    if code in {"test_timeout", "test_output_overflow", "tests_failed_output_overflow"}:
        return (
            "the contained runner did not complete the suite; this is not test evidence "
            "either way — push and rely on CI. " + publish
        )
    if code in {"test_snapshot_unavailable", "test_containment_unavailable"}:
        return "Infrastructure unavailable, not a failed suite. " + publish
    if code == "tests_failed":
        return (
            "Hold publication for a completed failing full-suite run with counts observed "
            "and failed_tests non-empty. If only the runner failed without test evidence, "
            "report the infrastructure failure, not a failed suite; " + publish
        )
    if code == "tests_passed":
        return (
            "Scoped tests must pass before pushing. Only a completed failing full-suite "
            "run with counts observed and failed_tests non-empty blocks publication; "
            "a full-suite timeout or unavailable snapshot/runner does not. "
            "Commit, push to the PR branch, and re-request review when these conditions hold."
        )
    return "Resolve this refusal; it does not authorize publication or bypass repository policy."


@tool
def repo_diff(
    repository: str,
    pull_request: int,
    mode: Literal["working", "staged", "base"] = "working",
    paths: tuple[str, ...] = (),
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read a bounded working, staged, or base diff from the bound checkout.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id;
    retained scopes do not support ``mode='base'``.
    """
    return _execute(runtime, repository, pull_request, GitDiff(mode, paths))


@tool
def repo_unmerged(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read unmerged-index records from the bound checkout."""
    return _execute(runtime, repository, pull_request, GitUnmerged())


_HISTORY_FILE_CAP = 2 * 1024 * 1024
_HISTORY_PAGE_CAP = 8192
_HISTORY_REVISION = re.compile(r"[0-9a-fA-F]{7,40}")


def _historical_blob(git: RepoGitTools, revision: str, path: str) -> tuple[str, int, str]:
    """Resolve only a regular blob in the immutable base/head ancestry."""
    if not isinstance(revision, str) or _HISTORY_REVISION.fullmatch(revision) is None:
        raise ToolPolicyRefusal("repository history rejected: revision must be a commit SHA")
    _validate_path(path)
    scope = git._scope
    commit = git._checked(("rev-parse", "--verify", f"{revision}^{{commit}}")).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ToolPolicyRefusal("repository history rejected: invalid commit")
    # A fetched unrelated commit may exist in the object database. Presence is
    # not authority: the exact commit must belong to one of the bound lineages.
    if not any(
        result.returncode == 0 and not result.timed_out and not result.output_limited
        for bound in (scope.observed_base_sha, scope.observed_head_sha)
        for result in (git._raw(("merge-base", "--is-ancestor", commit, bound)),)
    ):
        raise ToolPolicyRefusal("repository history rejected: revision is outside base and head history")
    entry = git._checked(("ls-tree", "-z", commit, "--", f":(literal){path}")).stdout
    match = re.fullmatch(r"(100644|100755) blob ([0-9a-f]{40})\t([^\x00]+)\x00", entry)
    if match is None or match.group(3) != path:
        raise ToolPolicyRefusal("repository history rejected: source is not a regular file")
    blob = match.group(2)
    size_text = git._checked(("cat-file", "-s", blob)).stdout.strip()
    if not size_text.isascii() or not size_text.isdecimal():
        raise ToolPolicyRefusal("repository history rejected: invalid blob size")
    size = int(size_text)
    if size > _HISTORY_FILE_CAP:
        raise ToolPolicyRefusal("repository history rejected: file exceeds 2 MiB")
    return blob, size, match.group(1)


def _historical_bytes(git: RepoGitTools, blob: str, size: int) -> bytes:
    """Read binary object data directly, never via a model-visible Git result."""
    try:
        result = subprocess.run(
            (git._git, "-C", str(git._root), "--no-pager", "--no-optional-locks",
             "cat-file", "blob", blob),
            env=git.environment, capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolException("repository history rejected: Git object read failed") from exc
    if result.returncode or len(result.stdout) != size:
        raise ToolException("repository history rejected: Git object read failed")
    return result.stdout


def _history_file(
    runtime: ToolRuntime[AuthContext] | None, repository: str, pull_request: int,
    revision: str, path: str, *, write: bool,
) -> tuple[RepoReviewState, RepoGitTools, bytes, str]:
    if _retained_scope(runtime, repository, pull_request) is not None:
        raise _retained_refusal("repository history")
    state = _state(runtime, repository, pull_request)
    from ..models import RepoPRAction

    try:
        git = RepoGitTools(state, auth_context=getattr(runtime, "context", None), enforce=True)
        git._require("repo_restore_file" if write else "repo_file_at_revision",
                     RepoPRAction.WRITE if write else RepoPRAction.INSPECT)
        context = getattr(runtime, "context", None)
        if write and getattr(getattr(context, "ifc_state", None), "has_untrusted_active_ingest", lambda: False)():
            raise ToolPolicyRefusal("repository history rejected: write blocked by untrusted ingest")
        blob, size, mode = _historical_blob(git, revision, path)
    except GitRefusal as exc:
        raise ToolPolicyRefusal(f"repository history rejected: {exc.code}") from exc
    return state, git, _historical_bytes(git, blob, size), mode


def _restore_bytes(state: RepoReviewState, destination_path: str, content: bytes, mode: str) -> str:
    from ..access_control import _target_within_active_pr_checkout_lease

    root = Path(state.checkout_lease.path)
    if not isinstance(destination_path, str) or not destination_path or "\x00" in destination_path:
        raise ToolPolicyRefusal("repository restore rejected: invalid destination")
    requested = Path(destination_path)
    target = requested if requested.is_absolute() else root / requested
    if not _target_within_active_pr_checkout_lease(str(target), state):
        raise ToolPolicyRefusal("repository restore rejected: destination is outside writable checkout")
    relative = target.relative_to(root)
    if not relative.parts:
        raise ToolPolicyRefusal("repository restore rejected: destination must be a file")
    # Walk from the validated lease root with no-follow directory descriptors;
    # never create through an existing symlink, even one pointing within it.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary: str | None = None
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        name = relative.name
        try:
            existing = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ToolPolicyRefusal("repository restore rejected: destination is not a regular file")
        temporary = f".repo-restore-{uuid.uuid4().hex}"
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            stream.write(content)
            # Apply the committed mode explicitly: creation modes are masked by
            # umask, and the private temporary must not dictate the final mode.
            os.fchmod(stream.fileno(), 0o755 if mode == "100755" else 0o644)
        # Recheck the pathname before the atomic replacement (including policy).
        if not _target_within_active_pr_checkout_lease(str(target), state):
            raise ToolPolicyRefusal("repository restore rejected: destination changed")
        try:
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise ToolPolicyRefusal("repository restore rejected: destination changed")
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        temporary = None
    except OSError as exc:
        raise ToolPolicyRefusal("repository restore rejected: unsafe destination") from exc
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=fd)
        os.close(fd)
    return relative.as_posix()


@tool
def repo_restore_file(
    repository: str, pull_request: int, revision: str, source_path: str,
    destination_path: str, runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Use for byte-exact fixtures or restores from repository history into the scoped PR checkout.

    Copies a regular file from a base/head ancestor (up to 2 MiB) without
    sending its bytes through the model. Returns only destination, byte count,
    sha256, and Git mode (100644 or 100755). Preserves the committed executable
    bit. Not available in review or after untrusted active ingest.
    """
    state, _git, content, mode = _history_file(
        runtime, repository, pull_request, revision, source_path, write=True,
    )
    destination = _restore_bytes(state, destination_path, content, mode)
    _publish_attested_lease_result(runtime, state)
    return {
        "destination": destination, "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(), "mode": mode,
    }


@tool
def repo_file_at_revision(
    repository: str, pull_request: int, revision: str, path: str,
    offset: int = 0, limit: int = 4096,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read old versions from repository history in bounded, byte-exact pages.

    Use byte offset/limit to inspect long files. content is readable UTF-8 when
    the whole window decodes, otherwise base64 (including split characters).
    encoding identifies the format; next_offset is null at EOF, never silently
    truncated. Decode each page according to encoding to recover exact bytes.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= _HISTORY_PAGE_CAP:
        raise ToolPolicyRefusal("repository history rejected: invalid page window")
    state, _git, content, _mode = _history_file(
        runtime, repository, pull_request, revision, path, write=False,
    )
    if offset > len(content):
        raise ToolPolicyRefusal("repository history rejected: offset exceeds file size")
    end = min(len(content), offset + limit)
    window = content[offset:end]
    try:
        page_content = window.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        page_content = base64.b64encode(window).decode("ascii")
        encoding = "base64"
    _publish_attested_lease_result(runtime, state)
    return {
        "encoding": encoding, "content": page_content,
        "offset": offset, "total_bytes": len(content),
        "next_offset": end if end < len(content) else None,
    }


@tool
def repo_stage(
    repository: str,
    pull_request: int,
    paths: tuple[str, ...],
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage only explicit repository-relative paths in the bound checkout.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id.
    """
    return _execute(runtime, repository, pull_request, GitStage(paths))


@tool
def repo_commit(
    repository: str,
    pull_request: int,
    paths: tuple[str, ...],
    message: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Stage explicit paths and commit them with server-owned Git identity.

    On a retained Worklink remediation turn, ``repository`` is the retained
    run's repository and ``pull_request`` is the incident's Chainlink issue id.
    """
    return _execute(runtime, repository, pull_request, GitCommit(paths, message))


@tool
def repo_merge(
    repository: str, pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """The default way to update a PR branch with its base in every PR-remediation scope.

    Merge the immutable observed base into the bound checkout. If it merges
    cleanly, the merge commit already exists; next call repo_push.
    On conflicts: repo_unmerged, resolve, repo_stage, then repo_commit listing
    every staged path (the merge stages auto-merged files too), then repo_push.
    """
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
    """Start a bound rebase, or continue it with two-sided preservation evidence.

    Available only on turns for history-rewrite event types in
    _HISTORY_REWRITE_EVENT_TYPES (mimir/repo_tools.py):
    `heartbeat_pr_maintenance`, `pr_changes_requested_stale`, `pr_ci_failure`,
    `pr_mergeability_rebase`, `pr_mergeability_conflicting`.
    In other PR-remediation scopes, use repo_merge to update the branch with its base.
    """
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
    repo_file_at_revision,
    repo_restore_file,
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
