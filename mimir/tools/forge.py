"""Closed, scope-bound repository and pull-request tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function

from ..forge import ForgeClient, ForgeError, IssueTarget, ReviewVerdict
from ..redaction import redact_text
from ..models import (
    AuthContext, RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState,
    ServerDiscoveredPRStates,
)
from .refusals import ToolPolicyRefusal

_BODY_MAX_BYTES = 65_536
_PATH_MAX_BYTES = 4_096
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_ESCALATION_DESCRIPTION_MAX_BYTES = 4_096
_ESCALATION_ATTEMPT_MAX_BYTES = 512
_ESCALATION_MAX_ATTEMPTS = 16
_clients: dict[str, ForgeClient] = {}
_default_client: ForgeClient | None = None
_escalation_lock = threading.Lock()
_github_identity_degraded = False
_github_identity_degraded_error: ForgeError | None = None
_github_identity_degraded_lock = threading.Lock()
_github_identity_degraded_callback: Any | None = None


def set_github_identity_degraded_callback(
    callback: Any | None, *, notify_current: bool = False,
) -> None:
    """Install the process callback that emits the degraded event and alert."""
    global _github_identity_degraded_callback
    _github_identity_degraded_callback = callback
    if notify_current and callback is not None:
        with _github_identity_degraded_lock:
            current = _github_identity_degraded_error
        if current is not None:
            callback(current)


def github_identity_is_degraded() -> bool:
    """Return whether GitHub identity verification latched coding off."""
    with _github_identity_degraded_lock:
        return _github_identity_degraded


def github_identity_recovery_pending() -> bool:
    """Return whether coding is disabled by an unverified transient failure."""
    from ..forge.github import GitHubIdentityFailureKind

    with _github_identity_degraded_lock:
        current = _github_identity_degraded_error
    return (
        current is not None
        and getattr(current, "failure_kind", None) == GitHubIdentityFailureKind.TRANSIENT
    )


def _latch_github_identity_degraded(exc: ForgeError) -> None:
    global _github_identity_degraded, _github_identity_degraded_error
    from ..forge.github import GitHubIdentityFailureKind

    with _github_identity_degraded_lock:
        if (
            _github_identity_degraded
            and getattr(_github_identity_degraded_error, "failure_kind", None)
            != GitHubIdentityFailureKind.TRANSIENT
        ):
            return
        _github_identity_degraded = True
        _github_identity_degraded_error = exc
    callback = _github_identity_degraded_callback
    if callback is not None:
        callback(exc)


def register_forge_client(client: ForgeClient, *, repositories: tuple[str, ...]) -> None:
    """Register an adapter for exact repositories without changing the tools."""
    for repository in repositories:
        normalized = repository.strip().lower()
        if not normalized or "/" not in normalized:
            raise ValueError("invalid repository registration")
        _clients[normalized] = client


def set_forge_client(client: ForgeClient | None) -> None:
    """Set the fallback adapter used when no repository registration exists."""
    global _default_client
    _default_client = client


def initialize_github_forge_identity() -> bool:
    """Bind the declared GitHub identity, degrading coding on any mismatch."""
    global _github_identity_degraded, _github_identity_degraded_error
    if github_identity_is_degraded() and not github_identity_recovery_pending():
        return False
    declared_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if not declared_login:
        from ..forge.github import GitHubIdentityVerificationError

        _latch_github_identity_degraded(
            GitHubIdentityVerificationError("github declared identity is empty")
        )
        return False
    from ..forge.github import GitHubForgeClient

    client = GitHubForgeClient()
    try:
        client.verify_identity(declared_login)
    except ForgeError as exc:
        _latch_github_identity_degraded(exc)
        return False
    set_forge_client(client)
    with _github_identity_degraded_lock:
        _github_identity_degraded = False
        _github_identity_degraded_error = None
    return True


def confirm_github_tool_identity(principal: str, token: str | None = None) -> str:
    """Refuse safely and latch coding off when the process binding no longer holds."""
    if github_identity_is_degraded():
        raise ToolPolicyRefusal(
            "coding capability is disabled until restart after GitHub identity verification failed"
        )
    from ..forge.github import confirm_github_identity

    try:
        return confirm_github_identity(principal, token)
    except ForgeError as exc:
        _latch_github_identity_degraded(exc)
        raise ToolPolicyRefusal(
            f"coding capability disabled: GitHub identity verification failed: {exc}"
        ) from exc


def _scope(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
) -> RepoPRActionScope:
    # PR action policy is enforced by access_control.authorize_repo_pr_tool.
    # This layer only resolves the server-issued scope used for safe execution.
    return resolve_review_state(runtime, repository, pull_request).action_scope


def resolve_review_state(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
) -> Any:
    """Resolve existing narrow authority or derive standing live review authority."""
    context = getattr(runtime, "context", None)
    return resolve_review_state_for_context(context, repository, pull_request)


def resolve_review_state_for_context(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    """Context-level variant used by authorization before tool invocation."""
    cache = getattr(context, "server_discovered_pr_states", None)
    state = cache.resolve(repository, pull_request) if (
        isinstance(cache, ServerDiscoveredPRStates)
        and isinstance(repository, str)
        and isinstance(pull_request, int)
    ) else None
    registry = getattr(context, "repo_pr_scope_registry", None)
    if state is None:
        state = registry.resolve(repository, pull_request) if isinstance(
            registry, RepoPRScopeRegistry,
        ) else None
    if state is not None:
        return state
    if (
        not isinstance(repository, str)
        or not isinstance(pull_request, int)
        or isinstance(pull_request, bool)
        or pull_request < 1
    ):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: repository must be text and pull_request "
            "must be a positive integer; for example, repository='owner/repo', pull_request=17"
        )
    from ..access_control import (
        create_server_discovered_review_scope,
        is_configured_github_repo,
        resolve_server_discovered_review_scope,
    )

    if not is_configured_github_repo(repository):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: repository is not configured in GITHUB_REPOS"
        )
    if (
        context is None
        or context.trigger != "user_message"
        or context.is_service
        or context.event_ingress is not None
        or not context.canonical_principal
        or "admin" not in context.roles
    ):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: live scope discovery requires an "
            "authenticated operator user turn"
        )
    cached = cache.resolve(repository, pull_request) if cache is not None else None
    if cached is not None:
        return cached
    client = _client_for_repository(repository)
    try:
        snapshot = client.get_pull_request_snapshot(repository.lower(), pull_request)
    except ForgeError as exc:
        raise ToolException(f"pull-request operation rejected: {exc}") from exc
    review_state = None
    if snapshot.state == "open" and snapshot.author == os.environ.get(
        "MIMIR_GITHUB_SELF_LOGIN", ""
    ).strip():
        discovery_scope = create_server_discovered_review_scope(repository, snapshot)
        if discovery_scope is not None:
            try:
                reviews = client.list_reviews(discovery_scope)
            except ForgeError as exc:
                raise ToolException(f"pull-request operation rejected: {exc}") from exc
            latest: dict[str, str] = {}
            for review in reviews:
                state = review.state.upper()
                if state in {"APPROVED", "CHANGES_REQUESTED"}:
                    latest[review.author] = state
            if "CHANGES_REQUESTED" in latest.values():
                review_state = "CHANGES_REQUESTED"
    resolution = resolve_server_discovered_review_scope(
        snapshot.repo, snapshot, review_state=review_state,
    )
    scope = resolution.scope
    if (
        scope is None
        or scope.canonical_repo != repository.lower()
        or scope.pr_number != pull_request
    ):
        raise ToolPolicyRefusal(resolution.refusal_reason or (
            "pull-request operation rejected: live pull request is closed or invalid"
        ))
    state = RepoReviewState(scope)
    return cache.remember(state) if cache is not None else state


def revalidate_review_head_for_context(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> None:
    """Refuse a typed review effect if the forge no longer reports its scoped head."""
    state = resolve_review_state_for_context(context, repository, pull_request)
    scope = state.action_scope
    client = _client_for_repository(scope.canonical_repo)
    try:
        snapshot = client.get_pull_request_snapshot(
            scope.canonical_repo, scope.pr_number,
        )
    except ForgeError as exc:
        raise ToolException(f"pull-request operation rejected: {exc}") from exc
    if (
        not isinstance(snapshot.repo, str)
        or snapshot.repo.lower() != scope.canonical_repo
        or snapshot.number != scope.pr_number
        or snapshot.state != "open"
        or snapshot.head_sha.lower() != scope.observed_head_sha
    ):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: pull request head advanced after scope issuance"
        )


def remediation_checkout_preflight(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> tuple[RepoReviewState | None, str | None]:
    """Refresh one stale own-remediation scope from a live provider snapshot."""
    registry = getattr(context, "repo_pr_scope_registry", None)
    original = registry.resolve(repository, pull_request) if isinstance(
        registry, RepoPRScopeRegistry,
    ) else None
    if original is None:
        return resolve_review_state_for_context(context, repository, pull_request), None
    scope = original.action_scope
    if scope.event_type not in {"pr_changes_requested_stale", "pr_ci_failure"} or scope.provenance.value != "poller_payload":
        return resolve_review_state_for_context(context, repository, pull_request), None

    client = _client_for_repository(repository)
    try:
        snapshot = client.get_pull_request_snapshot(repository.lower(), pull_request)
    except ForgeError as exc:
        raise ToolException(f"repository checkout rejected: {exc}") from exc
    if snapshot.state != "open":
        return None, "pull request is closed or merged"
    if scope.event_type == "pr_ci_failure":
        if snapshot.head_sha.lower() != scope.observed_head_sha.lower():
            return None, "pull request head was superseded"
        try:
            checks = client.list_checks(scope)
        except ForgeError as exc:
            raise ToolException(f"repository checkout rejected: {exc}") from exc
        if not any(
            check.status == "completed"
            and check.conclusion in {
                "failure", "timed_out", "startup_failure", "action_required",
            }
            for check in checks
        ):
            return None, "pull request checks are no longer failing"
        return original, None
    if snapshot.head_sha.lower() == scope.observed_head_sha.lower():
        return original, None

    cache = getattr(context, "server_discovered_pr_states", None)
    if not isinstance(cache, ServerDiscoveredPRStates) or not cache.begin_remint(
        repository, pull_request,
    ):
        return resolve_review_state_for_context(context, repository, pull_request), None

    from ..access_control import create_server_discovered_heartbeat_scope

    fresh_scope = create_server_discovered_heartbeat_scope(
        repository, snapshot, event_type=scope.event_type,
    )
    if fresh_scope is None:
        # Failed re-authorization must retain today's exact stale-scope refusal.
        return original, None
    fresh = RepoReviewState(fresh_scope)

    from ..repo_tools import was_agent_push

    if not was_agent_push(
        repository, pull_request, scope.observed_head_sha, snapshot.head_sha,
    ):
        try:
            reviews = client.list_reviews(fresh_scope)
        except ForgeError as exc:
            raise ToolException(f"repository checkout rejected: {exc}") from exc
        latest: dict[str, str] = {}
        self_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
        for review in reviews:
            state = review.state.upper()
            if review.author == self_login or state not in {"APPROVED", "CHANGES_REQUESTED"}:
                continue
            latest[review.author] = state
        if "CHANGES_REQUESTED" not in latest.values():
            return None, "pull request no longer has a blocking changes-requested review"
    return cache.remember_remint(original, fresh), None


def _client(scope: RepoPRActionScope) -> ForgeClient:
    return _client_for_repository(scope.canonical_repo)


def _client_for_repository(repository: str) -> ForgeClient:
    client = _clients.get(repository.lower()) or _default_client
    if client is None:
        from ..forge.github import GitHubForgeClient

        client = GitHubForgeClient()
    return client


def _body(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolPolicyRefusal("body must be non-empty text; for example, body='Looks good'")
    if len(value.encode("utf-8")) > _BODY_MAX_BYTES:
        raise ToolPolicyRefusal(
            "body must be non-empty text within the 65536-byte UTF-8 limit; "
            "for example, body='Looks good'"
        )
    if "\x00" in value:
        raise ToolPolicyRefusal(
            "body must contain text without null bytes; for example, body='Looks good'"
        )
    return value


def _repository(value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise ToolPolicyRefusal(
            "repository must be text shaped 'owner/repo'; for example, repository='octocat/hello'"
        )
    return value


def _issue_number(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ToolPolicyRefusal("issue must be a positive integer; for example, issue=220")
    return value


def resolve_issue_comment_target(repository: str, issue: int) -> IssueTarget:
    """Fetch and validate the exact configured issue used as the IFC sink."""
    repo = _repository(repository)
    number = _issue_number(issue)
    from ..access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal(
            "issue comment rejected: repository is not configured in GITHUB_REPOS"
        )
    return _call(lambda: _client_for_repository(repo).get_open_issue_target(repo, number))


def _path(value: str) -> str:
    path = Path(value)
    if (
        not value
        or len(value.encode("utf-8")) > _PATH_MAX_BYTES
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise ToolPolicyRefusal(
            "path must be a relative repository path without '..' or control characters, "
            "at most 4096 UTF-8 bytes; for example, path='src/app.py'"
        )
    return value


def _call(operation: Any) -> Any:
    try:
        return operation()
    except ForgeError as exc:
        from ..forge.github import GitHubIdentityVerificationError

        if isinstance(exc, GitHubIdentityVerificationError):
            _latch_github_identity_degraded(exc)
            raise ToolPolicyRefusal(
                f"coding capability disabled: GitHub identity verification failed: {exc}"
            ) from exc
        # The adapter may have contacted the forge before failing, so this is a
        # fault rather than a proven pre-execution policy refusal.
        raise ToolException(str(exc)) from exc


@tool
def pr_metadata(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read metadata for an exact pull request authorized by this turn."""
    scope = _scope(runtime, repository, pull_request)
    return asdict(_call(lambda: _client(scope).get_pull_request(scope)))


