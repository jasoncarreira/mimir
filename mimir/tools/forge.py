"""Closed, scope-bound repository and pull-request tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import asdict
from datetime import date, datetime, time, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function
from pydantic import StrictInt

from ..forge import ForgeClient, ForgeError, IssueTarget, PullRequestProjection, ReviewVerdict
from ..github_withhold import WithheldItem, partition, placeholder, sanitize_login, sanitize_url, summarise
from ..redaction import redact_text
from ..models import (
    AuthContext, RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState,
    ServerDiscoveredPRScopeStore, ServerDiscoveredPRStates,
)
from .refusals import ToolPolicyRefusal

log = logging.getLogger(__name__)

_BODY_MAX_BYTES = 65_536
_PATH_MAX_BYTES = 4_096
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_BOT_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}\[bot\]", re.I)
_DIAGNOSTIC_LOGIN = re.compile(r"(?:[A-Za-z0-9_.-]{1,100}|[A-Za-z0-9-]{1,39}\[bot\])", re.I)
_CHECK_NAME = re.compile(r"[A-Za-z0-9 _./()\[\]-]{1,64}")
_CHECK_STATUSES = frozenset({"queued", "in_progress", "completed", "waiting", "pending", "requested"})
_CHECK_CONCLUSIONS = frozenset({
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out",
    "action_required", "startup_failure", "stale",
})
_ESCALATION_DESCRIPTION_MAX_BYTES = 4_096
_ESCALATION_ATTEMPT_MAX_BYTES = 512
_ESCALATION_MAX_ATTEMPTS = 16
_SCOPE_REFUSAL_TARGET_LIMIT = 5
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


def _scope_miss_refusal(
    cache: Any,
    registry: Any,
    repository: str,
    pull_request: int,
) -> str | None:
    states: tuple[RepoReviewState, ...] = ()
    if isinstance(cache, ServerDiscoveredPRStates):
        states = cache.review_states
    if isinstance(registry, RepoPRScopeRegistry):
        states += registry.review_states

    targets = tuple(dict.fromkeys(
        (state.repo.lower(), state.pr_number) for state in states
    ))
    if not targets:
        return None

    shown = targets[:_SCOPE_REFUSAL_TARGET_LIMIT]
    rendered = "; ".join(
        f"repository={json.dumps(repo)}, pull_request={number}"
        for repo, number in shown
    )
    if len(targets) > len(shown):
        rendered += (
            f"; [{len(shown)} of {len(targets)} shown; "
            f"{len(targets) - len(shown)} more]"
        )
    return (
        "pull-request operation rejected: requested "
        f"repository={json.dumps(repository)}, pull_request={pull_request} is outside "
        f"this turn's scope; in-scope targets: {rendered}"
    )


def resolve_review_state_for_context(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    """Context-level variant used by authorization before tool invocation."""
    from ..access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    service = get_trusted_service_from_auth_context(context)
    if heartbeat_git_authority_enabled(service) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        return _resolve_heartbeat_git_state(context, repository, pull_request)
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
        can_resolve_forge_review_scope,
        create_server_discovered_review_scope,
        is_configured_github_repo,
        resolve_server_discovered_review_scope,
    )

    if not is_configured_github_repo(repository):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: repository is not configured in GITHUB_REPOS"
        )
    refusal = cache.refusal(repository, pull_request) if isinstance(
        cache, ServerDiscoveredPRStates,
    ) else None
    if refusal is not None:
        raise ToolPolicyRefusal(refusal)
    store = getattr(context, "server_discovered_pr_scope_store", None)
    stored_scope = store.resolve(repository, pull_request) if (
        isinstance(store, ServerDiscoveredPRScopeStore)
        and context is not None
        and can_resolve_forge_review_scope(context, stage="stored")
    ) else None
    snapshot = None
    if stored_scope is not None:
        client = _client_for_repository(repository)
        try:
            snapshot = client.get_pull_request_snapshot(
                stored_scope.canonical_repo, stored_scope.pr_number,
            )
        except ForgeError as exc:
            raise ToolException(f"pull-request operation rejected: {exc}") from exc
        if (
            snapshot.state == "open"
            and isinstance(snapshot.repo, str)
            and snapshot.repo.lower() == stored_scope.canonical_repo
            and snapshot.number == stored_scope.pr_number
            and snapshot.head_sha.lower() == stored_scope.observed_head_sha
        ):
            reused = RepoReviewState(stored_scope)
            return cache.remember(reused) if isinstance(
                cache, ServerDiscoveredPRStates,
            ) else reused
        store.discard(
            repository, pull_request, expected_scope=stored_scope,
        )
        if isinstance(cache, ServerDiscoveredPRStates):
            cache.remember_escalation_scope(stored_scope)
        cause = None
        if snapshot.state != "open":
            cause = "the pull request is closed"
        elif (
            not isinstance(snapshot.repo, str)
            or snapshot.repo.lower() != stored_scope.canonical_repo
            or snapshot.number != stored_scope.pr_number
        ):
            cause = "the provider repository or pull request number does not match"
        if cause is not None:
            stale_refusal = (
                "pull-request operation rejected: stored server-discovered scope for "
                f"repository={json.dumps(repository)}, pull_request={pull_request} is stale; "
                f"{cause}"
            )
            if isinstance(cache, ServerDiscoveredPRStates):
                cache.remember_refusal(repository, pull_request, stale_refusal)
            raise ToolException(stale_refusal)
    if (
        not can_resolve_forge_review_scope(context, stage="fetch")
    ):
        scope_refusal = _scope_miss_refusal(
            cache, registry, repository, pull_request,
        )
        if scope_refusal is not None:
            if stored_scope is not None:
                scope_refusal += "; head advanced; discovery not permitted for this turn"
            if isinstance(cache, ServerDiscoveredPRStates):
                cache.remember_refusal(
                    repository, pull_request, scope_refusal,
                )
            raise ToolPolicyRefusal(scope_refusal)
        scope_refusal = (
            "pull-request operation rejected: requested "
            f"repository={json.dumps(repository)}, pull_request={pull_request}; "
            "live scope discovery requires an authenticated operator user turn"
        )
        if stored_scope is not None:
            scope_refusal += "; head advanced; discovery not permitted for this turn"
        if isinstance(cache, ServerDiscoveredPRStates):
            cache.remember_refusal(repository, pull_request, scope_refusal)
        raise ToolPolicyRefusal(scope_refusal)
    cached = cache.resolve(repository, pull_request) if cache is not None else None
    if cached is not None:
        return cached
    if snapshot is None:
        client = _client_for_repository(repository)
        try:
            snapshot = client.get_pull_request_snapshot(repository.lower(), pull_request)
        except ForgeError as exc:
            raise ToolException(f"pull-request operation rejected: {exc}") from exc
    self_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if (
        not can_resolve_forge_review_scope(
            context,
            stage="accept",
            pr_author=snapshot.author,
            self_login=self_login,
        )
    ):
        scope_refusal = (
            "pull-request operation rejected: requested "
            f"repository={json.dumps(repository)}, pull_request={pull_request}; "
            "live scope discovery requires an authenticated operator user turn or a "
            "trusted poller with pr_metadata and a configured MIMIR_GITHUB_SELF_LOGIN; "
            "reviewing another author's pull request also requires an explicit "
            "pr_review_others capability grant"
        )
        if stored_scope is not None:
            scope_refusal += "; head advanced; discovery not permitted for this turn"
        if isinstance(cache, ServerDiscoveredPRStates):
            cache.remember_refusal(repository, pull_request, scope_refusal)
        raise ToolPolicyRefusal(scope_refusal)
    review_state = None
    if snapshot.state == "open" and snapshot.author == self_login:
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
    verdict = (
        True if snapshot.author == self_login else
        _author_verdict(context, snapshot.repo, snapshot.author, client)
        if isinstance(snapshot.author, str) and snapshot.author else None
    )
    resolution = resolve_server_discovered_review_scope(
        snapshot.repo, snapshot, review_state=review_state,
        pr_author_is_trusted=verdict,
    )
    scope = resolution.scope
    if (
        scope is None
        or scope.canonical_repo != repository.lower()
        or scope.pr_number != pull_request
    ):
        raise ToolException(resolution.refusal_reason or (
            "pull-request operation rejected: live pull request is closed or invalid"
        ))
    state = RepoReviewState(scope)
    if snapshot.author != self_login:
        from ..access_control import get_trusted_service_from_auth_context
        from ..event_logger import log_event_sync

        service = get_trusted_service_from_auth_context(context)
        if service is not None and service.has_capability("pr_review_others"):
            log_event_sync(
                "forge_review_others_scope_resolved",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
                capability="pr_review_others",
                author=snapshot.author,
            )
    store = getattr(context, "server_discovered_pr_scope_store", None)
    if isinstance(store, ServerDiscoveredPRScopeStore):
        store.remember_server_discovery(scope)
    return cache.remember(state) if cache is not None else state


def _resolve_heartbeat_git_state(
    context: AuthContext,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    """Live own-PR authority; the operator's cross-turn store is not a grant."""
    from ..access_control import (
        can_resolve_forge_review_scope,
        create_server_discovered_heartbeat_scope,
        heartbeat_cached_scope_refusal,
        is_configured_github_repo,
    )

    if not is_configured_github_repo(repository):
        raise ToolPolicyRefusal("heartbeat_repository_denied: repository is not configured")
    if type(pull_request) is not int or pull_request < 1:
        raise ToolPolicyRefusal("heartbeat_pr_invalid: pull_request must be a positive integer")
    login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if not login:
        raise ToolPolicyRefusal("heartbeat_identity_missing: MIMIR_GITHUB_SELF_LOGIN is empty")
    if not can_resolve_forge_review_scope(context, stage="fetch"):
        raise ToolPolicyRefusal("heartbeat_authority_denied: trusted pr_metadata capability required")
    cache = context.server_discovered_pr_states
    registry = context.repo_pr_scope_registry
    existing = cache.resolve(repository, pull_request) if cache is not None else None
    if existing is None and registry is not None:
        existing = registry.resolve(repository, pull_request)
    if existing is not None and (
        existing.action_scope.pull_request_author != login
        or existing.action_scope.principal != login
    ):
        raise ToolPolicyRefusal("heartbeat_other_author: cached scope is not authored by the configured identity")
    try:
        snapshot = _client_for_repository(repository).get_pull_request_snapshot(
            repository.lower(), pull_request,
        )
    except ForgeError as exc:
        raise ToolException(f"heartbeat_pr_unverified: {exc}") from exc
    from ..models import NormalizedPullRequestSnapshot

    if (
        not isinstance(snapshot, NormalizedPullRequestSnapshot)
        or not isinstance(snapshot.repo, str)
        or snapshot.repo.lower() != repository.lower()
        or type(snapshot.number) is not int
        or snapshot.number != pull_request
        or snapshot.state != "open"
    ):
        raise ToolPolicyRefusal("heartbeat_pr_invalid: provider must report the requested open PR")
    if snapshot.author != login:
        raise ToolPolicyRefusal("heartbeat_other_author: PR is not authored by the configured identity")
    scope = create_server_discovered_heartbeat_scope(
        repository, snapshot, event_type="heartbeat_pr_maintenance",
    )
    if scope is None:
        raise ToolPolicyRefusal("heartbeat_pr_invalid: provider refs or configured repository binding are invalid")
    if existing is not None:
        # Never re-pin a live checkout after the provider advances. Git publication
        # also checks the remote ref, closing the race after this API observation.
        refusal = heartbeat_cached_scope_refusal(existing.action_scope, scope)
        if refusal is not None:
            raise ToolPolicyRefusal(refusal)
        return existing
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
        _publish_scoped_error(SimpleNamespace(context=context), scope)
        raise ToolException(f"pull-request operation rejected: {exc}") from exc
    if (
        not isinstance(snapshot.repo, str)
        or snapshot.repo.lower() != scope.canonical_repo
        or snapshot.number != scope.pr_number
        or snapshot.state != "open"
        or snapshot.head_sha.lower() != scope.observed_head_sha
    ):
        raise ToolException(
            "pull-request operation rejected: pull request head advanced after scope issuance"
        )


