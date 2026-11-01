"""Cultivate's owner-bound repo and pull-request action vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from types import MappingProxyType
from typing import TYPE_CHECKING

from mimir.tool_descriptors import (
    ResultOriginKind, SinkCategory, ToolDescriptor, _argument_payload, _generic_target,
)
from mimir.plugins.registry import TrustedScopeConstructors

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


class RepoPRScopeProvenance(StrEnum):
    POLLER_PAYLOAD = "poller_payload"
    SERVER_DISCOVERED = "server_discovered"


class RepoPRAction(StrEnum):
    INSPECT = "repo.inspect"
    CHECKOUT = "repo.checkout"
    TEST = "repo.test"
    WRITE = "repo.write"
    COMMIT = "repo.commit"
    PUSH = "repo.push"
    PR_COMMENT = "pr.comment"
    PR_EDIT = "pr.edit"
    PR_REREQUEST = "pr.rerequest"
    PR_REVIEW = "pr.review"


@dataclass(frozen=True)
class RepoPRActionScope:
    """Immutable server-issued authority for actions on one observed PR state."""

    provenance: RepoPRScopeProvenance
    canonical_repo: str
    canonical_root: str
    canonical_origin: str
    principal: str
    event_type: str
    allowed_operations: frozenset[str]
    pr_number: int
    head_repo: str
    head_remote: str
    destination_ref: str
    observed_head_sha: str
    base_ref: str
    observed_base_sha: str
    checkout_ref: str | None = None
    pull_request_author: str | None = None
    owner: str = "cultivate"
    scope_id: str = field(init=False)

    def __post_init__(self) -> None:
        provenance = RepoPRScopeProvenance(self.provenance)
        object.__setattr__(self, "provenance", provenance)
        if not isinstance(self.allowed_operations, frozenset):
            raise TypeError("allowed_operations must be a frozenset")
        unknown_actions = self.allowed_operations - frozenset(action.value for action in RepoPRAction)
        if unknown_actions:
            raise ValueError("unsupported repo/PR action")
        authority = {
            "provenance": self.provenance,
            "canonical_repo": self.canonical_repo,
            "canonical_root": self.canonical_root,
            "canonical_origin": self.canonical_origin,
            "principal": self.principal,
            "event_type": self.event_type,
            "allowed_operations": sorted(self.allowed_operations),
            "pr_number": self.pr_number,
            "head_repo": self.head_repo,
            "head_remote": self.head_remote,
            "destination_ref": self.destination_ref,
            "observed_head_sha": self.observed_head_sha,
            "base_ref": self.base_ref,
            "observed_base_sha": self.observed_base_sha,
        }
        if self.checkout_ref is not None:
            authority["checkout_ref"] = self.checkout_ref
        if self.pull_request_author is not None:
            authority["pull_request_author"] = self.pull_request_author
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "scope_id", hashlib.sha256(encoded).hexdigest())

    @property
    def head_ref(self) -> str:
        return self.destination_ref.removeprefix("refs/heads/")


@dataclass(frozen=True)
class RepoReviewState:
    """Immutable PR authority plus typed checkout-lease and execution evidence."""

    action_scope: RepoPRActionScope
    checked_out: bool = field(default=False, init=False, compare=False)
    checkout_lease: object = field(default=None, init=False, repr=False, compare=False)
    git_expected_head: str | None = field(default=None, init=False, repr=False, compare=False)
    full_tested_head: str | None = field(default=None, init=False, repr=False, compare=False)
    conflict_evidence_head: str | None = field(default=None, init=False, repr=False, compare=False)
    _author_attestation_verdicts: dict[tuple[str, str], tuple[bool, str]] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _author_attestation_lock: object = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False,
    )

    @property
    def repo(self) -> str:
        return self.action_scope.canonical_repo

    @property
    def pr_number(self) -> int:
        return self.action_scope.pr_number

    @property
    def head_ref(self) -> str:
        return self.action_scope.head_ref

    @property
    def root(self) -> str:
        lease = self.checkout_lease
        if lease is not None and getattr(lease, "is_active", False):
            return str(lease.path)
        return self.action_scope.canonical_root

    def attach_checkout_lease(self, lease: object) -> None:
        if (
            getattr(lease, "scope_id", None) != self.action_scope.scope_id
            or getattr(lease, "owner", None) != self.action_scope.principal
            or not getattr(lease, "is_active", False)
        ):
            raise ValueError("checkout lease does not match review scope")
        object.__setattr__(self, "checkout_lease", lease)
        object.__setattr__(self, "checked_out", True)
        object.__setattr__(self, "git_expected_head", self.action_scope.observed_head_sha.lower())

    def record_git_head(self, scope_id: str, head: str) -> None:
        lease = self.checkout_lease
        normalized = head.lower()
        if (
            lease is None
            or scope_id != self.action_scope.scope_id
            or getattr(lease, "scope_id", None) != scope_id
            or not getattr(lease, "is_active", False)
            or len(normalized) != 40
            or any(character not in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("Git HEAD update does not match active review scope")
        object.__setattr__(self, "git_expected_head", normalized)
        object.__setattr__(self, "full_tested_head", None)
        object.__setattr__(self, "conflict_evidence_head", None)
        with self._author_attestation_lock:
            self._author_attestation_verdicts.clear()

    def author_attestation_verdict(self, head: str, verify: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
        key = (self.action_scope.scope_id, head.lower())
        with self._author_attestation_lock:
            cached = self._author_attestation_verdicts.get(key)
            if cached is not None:
                return cached
            verdict = verify()
            self._author_attestation_verdicts[key] = verdict
            return verdict

    def record_full_test(self, scope_id: str, head: str) -> None:
        self._record_checkout_proof(scope_id, head, "full_tested_head")

    def record_conflict_evidence(self, scope_id: str, head: str) -> None:
        self._record_checkout_proof(scope_id, head, "conflict_evidence_head")

    def _record_checkout_proof(self, scope_id: str, head: str, field_name: str) -> None:
        lease = self.checkout_lease
        normalized = head.lower()
        if (
            lease is None
            or scope_id != self.action_scope.scope_id
            or getattr(lease, "scope_id", None) != scope_id
            or not getattr(lease, "is_active", False)
            or normalized != self.git_expected_head
        ):
            raise ValueError("checkout proof does not match the current scoped HEAD")
        object.__setattr__(self, field_name, normalized)

    def revoke_checkout_lease(self, lease: object) -> None:
        if self.checkout_lease is lease:
            object.__setattr__(self, "checkout_lease", None)
            object.__setattr__(self, "checked_out", False)
            object.__setattr__(self, "git_expected_head", None)
            object.__setattr__(self, "full_tested_head", None)
            object.__setattr__(self, "conflict_evidence_head", None)


@dataclass(frozen=True)
class RepoPRScopeRegistry:
    """Immutable per-turn registry of independently checked-out PR scopes."""

    review_states: tuple[RepoReviewState, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.review_states, tuple):
            raise TypeError("review_states must be a tuple")
        targets: set[tuple[str, int]] = set()
        for state in self.review_states:
            if not isinstance(state, RepoReviewState):
                raise TypeError("review_states must contain RepoReviewState values")
            target = (state.repo.lower(), state.pr_number)
            if target in targets:
                raise ValueError("duplicate pull-request scope target")
            targets.add(target)

    @property
    def action_scopes(self) -> tuple[RepoPRActionScope, ...]:
        return tuple(state.action_scope for state in self.review_states)

    def resolve(self, repository: object, pull_request: object) -> RepoReviewState | None:
        if (
            not isinstance(repository, str)
            or not repository
            or not isinstance(pull_request, int)
            or isinstance(pull_request, bool)
        ):
            return None
        target = (repository.lower(), pull_request)
        return next(
            (state for state in self.review_states if (state.repo.lower(), state.pr_number) == target),
            None,
        )

    def resolve_checkout_path(self, path: object) -> RepoReviewState | None:
        if not isinstance(path, (str, Path)):
            return None
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            return None
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        for state in self.review_states:
            lease = state.checkout_lease
            if (
                lease is None
                or not getattr(lease, "is_active", False)
                or getattr(lease, "scope_id", None) != state.action_scope.scope_id
                or getattr(lease, "owner", None) != state.action_scope.principal
            ):
                continue
            try:
                lease_root = Path(lease.lease_root).resolve(strict=True)
                checkout = Path(lease.path).resolve(strict=True)
                if checkout.parent != lease_root or checkout == lease_root:
                    continue
                resolved.relative_to(checkout)
            except (OSError, RuntimeError, ValueError):
                continue
            return state
        return None


@dataclass(frozen=True)
class RetainedFactoryScope:
    """Server-derived authority for one retained factory incident."""

    issue_id: int
    signature: str
    occurrence_id: str
    run_id: str
    attempt: int
    session: str
    repository: str
    branch: str
    sandbox: str
    scope_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.issue_id, int)
            or isinstance(self.issue_id, bool)
            or self.issue_id < 1
            or not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < 1
            or any(
                not isinstance(value, str) or not value or "\x00" in value
                for value in (
                    self.signature, self.occurrence_id, self.run_id, self.session,
                    self.repository, self.branch, self.sandbox,
                )
            )
            or not Path(self.sandbox).is_absolute()
        ):
            raise ValueError("retained factory scope identity is invalid")
        authority = {
            "issue_id": self.issue_id, "signature": self.signature,
            "occurrence_id": self.occurrence_id, "run_id": self.run_id,
            "attempt": self.attempt, "session": self.session,
            "repository": self.repository, "branch": self.branch,
            "sandbox": self.sandbox,
        }
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "scope_id", hashlib.sha256(encoded).hexdigest())

    def resolve_path(self, path: object, *, strict: bool = False) -> Path | None:
        if not isinstance(path, (str, Path)):
            return None
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            return None
        try:
            root = Path(self.sandbox).resolve(strict=True)
            resolved = candidate.resolve(strict=strict)
            relative = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved if relative.parts else None


@dataclass
class ServerDiscoveredPRStates:
    """Per-turn cache of review states derived from live provider snapshots."""

    _states: dict[tuple[str, int], RepoReviewState] = field(default_factory=dict, init=False, repr=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)
    _remint_attempts: set[tuple[str, int]] = field(default_factory=set, init=False, repr=False)
    _refusals: dict[tuple[str, int], str] = field(default_factory=dict, init=False, repr=False)
    _escalation_states: dict[tuple[str, int], RepoReviewState] = field(default_factory=dict, init=False, repr=False)

    def remember_escalation_scope(self, scope: RepoPRActionScope) -> None:
        with self._lock:
            self._escalation_states[(scope.canonical_repo, scope.pr_number)] = RepoReviewState(scope)

    def resolve_for_tool(self, tool_name: str, repository: str, pull_request: int) -> RepoReviewState | None:
        state = self.resolve(repository, pull_request)
        if state is None and tool_name == "unsupported_operation":
            with self._lock:
                return self._escalation_states.get((repository.lower(), pull_request))
        return state

    @property
    def review_states(self) -> tuple[RepoReviewState, ...]:
        with self._lock:
            return tuple(self._states.values())

    def resolve(self, repository: str, pull_request: int) -> RepoReviewState | None:
        with self._lock:
            return self._states.get((repository.lower(), pull_request))

    def resolve_checkout_path(self, path: object) -> RepoReviewState | None:
        return RepoPRScopeRegistry(self.review_states).resolve_checkout_path(path)

    def remember(self, state: RepoReviewState) -> RepoReviewState:
        target = (state.repo.lower(), state.pr_number)
        with self._lock:
            return self._states.setdefault(target, state)

    def refusal(self, repository: str, pull_request: int) -> str | None:
        with self._lock:
            return self._refusals.get((repository.lower(), pull_request))

    def remember_refusal(self, repository: str, pull_request: int, refusal: str) -> str:
        target = (repository.lower(), pull_request)
        with self._lock:
            return self._refusals.setdefault(target, refusal)

    def begin_remint(self, repository: str, pull_request: int) -> bool:
        target = (repository.lower(), pull_request)
        with self._lock:
            if target in self._remint_attempts:
                return False
            self._remint_attempts.add(target)
            return True

    def remember_remint(self, original: RepoReviewState, state: RepoReviewState) -> RepoReviewState:
        target = (original.repo.lower(), original.pr_number)
        replacement_target = (state.repo.lower(), state.pr_number)
        if (
            target != replacement_target
            or original.action_scope.event_type not in {"pr_changes_requested_stale", "pr_ci_failure"}
            or state.action_scope.event_type != original.action_scope.event_type
            or state.action_scope.provenance != RepoPRScopeProvenance.SERVER_DISCOVERED
        ):
            raise ValueError("reminted review scope does not match original remediation target")
        with self._lock:
            if target not in self._remint_attempts:
                raise ValueError("reminted review scope was not reserved")
            return self._states.setdefault(target, state)


@dataclass
class ServerDiscoveredPRScopeStore:
    """Agent-owned immutable scope snapshots available to later turns."""

    _scopes: dict[tuple[str, int], RepoPRActionScope] = field(default_factory=dict, init=False, repr=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    def resolve(self, repository: object, pull_request: object) -> RepoPRActionScope | None:
        if (
            not isinstance(repository, str)
            or not repository
            or not isinstance(pull_request, int)
            or isinstance(pull_request, bool)
        ):
            return None
        with self._lock:
            return self._scopes.get((repository.lower(), pull_request))

    def remember_server_discovery(self, scope: RepoPRActionScope) -> None:
        if (
            not isinstance(scope, RepoPRActionScope)
            or scope.provenance != RepoPRScopeProvenance.SERVER_DISCOVERED
        ):
            raise ValueError("only server-discovered pull-request scopes can be stored")
        target = (scope.canonical_repo, scope.pr_number)
        with self._lock:
            self._scopes[target] = scope

    def discard(
        self, repository: str, pull_request: int, *, expected_scope: RepoPRActionScope,
    ) -> None:
        target = (repository.lower(), pull_request)
        with self._lock:
            if self._scopes.get(target) is expected_scope:
                self._scopes.pop(target, None)


_REMEDIATION_ACTIONS = frozenset({
    RepoPRAction.INSPECT.value, RepoPRAction.CHECKOUT.value,
    RepoPRAction.TEST.value, RepoPRAction.WRITE.value,
    RepoPRAction.COMMIT.value, RepoPRAction.PUSH.value,
    RepoPRAction.PR_COMMENT.value, RepoPRAction.PR_EDIT.value,
    RepoPRAction.PR_REREQUEST.value,
})
_REVIEW_ACTIONS = frozenset({
    RepoPRAction.INSPECT.value, RepoPRAction.CHECKOUT.value,
    RepoPRAction.TEST.value, RepoPRAction.PR_REVIEW.value,
    RepoPRAction.PR_COMMENT.value,
})


def issue_repo_scope(*, is_remediation: bool, **fields: object) -> RepoPRActionScope:
    """Select the owner's action vocabulary for validated server observations."""
    event_type = fields.get("event_type")
    allowed = (
        _REMEDIATION_ACTIONS - {RepoPRAction.PR_EDIT.value, RepoPRAction.PR_REREQUEST.value}
        if event_type == "pr_ci_failure"
        else _REMEDIATION_ACTIONS if is_remediation else _REVIEW_ACTIONS
    )
    return RepoPRActionScope(allowed_operations=frozenset(allowed), **fields)