@tool
def pr_files(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded file projections for the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    return [asdict(item) for item in _call(lambda: _client(scope).list_files(scope))]


@tool
def pr_diff(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read the bounded unified diff for the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    return _call(lambda: _client(scope).get_diff(scope))


@tool
def pr_checks(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded check projections for the bound pull request head."""
    scope = _scope(runtime, repository, pull_request)
    return [asdict(item) for item in _call(lambda: _client(scope).list_checks(scope))]


@tool
def pr_reviews(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded submitted-review projections for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    return [asdict(item) for item in _call(lambda: _client(scope).list_reviews(scope))]


@tool
def pr_comments(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded conversation and inline comments for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    return [asdict(item) for item in _call(lambda: _client(scope).list_comments(scope))]


@tool
def pr_review_requests(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded pending review requests for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    return [
        asdict(item)
        for item in _call(lambda: _client(scope).list_review_requests(scope))
    ]


@tool
def pr_submit_review(
    repository: str,
    pull_request: int,
    verdict: ReviewVerdict,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Submit one approve, comment, or request-changes review on the bound PR."""
    scope = _scope(runtime, repository, pull_request)
    safe_body = _body(body)
    return asdict(_call(lambda: _client(scope).submit_review(scope, verdict, safe_body)))


@tool
def pr_inline_review_comment(
    repository: str,
    pull_request: int,
    path: str,
    line: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one inline review comment to a right-side line on the bound PR head."""
    scope = _scope(runtime, repository, pull_request)
    if isinstance(line, bool) or not isinstance(line, int) or line < 1 or line > 10_000_000:
        raise ToolPolicyRefusal(
            "line must be an integer from 1 through 10000000; for example, line=42"
        )
    safe_path = _path(path)
    safe_body = _body(body)
    return asdict(_call(lambda: _client(scope).add_inline_review_comment(
        scope, path=safe_path, line=line, body=safe_body,
    )))


@tool
def pr_comment(
    repository: str,
    pull_request: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one conversation comment to the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    safe_body = _body(body)
    return asdict(_call(lambda: _client(scope).add_pull_request_comment(scope, safe_body)))


@tool
def issue_comment(
    repository: str,
    issue: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one comment to a server-resolved open issue in a configured repository."""
    safe_body = _body(body)
    target = resolve_issue_comment_target(repository, issue)
    return asdict(_call(lambda: _client_for_repository(target.canonical_repo).add_issue_comment(
        target.canonical_repo, target.issue_number, safe_body,
    )))


@tool
def pr_rerequest_review(
    repository: str,
    pull_request: int,
    reviewer: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Re-request one reviewer on the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    if _REVIEWER.fullmatch(reviewer) is None:
        raise ToolPolicyRefusal(
            "reviewer must be a 1-39 character GitHub login containing letters, digits, "
            "or hyphens; for example, reviewer='octocat'"
        )
    _call(lambda: _client(scope).rerequest_review(scope, reviewer))
    return {"status": "review_rerequested", "reviewer": reviewer}


def _escalation_state_path() -> Path:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ToolPolicyRefusal("unsupported operation escalation requires MIMIR_HOME")
    return Path(home).resolve() / "state" / "unsupported_operations.json"


def _bounded_escalation_text(value: Any, *, fallback: str, max_bytes: int) -> str:
    """Normalize untrusted prose into bounded, single-line operator-visible text."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value[: max_bytes * 2]
    else:
        text = str(value)[: max_bytes * 2]
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    text = redact_text(" ".join(text.split())) or fallback
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return text or fallback


def _normalize_attempts(value: Any) -> list[str]:
    if value is None:
        candidates: list[Any] = []
    elif isinstance(value, (list, tuple, set, frozenset)):
        candidates = list(value)[:_ESCALATION_MAX_ATTEMPTS]
    else:
        candidates = [value]
    return [
        _bounded_escalation_text(
            candidate,
            fallback="unspecified attempt",
            max_bytes=_ESCALATION_ATTEMPT_MAX_BYTES,
        )
        for candidate in candidates
    ]


def _operation_key(description: str) -> str:
    words = re.findall(r"[a-z0-9]+", description.lower())
    prefix = "_".join(words[:6])[:64].strip("_") or "unspecified_operation"
    digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _emit_unsupported(
    scope: RepoPRActionScope,
    operation: str,
    description: str,
    attempted_operations: list[str],
) -> bool:
    key = f"{scope.scope_id}:{operation}"
    state_path = _escalation_state_path()
    with _escalation_lock:
        try:
            known = set(json.loads(state_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            known = set()
        except (OSError, ValueError, TypeError) as exc:
            raise ToolException("unsupported operation escalation state is unreadable") from exc
        if key in known:
            return False

        from ..event_logger import log_durable_event_sync

        try:
            log_durable_event_sync(
                "unsupported_operation",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
                operation=operation,
                description=description,
                attempted_operations=attempted_operations,
                scope_id=scope.scope_id,
                operator_visible=True,
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(sorted(known | {key}), separators=(",", ":")),
                encoding="utf-8",
            )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(state_path)
            directory_fd = os.open(state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException("unsupported operation escalation could not be persisted") from exc
    return True


@tool
def unsupported_operation(
    repository: str,
    pull_request: int,
    description: Any = None,
    attempted_operations: Any = None,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Escalate a bound-PR need in prose, including operations already attempted."""
    scope = _scope(runtime, repository, pull_request)
    safe_description = _bounded_escalation_text(
        description,
        fallback="The caller did not provide a description of the unsupported operation.",
        max_bytes=_ESCALATION_DESCRIPTION_MAX_BYTES,
    )
    safe_attempts = _normalize_attempts(attempted_operations)
    operation = _operation_key(safe_description)
    emitted = _emit_unsupported(scope, operation, safe_description, safe_attempts)
    return {
        "status": "unsupported_operation",
        "escalated": emitted,
        "repository": scope.canonical_repo,
        "pull_request": scope.pr_number,
        "operation": operation,
        "description": safe_description,
        "attempted_operations": safe_attempts,
    }


def _bind_injected_runtime(forge_tool: StructuredTool) -> StructuredTool:
    """Bind runtime before LangChain caches its raw callable annotation."""
    if forge_tool.func is None:
        raise RuntimeError(f"forge tool {forge_tool.name!r} has no sync callable")
    forge_tool.func.__annotations__["runtime"] = ToolRuntime
    forge_tool.args_schema = create_schema_from_function(
        forge_tool.name,
        forge_tool.func,
        filter_args=(),
        include_injected=True,
    )
    forge_tool.__dict__.pop("_injected_args_keys", None)
    return forge_tool


FORGE_TOOLS = tuple(_bind_injected_runtime(forge_tool) for forge_tool in (
    pr_metadata,
    pr_files,
    pr_diff,
    pr_checks,
    pr_reviews,
    pr_comments,
    pr_review_requests,
    pr_submit_review,
    pr_inline_review_comment,
    pr_comment,
    issue_comment,
    pr_rerequest_review,
    unsupported_operation,
))