def remediation_checkout_preflight(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> tuple[RepoReviewState | None, str | None]:
    """Refresh one stale own-remediation scope from a live provider snapshot."""
    from ..access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    if heartbeat_git_authority_enabled(get_trusted_service_from_auth_context(context)) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        return resolve_review_state_for_context(context, repository, pull_request), None
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
        _publish_scoped_error(SimpleNamespace(context=context), scope)
        raise ToolException(f"repository checkout rejected: {exc}") from exc
    if snapshot.state != "open":
        return None, "pull request is closed or merged"
    if scope.event_type == "pr_ci_failure":
        fresh = None
        if snapshot.head_sha.lower() != scope.observed_head_sha.lower():
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
                return None, "pull request head was superseded"
            fresh = RepoReviewState(fresh_scope)
            scope = fresh_scope
        try:
            checks = client.list_checks(scope)
        except ForgeError as exc:
            _publish_scoped_error(SimpleNamespace(context=context), scope)
            raise ToolException(f"repository checkout rejected: {exc}") from exc
        if not any(
            check.status == "completed"
            and check.conclusion in {
                "failure", "timed_out", "startup_failure", "action_required",
            }
            for check in checks
        ):
            return None, "pull request checks are no longer failing"
        if fresh is not None:
            reminted = cache.remember_remint(original, fresh)
            _seed_verified_remint(context, original.action_scope, reminted.action_scope)
            return reminted, None
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
    reminted = cache.remember_remint(original, fresh)
    _seed_verified_remint(context, original.action_scope, reminted.action_scope)
    return reminted, None


def _seed_verified_remint(
    context: AuthContext | None, old: RepoPRActionScope, new: RepoPRActionScope,
) -> None:
    from ..repo_tools import was_verified_push

    if (
        context is not None
        and context.ifc_state is not None
        and old.canonical_repo.casefold() == new.canonical_repo.casefold()
        and old.pr_number == new.pr_number
        and was_verified_push(
            old.canonical_repo, old.pr_number, old.observed_head_sha, new.observed_head_sha,
        )
    ):
        context.ifc_state.record_own_push(
            old.canonical_repo, old.pr_number, old.observed_head_sha,
        )


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


def _branch(value: str, name: str, *, qualified: bool = False) -> str:
    branch = value
    if qualified and isinstance(value, str) and ":" in value:
        owner, separator, branch = value.partition(":")
        if _REVIEWER.fullmatch(owner) is None or not separator:
            raise ToolPolicyRefusal(f"{name} must be a valid branch name")
    if (
        not isinstance(value, str) or not branch or branch.startswith("-") or ":" in branch
        or len(value.encode("utf-8")) > 255 or ".." in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ToolPolicyRefusal(f"{name} must be a valid branch name within 255 UTF-8 bytes")
    return value


def _merged_since(value: str) -> datetime:
    if not isinstance(value, str):
        raise ToolPolicyRefusal("merged_since must be an ISO-8601 date or datetime")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.combine(date.fromisoformat(value), time.min)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if "T" not in value and " " not in value:
                raise ValueError("not a datetime")
    except ValueError as exc:
        raise ToolPolicyRefusal("merged_since must be an ISO-8601 date or datetime") from exc
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _pr_search(value: str) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or len(value.encode("utf-8")) > 256
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ToolPolicyRefusal("search must be non-empty text within 256 UTF-8 bytes without control characters")
    if any(forbidden in value.casefold() for forbidden in ("repo:", "org:", "user:", "is:issue")):
        raise ToolPolicyRefusal("search cannot contain repo:, org:, user:, or is:issue")
    # Appending repo: is not confinement if boolean syntax or an open quote
    # can change how GitHub's advanced search parser binds those qualifiers.
    if any(char in value for char in '\"()') or any(
        token.casefold() in {"or", "and", "not"} for token in value.split()
    ):
        raise ToolPolicyRefusal("search cannot contain quotes, parentheses, or boolean operators")
    fixed = {
        "in:title", "in:body", "in:comments", "is:open", "is:closed",
        "is:merged", "is:unmerged", "is:draft",
    }
    for token in value.split():
        if ":" not in token or token in fixed:
            continue
        qualifier, _, argument = token.partition(":")
        if qualifier in {"head", "base"}:
            _branch(argument, qualifier)
        elif qualifier == "author" and _REVIEWER.fullmatch(argument) is not None:
            continue
        elif qualifier == "label" and 0 < len(argument) <= 50 and argument.isprintable() and ":" not in argument:
            continue
        else:
            raise ToolPolicyRefusal("search contains an unsupported qualifier")
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


def _publish_scoped_error(runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope | None) -> None:
    """Attest server error text only after an exact own-repository PR is known."""
    if scope is None:
        return
    from ..access_control import _github_repo_from_remote

    context = getattr(runtime, "context", None)
    author = scope.pull_request_author
    if (
        context is None or context.ifc_state is None or not author
        or scope.head_repo != scope.canonical_repo
        or scope.head_remote != "origin"
        or _github_repo_from_remote(scope.canonical_origin) != scope.canonical_repo
        or scope.checkout_ref not in (None, f"refs/pull/{scope.pr_number}/head")
    ):
        return
    try:
        if _author_verdict(context, scope, author, _client(scope)) is True:
            _publish_author_attestation(runtime, scope, (author,), "forge_error")
    except ForgeError:
        return


def _call(operation: Any, *, runtime: ToolRuntime[AuthContext] | None = None,
          scope: RepoPRActionScope | None = None) -> Any:
    try:
        return operation()
    except ForgeError as exc:
        from ..forge.github import GitHubIdentityVerificationError

        from ..forge.client import ForgeReadUnavailable

        if isinstance(exc, ForgeReadUnavailable):
            # A typed, fixed diagnostic contains no third-party content. Do
            # not extend this exemption to arbitrary adapter error strings.
            raise ToolPolicyRefusal(str(exc)) from exc
        if isinstance(exc, GitHubIdentityVerificationError):
            _latch_github_identity_degraded(exc)
            raise ToolException(
                f"coding capability disabled: GitHub identity verification failed: {exc}"
            ) from exc
        # The adapter may have contacted the forge before failing, so this is a
        # fault rather than a proven pre-execution policy refusal.
        _publish_scoped_error(runtime, scope)
        raise ToolException(str(exc)) from exc


def _author_verdict(context: AuthContext, repo: str | RepoPRActionScope, author: str, client: Any) -> bool | None:
    from ..config import trusted_github_bot_logins

    repository = repo.canonical_repo if isinstance(repo, RepoPRActionScope) else repo
    if not isinstance(author, str) or not author or context.ifc_state is None:
        return None
    attest = getattr(client, "author_is_trusted", None)
    if _BOT_LOGIN.fullmatch(author):
        # A bot is trusted only by exact operator designation; never ask the
        # collaborator API to upgrade an unlisted app identity.
        return context.ifc_state.repository_author_trust.resolve(
            repository, author,
            lambda: author.casefold() in trusted_github_bot_logins(),
        )
    if not callable(attest):
        return None
    return context.ifc_state.repository_author_trust.resolve(
        repository, author,
        lambda: attest(repository, author),
    )


def _withheld_event(tool: str, kind: str, reason: str, count: int = 1) -> None:
    from ..event_logger import log_event_sync

    try:
        log_event_sync("github_content_withheld", tool=tool, kind=kind, count=count, reason=reason)
    except RuntimeError:
        pass


def _withhold_thread(runtime: ToolRuntime[AuthContext], scope: RepoPRActionScope,
                     items: Any, tool: str) -> list[dict[str, Any]]:
    client = _client(scope)
    context = runtime.context
    projected = [dict(asdict(item), kind=(
        "review" if tool == "pr_reviews" else
        "review_comment" if item.path is not None else "comment"
    ), repository=scope.canonical_repo) for item in items]
    kept, withheld = partition(
        projected, lambda item: item["author"],
        lambda author: _author_verdict(context, scope, author, client),
    )
    _publish_author_attestation(runtime, scope, tuple(item["author"] for item in kept), tool)
    for item in withheld:
        _withheld_event(tool, item.kind, item.reason)
    result = [{key: value for key, value in item.items() if key not in {"kind", "repository"}}
              for item in kept]
    if len(withheld) > 20:
        result.append(summarise(withheld))
    else:
        result.extend(placeholder(item) for item in withheld)
    return result


def _publish_trusted_projection(runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope) -> None:
    """Independently attest a validated server-owned projection at its scope.

    Never depend on another parallel read warming the author cache. These
    bounded projections belong to the authorized scope, not a later live head;
    _pr_content_authors still rejects an unrelated live-head change.
    """
    client = _client(scope)
    if not callable(getattr(client, "author_is_trusted", None)):
        return
    authors, _head_sha = _call(lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope)
    _publish_author_attestation(
        runtime, scope, authors, "forge_projection", head_sha=scope.observed_head_sha,
    )


def _publish_write_projection(runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope) -> None:
    """Best-effort attestation must not turn a completed write into a retry.

    On adapter/read failure publish no trusted provenance: result classification
    retains its default untrusted label. Never reuse cached trust as a fallback.
    """
    try:
        _publish_trusted_projection(runtime, scope)
    except (ForgeError, ToolException):
        # Do not expose arbitrary adapter error text after the write succeeded.
        log.warning("forge write completed; result attestation unavailable")


def _safe_check_projection(check: Any, scope: RepoPRActionScope) -> bool:
    url = check.details_url
    return (
        isinstance(check.status, str) and check.status in _CHECK_STATUSES
        and (check.conclusion is None or isinstance(check.conclusion, str)
             and check.conclusion in _CHECK_CONCLUSIONS)
        and isinstance(check.name, str) and _CHECK_NAME.fullmatch(check.name) is not None
        and (url is None or isinstance(url, str)
             and url.startswith(f"https://github.com/{scope.canonical_repo}/"))
    )


def _publish_author_attestation(
    runtime: ToolRuntime[AuthContext] | None,
    scope: RepoPRActionScope,
    authors: tuple[str, ...],
    tool: str,
    head_sha: str | None = None,
) -> None:
    """Publish provenance only for authors obtained from native forge projections.

    Missing actors/adapters and unavailable attestation fail closed for this
    result. Only definitive verdicts enter the turn-local cache; no PR-level
    verdict is persisted. Contained ``repo_test`` output is a function of the
    attested checked-in checkout and inherits its lease attestation. Scoped CI
    job logs inherit the PR author verdict; checks and forge mutation output
    have separate provenance rules.
    """
    from ..access_control import publish_protected_result
    from ..models import SourceLabel

    context = getattr(runtime, "context", None)
    if context is None:
        return
    client = _client(scope)
    if not callable(getattr(client, "author_is_trusted", None)) and not any(
        isinstance(author, str) and _BOT_LOGIN.fullmatch(author) for author in authors
    ):
        return
    trusted = True
    failed_authors = []
    unavailable_authors = []
    for author in dict.fromkeys(authors):
        if not isinstance(author, str) or not author or author == "<head-mismatch>":
            trusted = False
            failed_authors.append(
                "<head-mismatch>" if author == "<head-mismatch>" else "<missing-author>"
            )
            continue
        verdict = _author_verdict(context, scope, author, client)
        trusted = trusted and verdict is True
        # Only identifiers, never arbitrary adapter strings, enter diagnostics.
        diagnostic_author = author if _DIAGNOSTIC_LOGIN.fullmatch(author) else "<invalid-author>"
        if verdict is not True:
            failed_authors.append(diagnostic_author)
        if verdict is None:
            unavailable_authors.append(diagnostic_author)
            context.ifc_state.record_author_attestation_unavailable()
    principal = context.canonical_principal
    if context.is_service and principal:
        principal = f"service:{principal}"
    resource_id = f"{scope.canonical_repo}#pull/{scope.pr_number}@{head_sha or scope.observed_head_sha}"
    publish_protected_result((SourceLabel(
        principal=principal, domain="repository",
        resource_id=resource_id,
        bridge_instance="forge", sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool", integrity="trusted" if trusted else "untrusted",
        integrity_effect="active_ingest",
    ),))
    if not trusted:
        try:
            from .._context import get_current_turn
            from ..event_logger import log_event_sync

            turn = get_current_turn()
            log_event_sync(
                "forge_author_attestation_downgraded",
                session_id=context.channel_id,
                turn_id=turn.turn_id if turn is not None else None,
                repository=scope.canonical_repo,
                resource_id=resource_id,
                tool=tool,
                integrity="untrusted", integrity_effect="active_ingest",
                failed_authors=failed_authors,
                unavailable_authors=unavailable_authors,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must not alter the read
            # Observability must not turn a successful read into a tool failure.
            log.warning(
                "forge_author_attestation_downgraded: repository=%s resource_id=%s "
                "tool=%s failed_authors=%s unavailable_authors=%s "
                "(event logging failed: %s)",
                scope.canonical_repo,
                resource_id,
                tool, failed_authors, unavailable_authors, exc,
            )


def _pr_attestation(
    metadata: PullRequestProjection,
    scope: RepoPRActionScope,
    runtime: ToolRuntime[AuthContext] | None,
) -> tuple[tuple[str, ...], str]:
    """Bind PR-owned text to the observed head or an exact verified own push."""
    if metadata.number != scope.pr_number:
        return ("",), scope.observed_head_sha
    if metadata.head_sha != scope.observed_head_sha:
        # Runtime-less callers (notably checkout) retain observed-head-only
        # trust; accepting a new head requires per-turn lineage recording.
        if runtime is None:
            return ("<head-mismatch>",), scope.observed_head_sha
        from ..repo_tools import was_verified_push

        if not was_verified_push(
            scope.canonical_repo, scope.pr_number, scope.observed_head_sha, metadata.head_sha,
        ):
            return ("<head-mismatch>",), scope.observed_head_sha
        context = getattr(runtime, "context", None)
        if context is not None and context.ifc_state is not None:
            context.ifc_state.record_own_push(
                scope.canonical_repo, scope.pr_number, scope.observed_head_sha,
            )
    return (metadata.author,), metadata.head_sha


def _pr_content_authors(
    client: ForgeClient, scope: RepoPRActionScope,
    runtime: ToolRuntime[AuthContext] | None = None,
) -> tuple[tuple[str, ...], str]:
    """Bind PR-owned diff/file text to API authorship at its verified head."""
    return _pr_attestation(client.get_pull_request(scope), scope, runtime)


@tool
def pr_list(
    repository: str,
    state: str = "open",
    author: str | None = None,
    base: str | None = None,
    head: str | None = None,
    merged_since: str | None = None,
    limit: StrictInt = 30,
    search: str | None = None,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded, untrusted PRs in a configured repository. Search results omit
    head_ref, base_ref and head_sha (empty strings); use pr_metadata for those fields.
    """
    repo = _repository(repository)
    from ..access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("pull-request list rejected: repository is not configured in GITHUB_REPOS")
    if state not in {"open", "closed", "merged", "all"}:
        raise ToolPolicyRefusal("state must be open, closed, merged, or all")
    if author is not None and (not isinstance(author, str) or _REVIEWER.fullmatch(author) is None):
        raise ToolPolicyRefusal("author must be a valid GitHub login")
    if base is not None:
        _branch(base, "base")
    if head is not None:
        _branch(head, "head", qualified=True)
    since = _merged_since(merged_since) if merged_since is not None else None
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolPolicyRefusal("limit must be an integer from 1 through 100")
    query = _pr_search(search) if search is not None else None
    client = _client_for_repository(repo)
    if query is None:
        items = _call(lambda: client.list_pull_requests(
            repo, state=state, base=base, head=head, limit=limit,
            author=author, merged_since=since,
        ))
    else:
        items = _call(lambda: client.search_pull_requests(
            repo, query=query, state=state, base=base, head=head, limit=limit,
            author=author, merged_since=since,
        ))
    context = getattr(runtime, "context", None)
    if context is None or context.ifc_state is None:
        # Direct invocations have no turn cache; still attest before projecting.
        def verdict_for(author: str) -> bool | None:
            from ..config import trusted_github_bot_logins

            if not isinstance(author, str) or not author:
                return None
            if _BOT_LOGIN.fullmatch(author):
                return author.casefold() in trusted_github_bot_logins()
            attest = getattr(client, "author_is_trusted", None)
            return attest(repo, author) if callable(attest) else None
    else:
        verdict_for = lambda author: _author_verdict(context, repo, author, client)
    result = []
    trusted = True
    for item in items:
        verdict = verdict_for(item.author)
        if verdict is True:
            result.append(asdict(item))
            continue
        trusted = False
        reason = ("attestation_unavailable" if verdict is None else
                  "bot_not_allowlisted" if item.author.endswith("[bot]") else "non_collaborator")
        result.append({"number": item.number, **placeholder(WithheldItem(
            "pull_request", sanitize_login(item.author), None,
            sanitize_url(item.url, repo), reason,
        ))})
        _withheld_event("pr_list", "pull_request", reason)
    if trusted and context is not None:
        from ..access_control import publish_protected_result
        from ..models import SourceLabel

        principal = context.canonical_principal
        if context.is_service and principal:
            principal = f"service:{principal}"
        publish_protected_result((SourceLabel(
            principal=principal, domain="repository", resource_id=f"{repo.lower()}#pulls",
            bridge_instance="forge", sensitivity="internal",
            authorized_principals=frozenset({principal}) if principal else frozenset(),
            source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
        ),))
    return result


@tool
def pr_metadata(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read metadata for an exact pull request authorized by this turn."""
    scope = _scope(runtime, repository, pull_request)
    metadata = _call(lambda: _client(scope).get_pull_request(scope), runtime=runtime, scope=scope)
    authors, head_sha = _pr_attestation(metadata, scope, runtime)
    _publish_author_attestation(runtime, scope, authors, "pr_metadata", head_sha=head_sha)
    return asdict(metadata)


@tool
def pr_spec(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read the armed Chainlink spec of record bound by Worklink evidence to this PR."""
    scope = _scope(runtime, repository, pull_request)
    from ..worklink.continuation import issue_bound_to_pr
    from ..access_control import publish_protected_result
    from ..models import SourceLabel

    context = getattr(runtime, "context", None)
    if context is not None:
        principal = context.canonical_principal
        if context.is_service and principal:
            principal = f"service:{principal}"
        publish_protected_result((SourceLabel(
            principal=principal, domain="repository",
            resource_id=f"{scope.canonical_repo}#pull/{scope.pr_number}@{scope.observed_head_sha}",
            bridge_instance="forge", sensitivity="internal",
            authorized_principals=frozenset({principal}) if principal else frozenset(),
            source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
        ),))

    home = os.environ.get("MIMIR_HOME", "").strip()
    issue_id = issue_bound_to_pr(Path(home), scope.canonical_repo, scope.pr_number) if home else None
    if issue_id is None:
        return {"error": "no_bound_spec"}
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["chainlink", "issue", "show", str(issue_id), "--json"],
                cwd=home, stdout=output, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            if result.returncode != 0 or output.tell() > 131_072:
                return {"error": "spec_read_failed"}
            output.seek(0)
            payload = json.load(output)
        if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] != issue_id:
            return {"error": "spec_read_failed"}
        labels = payload.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            return {"error": "spec_read_failed"}
        if not any(label.startswith("worklink:") for label in labels):
            return {"error": "spec_not_armed"}
        if not all(isinstance(payload.get(key), str) for key in ("title", "status", "description")):
            return {"error": "spec_read_failed"}
        return {key: payload[key] for key in ("id", "title", "status", "labels", "description")}
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return {"error": "spec_read_failed"}


@tool
def pr_files(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded file projections for the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    items = _call(lambda: client.list_files(scope), runtime=runtime, scope=scope)
    if callable(getattr(client, "author_is_trusted", None)):
        authors, head_sha = _call(lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope)
        _publish_author_attestation(runtime, scope, authors, "pr_files", head_sha=head_sha)
    return [asdict(item) for item in items]


@tool
def pr_diff(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read the bounded unified diff for the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    diff = _call(lambda: client.get_diff(scope), runtime=runtime, scope=scope)
    if callable(getattr(client, "author_is_trusted", None)):
        authors, head_sha = _call(lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope)
        _publish_author_attestation(runtime, scope, authors, "pr_diff", head_sha=head_sha)
    return diff


@tool
def pr_file_content(
    repository: str,
    pull_request: int,
    path: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read one regular file at the pull request's verified head."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    content = _call(lambda: client.get_file_content(scope, path), runtime=runtime, scope=scope)
    if callable(getattr(client, "author_is_trusted", None)):
        authors, head_sha = _call(lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope)
        _publish_author_attestation(runtime, scope, authors, "pr_file_content", head_sha=head_sha)
    return content


@tool
def pr_checks(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded check projections for the bound pull request head."""
    scope = _scope(runtime, repository, pull_request)
    checks = _call(lambda: _client(scope).list_checks(scope), runtime=runtime, scope=scope)
    if all(_safe_check_projection(item, scope) for item in checks):
        _publish_trusted_projection(runtime, scope)
    return [asdict(item) for item in checks]


@tool
def pr_job_log(
    repository: str,
    pull_request: StrictInt,
    job_id: StrictInt,
    run_id: StrictInt | None = None,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read a redacted bounded CI job excerpt; trusted only for an attested PR."""
    _repository(repository)
    for name, value in (("pull_request", pull_request), ("job_id", job_id), ("run_id", run_id)):
        if name == "run_id" and value is None:
            continue
        if type(value) is not int or value < 1:
            raise ToolPolicyRefusal(f"{name} must be a positive integer")
    context = getattr(runtime, "context", None)
    state = None
    for inventory in (
        getattr(context, "server_discovered_pr_states", None),
        getattr(context, "repo_pr_scope_registry", None),
    ):
        if isinstance(inventory, (ServerDiscoveredPRStates, RepoPRScopeRegistry)):
            state = inventory.resolve(repository, pull_request)
            if state is not None:
                break
    if state is None:
        raise ToolPolicyRefusal("job log rejected: pull request is outside this turn's scope")
    from ..access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    if heartbeat_git_authority_enabled(get_trusted_service_from_auth_context(context)) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        state = resolve_review_state_for_context(context, repository, pull_request)
    scope = state.action_scope
    client = _client(scope)
    excerpt = _call(lambda: client.get_job_log(scope, job_id, run_id), runtime=runtime, scope=scope)
    if callable(getattr(client, "author_is_trusted", None)):
        authors, _head_sha = _call(lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope)
        # get_job_log independently pins job/run metadata to this exact head.
        _publish_author_attestation(
            runtime, scope, authors, "pr_job_log", head_sha=scope.observed_head_sha,
        )
    return excerpt


def _attest_ci_runs(runtime: ToolRuntime[AuthContext] | None, repository: str,
                    runs: list[dict[str, Any]], resource_id: str) -> None:
    """Publish only when every returned run belongs to a protected head."""
    from ..access_control import publish_protected_result
    from ..models import SourceLabel

    context = getattr(runtime, "context", None)
    if context is None or not runs or not all(
        _trusted_ci_head(context, repository, run) for run in runs
    ):
        return
    principal = context.canonical_principal
    if context.is_service and principal:
        principal = f"service:{principal}"
    publish_protected_result((SourceLabel(
        principal=principal, domain="repository", resource_id=resource_id,
        bridge_instance="forge", sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
    ),))


def _trusted_ci_head(context: AuthContext, repository: str, run: dict[str, Any]) -> bool:
    from ..repo_tools import _PROTECTED_BRANCH_REFS

    if not isinstance(run, dict):
        return False
    branch, sha = run.get("head_branch"), run.get("head_sha")
    if (
        not isinstance(run.get("head_repository"), str)
        or run["head_repository"].lower() != repository.lower()
        or not isinstance(sha, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", sha) is None
    ):
        return False
    if isinstance(branch, str) and f"refs/heads/{branch}" in _PROTECTED_BRANCH_REFS:
        return True
    for inventory in (context.server_discovered_pr_states, context.repo_pr_scope_registry):
        for state in getattr(inventory, "review_states", ()):
            scope = state.action_scope
            if (
                scope.canonical_repo == repository.lower()
                and scope.head_repo == repository.lower()
                and scope.head_remote == "origin"
                and _github_origin_matches(scope)
                and scope.head_ref == branch and scope.observed_head_sha == sha
                and context.ifc_state is not None
                and context.ifc_state.pr_checkout_author_trust.get(scope.scope_id) is True
            ):
                return True
    return False


def _github_origin_matches(scope: RepoPRActionScope) -> bool:
    from ..access_control import _github_repo_from_remote

    return _github_repo_from_remote(scope.canonical_origin) == scope.canonical_repo


@tool
def ci_run_jobs(
    repository: str,
    run_id: StrictInt,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Read bounded jobs and failed steps for a configured, poller-named CI run."""
    repo = _repository(repository)
    from ..access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("CI run rejected: repository is not configured in GITHUB_REPOS")
    context = getattr(runtime, "context", None)
    if type(run_id) is not int or run_id < 1 or (repo.lower(), run_id) not in getattr(
        context, "ci_run_targets", frozenset(),
    ):
        raise ToolPolicyRefusal("CI run rejected: run is outside this turn's poller scope")
    client = _client_for_repository(repo)
    run = _call(lambda: client.get_run(repo, run_id))
    if isinstance(run, dict) and run.get("id") == run_id:
        _attest_ci_runs(runtime, repo, [run], f"{repo.lower()}#actions/run/{run_id}/jobs")
    return _call(lambda: client.list_run_jobs(repo, run_id))


@tool
def ci_run(
    repository: str,
    run_id: StrictInt,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read bounded metadata for a configured, poller-named CI run."""
    repo = _repository(repository)
    from ..access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("CI run rejected: repository is not configured in GITHUB_REPOS")
    context = getattr(runtime, "context", None)
    if type(run_id) is not int or run_id < 1 or (repo.lower(), run_id) not in getattr(
        context, "ci_run_targets", frozenset(),
    ):
        raise ToolPolicyRefusal("CI run rejected: run is outside this turn's poller scope")
    run = _call(lambda: _client_for_repository(repo).get_run(repo, run_id))
    if isinstance(run, dict) and run.get("id") == run_id:
        _attest_ci_runs(runtime, repo, [run], f"{repo.lower()}#actions/run/{run_id}")
    return run


@tool
def ci_recent_runs(
    repository: str,
    branch: str,
    workflow_id: StrictInt | None = None,
    limit: StrictInt = 10,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Read bounded recent CI runs on the poller-named branch."""
    repo = _repository(repository)
    from ..access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("CI runs rejected: repository is not configured in GITHUB_REPOS")
    context = getattr(runtime, "context", None)
    targets = getattr(context, "ci_branch_targets", frozenset())
    if (
        not isinstance(branch, str) or not branch
        or (workflow_id is not None and (type(workflow_id) is not int or workflow_id < 1))
        or not any(
            target_repo == repo.lower() and target_branch == branch
            and (workflow_id is None or workflow_id == target_workflow)
            for target_repo, target_branch, target_workflow in targets
        )
    ):
        raise ToolPolicyRefusal("CI runs rejected: branch or workflow is outside this turn's poller scope")
    if type(limit) is not int:
        raise ToolPolicyRefusal("CI runs rejected: limit must be an integer")
    bounded_limit = max(1, min(20, limit))
    runs = _call(lambda: _client_for_repository(repo).list_runs(repo, branch, workflow_id, bounded_limit))
    if isinstance(runs, list) and all(
        isinstance(run, dict) and run.get("head_branch") == branch
        and (workflow_id is None or run.get("workflow_id") == workflow_id)
        for run in runs
    ):
        _attest_ci_runs(runtime, repo, runs, f"{repo.lower()}#actions/runs?branch={branch}"
                        + (f"&workflow={workflow_id}" if workflow_id is not None else ""))
    return runs


@tool
def pr_reviews(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded submitted-review projections for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    items = _call(lambda: _client(scope).list_reviews(scope), runtime=runtime, scope=scope)
    return _withhold_thread(runtime, scope, items, "pr_reviews")


@tool
def pr_comments(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded conversation and inline comments for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    items = _call(lambda: _client(scope).list_comments(scope), runtime=runtime, scope=scope)
    return _withhold_thread(runtime, scope, items, "pr_comments")


@tool
def pr_review_requests(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded pending review requests for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    items = _call(lambda: _client(scope).list_review_requests(scope), runtime=runtime, scope=scope)
    if all(
        item.kind in {"user", "team"} and isinstance(item.reviewer, str)
        and _REVIEWER.fullmatch(item.reviewer) is not None for item in items
    ):
        _publish_trusted_projection(runtime, scope)
    return [asdict(item) for item in items]


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
    result = asdict(_call(lambda: _client(scope).submit_review(scope, verdict, safe_body)))
    _publish_write_projection(runtime, scope)
    return result


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
    result = asdict(_call(lambda: _client(scope).add_inline_review_comment(
        scope, path=safe_path, line=line, body=safe_body,
    )))
    _publish_write_projection(runtime, scope)
    return result


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
    result = asdict(_call(lambda: _client(scope).add_pull_request_comment(scope, safe_body)))
    _publish_write_projection(runtime, scope)
    return result


@tool
def pr_edit_body(
    repository: str,
    pull_request: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Replace only the bound pull request description with bounded literal text."""
    scope = _scope(runtime, repository, pull_request)
    safe_body = _body(body)
    _call(lambda: _client(scope).edit_pull_request_body(scope, safe_body))
    _publish_write_projection(runtime, scope)
    return {"status": "body_updated"}


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
    context = getattr(runtime, "context", None)
    cache = getattr(context, "server_discovered_pr_states", None)
    state = cache.resolve_for_tool("unsupported_operation", repository, pull_request) if isinstance(
        cache, ServerDiscoveredPRStates,
    ) else None
    from ..access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    if heartbeat_git_authority_enabled(get_trusted_service_from_auth_context(context)) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        state = resolve_review_state_for_context(context, repository, pull_request)
    scope = state.action_scope if state is not None else _scope(runtime, repository, pull_request)
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
    sync_function = forge_tool.func

    @wraps(sync_function)
    async def off_loop(*args: Any, **kwargs: Any) -> Any:
        # Explicitly offload both the forge transport and author attestation.
        # to_thread propagates the exact call's protected-provenance capture.
        return await asyncio.to_thread(sync_function, *args, **kwargs)

    forge_tool.coroutine = off_loop
    forge_tool.args_schema = create_schema_from_function(
        forge_tool.name,
        forge_tool.func,
        filter_args=(),
        include_injected=True,
    )
    forge_tool.__dict__.pop("_injected_args_keys", None)
    return forge_tool


FORGE_TOOLS = tuple(_bind_injected_runtime(forge_tool) for forge_tool in (
    pr_list,
    pr_metadata,
    pr_spec,
    pr_files,
    pr_diff,
    pr_file_content,
    pr_checks,
    pr_job_log,
    ci_run_jobs,
    ci_run,
    ci_recent_runs,
    pr_reviews,
    pr_comments,
    pr_review_requests,
    pr_submit_review,
    pr_inline_review_comment,
    pr_comment,
    pr_edit_body,
    issue_comment,
    pr_rerequest_review,
    unsupported_operation,
))