@dataclass(frozen=True)
class CultivateState:
    """Owner-scoped turn state; these references alone confer no authority."""

    repo_pr_scope_registry: RepoPRScopeRegistry | None = None
    repo_review_state: RepoReviewState | None = None
    repo_pr_action_scope: RepoPRActionScope | None = None
    server_discovered_pr_states: ServerDiscoveredPRStates | None = None
    server_discovered_pr_scope_store: ServerDiscoveredPRScopeStore | None = None
    retained_factory_scope: RetainedFactoryScope | None = None
    retained_factory_scope_refusal: str | None = None
    ci_run_targets: frozenset[tuple[str, int]] = frozenset()
    ci_branch_targets: frozenset[tuple[str, str, int | None]] = frozenset()
    tracker_issue_targets: frozenset[tuple[str, str, str]] = frozenset()

    @classmethod
    def project_trusted_auth(cls, auth: object) -> CultivateState | None:
        """Copy only host-issued ingress fields, never plugin or tool arguments."""
        values = {
            name: object.__getattribute__(auth, name)
            for name in cls.__dataclass_fields__
        }
        service = object.__getattribute__(auth, "service_authority")
        if (
            object.__getattribute__(auth, "trigger") == "scheduled_tick"
            and getattr(service, "authority_profile", None) not in {"github", "heartbeat"}
            and values["repo_pr_scope_registry"] is None
            and values["repo_review_state"] is None
            and values["repo_pr_action_scope"] is None
            and not getattr(values["server_discovered_pr_states"], "review_states", ())
            and values["retained_factory_scope"] is None
            and not values["ci_run_targets"]
            and not values["ci_branch_targets"]
            and not values["tracker_issue_targets"]
        ):
            return None
        return cls(**values)


def register_action_protocol(
    registrar: PolicyRegistrar, validate_scope: Callable[..., bool], *,
    checkout_tool: str | None = None,
) -> None:
    """Bind the vocabulary and grant validator to this selected registrar."""
    registrar.register_scoped_action(
        RepoPRAction, validate_scope, checkout_action=RepoPRAction.CHECKOUT,
        live_lease=_live_scoped_pr_lease, checkout_tool=checkout_tool,
        trusted_scope=TrustedScopeConstructors(
            scope_type=RepoPRActionScope,
            issue=issue_repo_scope, review=RepoReviewState,
            registry=RepoPRScopeRegistry, discovered=ServerDiscoveredPRStates,
            store=ServerDiscoveredPRScopeStore, retained=RetainedFactoryScope,
        ),
    )


def _live_scoped_pr_lease(scope: object, review_state: object) -> object | None:
    attached = getattr(review_state, "checkout_lease", None)
    if attached is None or not getattr(attached, "is_active", False):
        return None
    try:
        root = Path(os.environ["MIMIR_PR_CHECKOUT_LEASE_ROOT"])
        if not root.is_absolute() or root.is_symlink():
            return None
        root = root.resolve(strict=True)
        path = Path(attached.path)
        if path.is_symlink() or path.resolve(strict=True).parent != root:
            return None
        metadata = path / ".git/mimir-pr-checkout-lease.json"
        if (metadata.is_symlink() or metadata.parent.is_symlink()
                or (path / ".git/mimir-pr-checkout-lease-reclamation.json").exists()):
            return None
        raw = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if any(raw.get(field) != str(getattr(attached, field)) for field in ("path", "lease_root")):
            return None
        if any(raw.get(field) != getattr(attached, field) for field in (
            "recovery_id", "scope_id", "owner", "head_sha", "canonical_origin",
            "canonical_repo", "pr_number", "destination_ref",
        )):
            return None
        expires = datetime.fromisoformat(raw["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(UTC) or expires != attached.expires_at:
            return None
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None
    if (attached.scope_id != scope.scope_id or attached.owner != scope.principal
            or attached.canonical_repo.lower() != scope.canonical_repo.lower()
            or attached.pr_number != scope.pr_number
            or attached.destination_ref != scope.destination_ref
            or getattr(review_state, "action_scope", None) is not scope):
        return None
    if _observed_checkout_state(path) != (
        scope.head_ref, getattr(review_state, "git_expected_head", None)
    ):
        return None
    if attached.head_sha.lower() != scope.observed_head_sha.lower():
        from mimir.access_control import _lease_has_clean_lineage
        from .repository import hardened_git_command

        ancestry = hardened_git_command(
            path, ("merge-base", "--is-ancestor", scope.observed_head_sha, attached.head_sha),
            timeout=10,
        )
        if (ancestry.returncode != 0 or ancestry.timed_out or ancestry.output_limited
                or not _lease_has_clean_lineage(
                    path, attached, scope, scope.observed_head_sha.lower(), attached.head_sha.lower(),
                )):
            return None
    return attached


def _observed_checkout_state(path: Path) -> tuple[str, str] | None:
    """Inspect the current checkout without inheriting user Git configuration."""
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
    }
    output = bytearray()
    try:
        process = subprocess.Popen(
            ("/usr/bin/git", "-C", str(path), "-c", "core.fsmonitor=",
             "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
             "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
             "--no-pager", "--no-optional-locks", "status", "--porcelain=v2",
             "--branch", "--untracked-files=no"),
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
        )
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                remaining_bytes = 1_048_576
                while selector.get_map():
                    remaining_time = deadline - time.monotonic()
                    if remaining_time <= 0:
                        return None
                    for key, _ in selector.select(min(remaining_time, 0.1)):
                        stream = key.fileobj
                        chunk = os.read(stream.fileno(), min(65_536, remaining_bytes + 1))
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        if len(chunk) > remaining_bytes:
                            return None
                        remaining_bytes -= len(chunk)
                        if stream is process.stdout:
                            output.extend(chunk)
                if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    return None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
    except (OSError, subprocess.SubprocessError):
        return None
    fields = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.startswith("# branch."):
            key, separator, value = line[2:].partition(" ")
            if separator:
                fields[key] = value
    branch, head = fields.get("branch.head"), fields.get("branch.oid", "").lower()
    if (not branch or branch == "(detached)" or len(head) != 40
            or any(char not in "0123456789abcdef" for char in head)):
        return None
    return branch, head


def validate_scoped_pr_action(action: RepoPRAction, scope: object, grant: object) -> bool:
    """Validate the selected owner's action against host-observed authority."""
    return (
        type(action) is RepoPRAction
        and action.value in getattr(scope, "allowed_operations", ())
        and getattr(grant, "scope_id", None) == getattr(scope, "scope_id", None)
        and getattr(grant, "owner", None) == "cultivate"
        and getattr(grant, "action", None) == action.value
    )


# The Worklink factory's retained incident grants only these five operations;
# PR checkout leases use the separately declared repo_actions membership and
# per-tool result origin below. Do not broaden factory authority for PR results.
_RETAINED = ("repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit")
_BUILDS = ("worklink_run", "worklink_resume")
_PUBLISH = ("repo_commit", "repo_merge", "repo_rebase", "repo_revert", "repo_push")


def host_membership_projection() -> dict[str, tuple[str, ...]]:
    return {
        "retained_repo_tools": _RETAINED,
        "worklink_build_tools": _BUILDS,
        "repo_publish_tools": _PUBLISH,
    }


def resolve_review_state_for_context(auth_context: object, repository: object, pull_request: object) -> object:
    from .forge import resolve_review_state_for_context as resolve

    return resolve(auth_context, repository, pull_request)


def revalidate_review_head_for_context(auth_context: object, repository: object, pull_request: object) -> object:
    from .forge import revalidate_review_head_for_context as revalidate

    return revalidate(auth_context, repository, pull_request)


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


_FORGE = _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments
    pr_review_requests pr_submit_review pr_inline_review_comment pr_comment
    pr_edit_body issue_comment pr_rerequest_review unsupported_operation""")
# Provider declaration for PR-lease result classification (with the registered
# result_origin); the host additionally verifies the exact live checkout lease.
_REPO = _names("""repo_checkout repo_cleanup repo_fetch repo_status repo_test
    repo_diff repo_file_at_revision repo_restore_file repo_unmerged repo_stage repo_commit repo_merge repo_merge_abort
    repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push""")
CODING_TOOLS = _FORGE | _REPO | frozenset(_BUILDS) | {"spawn_open_code"}
TRACKER_TOOL_NAMES = _names("tracker_file_work tracker_comment tracker_set_state")
CULTIVATE_TOOL_NAMES = CODING_TOOLS | TRACKER_TOOL_NAMES


def _spawn_target(_name: str, arguments: dict, _context: object) -> tuple[str | None, ...]:
    target = arguments.get("cwd") or os.environ.get("MIMIR_HOME")
    artifact = arguments.get("artifact_root")
    return (str(target) if target else None, str(artifact)) if artifact else (
        str(target) if target else None,
    )


def _worklink_target(_name: str, _arguments: dict, _context: object) -> tuple[str | None, ...]:
    target = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    return (str(target) if target else None,)


def _repo_pr_target(tool_name: str, arguments: dict, context: object) -> tuple[str | None, ...]:
    from mimir.plugins.core import selected_policy_registry

    tool = selected_policy_registry().tools.get(tool_name)
    retained = getattr(context, "retained_factory_scope", None)
    if (tool is not None and tool.owner == "cultivate"
            and "retained_repo_tools" in tool.memberships
            and isinstance(retained, RetainedFactoryScope)
            and getattr(context, "is_service", False)
            and arguments.get("repository") == retained.repository
            and arguments.get("pull_request") == retained.issue_id):
        return (f"{retained.repository}#issue/{retained.issue_id}@retained/{retained.scope_id}",)
    repository, number = arguments.get("repository"), arguments.get("pull_request")
    discovered = getattr(context, "server_discovered_pr_states", None)
    state = (discovered.resolve_for_tool(tool_name, repository, number)
             if discovered is not None and isinstance(repository, str) and isinstance(number, int)
             else None)
    if state is None:
        registry = getattr(context, "repo_pr_scope_registry", None)
        state = (registry.resolve(repository, number)
                 if registry is not None and hasattr(registry, "resolve") else None)
    if state is None:
        return (None,)
    scope = state.action_scope
    return (f"{scope.canonical_repo}#pull/{scope.pr_number}"
            f"@{scope.observed_head_sha}:{scope.scope_id}",)


def _tracker_target(_name: str, arguments: dict, context: object) -> tuple[str | None, ...]:
    external_ref = arguments.get("external_ref")
    for adapter, ref, _url in getattr(context, "tracker_issue_targets", frozenset()):
        if external_ref == f"{adapter}:{ref}":
            return (f"tracker:{external_ref}",)
    return (None,)


_D = ToolDescriptor
_E = ResultOriginKind.EXTERNAL
_R = ResultOriginKind.REPOSITORY
_N = ResultOriginKind.NON_INGESTING
_DESCRIPTORS = MappingProxyType({
    "ci_recent_runs": _D(result_origin=_E | _R),
    "ci_run": _D(result_origin=_E | _R),
    "ci_run_jobs": _D(result_origin=_E | _R),
    "issue_comment": _D(SinkCategory.FORGE, _generic_target, "configured_repository_issue", sink_payload_extractor=_argument_payload("body")),
    "pr_checks": _D(result_origin=_E | _R),
    "pr_comment": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "pr_comments": _D(result_origin=_E | _R),
    "pr_diff": _D(result_origin=_E | _R),
    "pr_edit_body": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "pr_file_content": _D(result_origin=_E | _R),
    "pr_files": _D(result_origin=_E | _R),
    "pr_inline_review_comment": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "pr_job_log": _D(result_origin=_E | _R),
    "pr_list": _D(result_origin=_E | _R),
    "pr_metadata": _D(result_origin=_E | _R),
    "pr_rerequest_review": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "pr_review_requests": _D(result_origin=_E | _R),
    "pr_reviews": _D(result_origin=_E | _R),
    "pr_spec": _D(result_origin=_R),
    "pr_submit_review": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "repo_checkout": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=None),
    "repo_cleanup": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "repo_commit": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=_argument_payload("message")),
    "repo_diff": _D(result_origin=_R, git_operation_result=True),
    "repo_file_at_revision": _D(result_origin=_R, git_operation_result=True),
    "repo_restore_file": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "repo_fetch": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_merge": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_merge_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_push": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_rebase": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_rebase_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_revert": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_revert_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_stage": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, git_operation_result=True, sink_payload_extractor=None),
    "repo_status": _D(result_origin=_R, git_operation_result=True),
    "repo_test": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=None),
    "repo_unmerged": _D(result_origin=_R, git_operation_result=True),
    "spawn_open_code": _D(SinkCategory.SPAWN, _spawn_target, "spawn_process", ifc_delegation=True),
    "tracker_comment": _D(SinkCategory.FORGE, _tracker_target, "bound_tracker_issue", result_origin=_N, sink_payload_extractor=_argument_payload("body")),
    "tracker_file_work": _D(SinkCategory.FORGE, _tracker_target, "bound_tracker_issue", result_origin=_N, sink_payload_extractor=_argument_payload("description")),
    "tracker_set_state": _D(SinkCategory.FORGE, _tracker_target, "bound_tracker_issue", result_origin=_N, sink_payload_extractor=_argument_payload("state")),
    "unsupported_operation": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "worklink_resume": _D(SinkCategory.SPAWN, _worklink_target, "worklink"),
    "worklink_run": _D(SinkCategory.SPAWN, _worklink_target, "worklink"),
})

_FLOW = MappingProxyType({
    "source": _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments pr_review_requests
        repo_status repo_diff repo_file_at_revision repo_unmerged"""),
    "both": _names("worklink_run worklink_resume spawn_open_code repo_checkout repo_fetch repo_test"),
    "sink": _names("""pr_submit_review pr_inline_review_comment pr_comment pr_edit_body issue_comment
        pr_rerequest_review unsupported_operation repo_cleanup repo_restore_file repo_stage repo_commit repo_merge
        repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push
        tracker_file_work tracker_comment tracker_set_state"""),
})
_TIER = MappingProxyType({
    "scope-contained": _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
        pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments
        pr_review_requests repo_status repo_diff repo_file_at_revision repo_unmerged"""),
    "code-execution": _names("worklink_run worklink_resume spawn_open_code repo_test"),
    "scoped-with-provenance": _names("""pr_submit_review pr_inline_review_comment pr_comment
        pr_edit_body issue_comment pr_rerequest_review unsupported_operation repo_checkout repo_cleanup
        repo_fetch repo_restore_file repo_stage repo_commit repo_merge repo_merge_abort repo_rebase repo_rebase_abort
        repo_revert repo_revert_abort repo_push tracker_file_work tracker_comment tracker_set_state"""),
})
_RESULT_DOMAIN = MappingProxyType({
    "worklink": frozenset(_BUILDS), "coding_worker": _names("spawn_open_code"),
    # Only the three bound, fixed-shape tracker status writers have this label.
    "tracker_status": TRACKER_TOOL_NAMES,
    "repository": _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments pr_review_requests
        pr_submit_review pr_inline_review_comment pr_comment pr_edit_body issue_comment
        repo_checkout repo_fetch repo_status repo_test repo_diff repo_file_at_revision repo_unmerged repo_commit
        repo_merge repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push"""),
})
_READABLE_REPOSITORY = _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments
    pr_review_requests repo_checkout repo_fetch repo_status repo_test repo_diff repo_file_at_revision repo_unmerged""")
_READ_BACKEND = _names("""pr_metadata pr_spec pr_list pr_files pr_diff pr_file_content pr_checks
    pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments pr_review_requests
    repo_checkout repo_fetch repo_status repo_test repo_diff repo_file_at_revision repo_unmerged""")
_HEARTBEAT = _names("""worklink_run pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_reviews pr_comments pr_review_requests pr_submit_review pr_inline_review_comment
    pr_comment pr_edit_body pr_rerequest_review unsupported_operation repo_checkout repo_cleanup
    repo_fetch repo_status repo_test repo_diff repo_file_at_revision repo_restore_file repo_unmerged repo_stage repo_commit repo_merge
    repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push""")
_WRITES = _names("""spawn_open_code worklink_run worklink_resume repo_checkout repo_cleanup
    repo_fetch repo_test repo_restore_file repo_stage repo_commit repo_merge repo_merge_abort repo_rebase
    repo_rebase_abort repo_revert repo_revert_abort repo_push pr_submit_review
    pr_inline_review_comment pr_comment pr_edit_body issue_comment pr_rerequest_review
    tracker_file_work tracker_comment tracker_set_state""")
_MEMBERSHIPS = MappingProxyType({
    "write_resource_operations": frozenset(_BUILDS),
    "tainted_writes": _names("repo_restore_file repo_stage repo_commit repo_merge repo_rebase repo_revert repo_push repo_test"),
    "standing_review": _names("""pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_reviews pr_comments pr_review_requests pr_submit_review pr_inline_review_comment
        pr_comment pr_edit_body pr_rerequest_review repo_checkout repo_cleanup repo_fetch
        repo_status repo_test repo_diff repo_file_at_revision repo_unmerged"""),
    "remediation_effect": _names("""repo_commit repo_push pr_comment pr_edit_body
        pr_inline_review_comment pr_rerequest_review worklink_resume"""),
    "typed_error_signal": _names("worklink_run"),
    "forge_actions": _FORGE - _names("pr_list ci_run_jobs ci_run ci_recent_runs issue_comment"),
    "repo_actions": _REPO,
})
_ACTIONS = MappingProxyType({
    **{name: RepoPRAction.INSPECT for name in _names("""pr_metadata pr_spec pr_files
        pr_diff pr_file_content pr_checks pr_job_log pr_reviews pr_comments pr_review_requests
        repo_status repo_diff repo_file_at_revision repo_unmerged""")},
    **{name: RepoPRAction.CHECKOUT for name in _names("repo_checkout repo_cleanup repo_fetch")},
    "repo_test": RepoPRAction.TEST,
    **{name: RepoPRAction.WRITE for name in _names("repo_restore_file repo_stage repo_merge_abort repo_rebase_abort repo_revert_abort")},
    **{name: RepoPRAction.COMMIT for name in _names("repo_commit repo_merge repo_rebase repo_revert")},
    "repo_push": RepoPRAction.PUSH,
    "pr_submit_review": RepoPRAction.PR_REVIEW,
    "pr_inline_review_comment": RepoPRAction.PR_REVIEW,
    "pr_comment": RepoPRAction.PR_COMMENT,
    "pr_edit_body": RepoPRAction.PR_EDIT,
    "pr_rerequest_review": RepoPRAction.PR_REREQUEST,
})


@dataclass(frozen=True)
class _ToolDeclaration:
    descriptor: ToolDescriptor
    flow: str
    tier: str
    operation_class: str
    result_domain: str | None
    readable_domain: str | None
    profiles: frozenset[str]
    memberships: frozenset[str]
    read_backend: bool
    writes_code_or_state: bool
    typed_action: RepoPRAction | None


def _declaration(name: str, descriptor: ToolDescriptor) -> _ToolDeclaration:
    return _ToolDeclaration(
        descriptor=descriptor,
        flow=next(flow for flow, names in _FLOW.items() if name in names),
        tier=next(tier for tier, names in _TIER.items() if name in names),
        operation_class="admin_required" if name in {"pr_list", "issue_comment", "spawn_open_code"} else "resource_scoped",
        result_domain=next((domain for domain, names in _RESULT_DOMAIN.items() if name in names), None),
        readable_domain="repository" if name in _READABLE_REPOSITORY else None,
        profiles=frozenset(profile for profile, names in {
            "github": CULTIVATE_TOOL_NAMES - {"spawn_open_code"},
            "custom": CODING_TOOLS - {"issue_comment"},
            "heartbeat": _HEARTBEAT,
        }.items() if name in names),
        memberships=frozenset(category for category, names in _MEMBERSHIPS.items() if name in names),
        read_backend=name in _READ_BACKEND,
        writes_code_or_state=name in _WRITES,
        typed_action=_ACTIONS.get(name),
    )


_TOOL_DECLARATIONS = MappingProxyType({
    name: _declaration(name, descriptor) for name, descriptor in _DESCRIPTORS.items()
})
if set(_TOOL_DECLARATIONS) != CULTIVATE_TOOL_NAMES or any(
    sum(name in group for group in table.values()) != 1
    for name in CULTIVATE_TOOL_NAMES for table in (_FLOW, _TIER)
):
    raise RuntimeError("incomplete Cultivate tool policy")


def _factory(module: str, name: str) -> Callable[[], object]:
    def materialize() -> object:
        from importlib import import_module

        source = import_module(module)
        if name == "spawn_open_code":
            return source.spawn_open_code
        if name in _BUILDS:
            return getattr(source, name)
        if module.endswith("tracker.tools"):
            collection = source.TRACKER_TOOLS
        else:
            collection = source.FORGE_TOOLS if module.endswith("forge") else source.REPO_TOOLS
        return next(tool for tool in collection if tool.name == name)

    return materialize


def register_tools(registrar: PolicyRegistrar) -> None:
    """Install the moved tool implementations on the selected policy before freeze."""
    from mimir.plugins.registry import PolicyConsumerHooks
    from . import forge, opencode, repository, review_guard
    from .worklink.retained_scope import derive_retained_factory_scope

    def retained_checkout_root() -> Path:
        from .worklink.worker_client import worklink_checkout_root

        return worklink_checkout_root()

    def validate_repository_selection(home: Path, inventory: object) -> None:
        from .worklink.backends.registry import WorklinkConfig
        from .repository import dedicated_worklink_base

        worklink = WorklinkConfig.load(home / "worklink.yaml")
        target = inventory.repository(worklink.repository) if worklink.repository else None
        if worklink.repository is not None and target is None:
            raise RuntimeError(
                "worklink.yaml repository does not name a declared repository: "
                f"{worklink.repository}"
            )
        declared_target = str(target.root) if target is not None else None
        dedicated = dedicated_worklink_base(inventory, target) if target is not None else None
        for name in ("WORKLINK_REPO", "MIMIR_WORKLINK_REPO"):
            legacy_target = os.environ.get(name)
            if legacy_target and (
                declared_target is None
                or Path(legacy_target).resolve() not in (
                    {Path(declared_target), dedicated} if dedicated is not None else {Path(declared_target)}
                )
            ):
                raise RuntimeError(
                    f"{name} disagrees with worklink.yaml: "
                    f"legacy={legacy_target!r}, declared={declared_target!r}"
                )
        if declared_target is not None and not os.environ.get("WORKLINK_REPO"):
            os.environ["WORKLINK_REPO"] = declared_target

    registrar.register_turn_hook("startup_prerequisites", opencode.startup_prerequisites)
    registrar.register_capability("pr_review_others", tier="scoped-with-provenance")
    registrar.add_to_profile("github", "pr_review_others")
    registrar.add_to_profile("custom", "pr_review_others")

    registrar.register_policy_consumer_hooks(PolicyConsumerHooks(
        owner="cultivate", resolve_review=forge.resolve_review_state_for_context,
        revalidate_head=forge.revalidate_review_head_for_context,
        resolve_issue=forge.resolve_issue_comment_target,
        superseded_push=repository.was_superseded_by_own_push,
        verified_push=repository.was_verified_push,
        agent_push=repository.was_agent_push,
        tracked_file=repository.is_tracked_file_in_pr_checkout,
        derive_retained_scope=derive_retained_factory_scope,
        action_for=lambda name: RepoPRAction[name.upper()],
        retained_checkout_root=retained_checkout_root,
        active_checkout=repository.active_pr_checkout_lease_for_path,
        git_command=repository.hardened_git_command,
        protected_refs=repository._PROTECTED_BRANCH_REFS,
        lineage=repository._recorded_lineage,
        verified_base=repository._recorded_verified_base,
        git_binary=repository._DEFAULT_GIT, git_config=repository._BASE_CONFIG,
        git_env=repository._sanitized_git_env, lease_metadata=repository._METADATA,
        review_submission=review_guard.review_submission_from_request,
        claim_review=review_guard.claim_review_submission,
        repository_inventory=repository.RepositoryInventory.load,
        repository_selection=validate_repository_selection,
        shell_environment=review_guard.github_shell_environment,
    ))
    for name, declaration in sorted(_TOOL_DECLARATIONS.items()):
        descriptor = declaration.descriptor
        module = (
            "mimir_cultivate.forge" if name.startswith(("pr_", "ci_", "issue_"))
            or name == "unsupported_operation" else
            "mimir_cultivate.repository" if name.startswith("repo_") else
            "mimir_cultivate.tracker.tools" if name in TRACKER_TOOL_NAMES else
            "mimir_cultivate.registration" if name in _BUILDS else
            "mimir_cultivate.opencode"
        )
        action = declaration.typed_action
        adapter = (
            "worklink_repo" if name in _BUILDS else
            "spawn_workspace" if descriptor.sink_category == SinkCategory.SPAWN else
            None
        )
        registrar.register_tool(
            name=name, factory=_factory(module, name), flow=declaration.flow,
            tier=declaration.tier, operation_class=declaration.operation_class,
            resource_scope=(
                "worklink" if name in _BUILDS else
                "tracker_issue" if name in TRACKER_TOOL_NAMES else
                "ci_branch" if name == "ci_recent_runs" else
                "ci_run" if name in {"ci_run", "ci_run_jobs"} else
                "repo_pr" if declaration.operation_class == "resource_scoped" and action is not None
                else "channel" if declaration.operation_class == "resource_scoped" else None
            ),
            typed_action=action, result_domain=declaration.result_domain,
            result_origin=descriptor.result_origin,
            sink_category=descriptor.sink_category,
            sink_destination=descriptor.sink_destination,
            sink_target_extractor=descriptor.sink_target_extractor,
            sink_payload_extractor=descriptor.sink_payload_extractor,
            fetch_authorization=descriptor.fetch_authorization,
            git_operation_result=descriptor.git_operation_result,
            ifc_delegation=descriptor.ifc_delegation,
            budget_exempt=descriptor.budget_exempt,
            writes_code_or_state=declaration.writes_code_or_state,
            profiles=declaration.profiles, read_backend=declaration.read_backend,
            readable_domain=declaration.readable_domain,
            memberships=declaration.memberships, service_sink_adapter=adapter,
        )
        registrar.register_capability(name)
        for profile in declaration.profiles:
            registrar.add_to_profile(profile, name)
