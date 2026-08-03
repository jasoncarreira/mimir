"""Core dataclasses passed through the call chain.

Per-turn state lives on TurnContext (never module globals — see SPEC §4.6).
TurnRecord is the on-disk turns.jsonl shape (SPEC §10.2).
"""

from __future__ import annotations

import hashlib
import json
import time
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TurnInteractivity(StrEnum):
    """Server-owned interactivity classification for a turn."""

    INTERACTIVE = "interactive"
    NON_INTERACTIVE = "non_interactive"


class RepoPRScopeProvenance(StrEnum):
    POLLER_PAYLOAD = "poller_payload"
    SERVER_DISCOVERED = "server_discovered"


class RepoPRAction(StrEnum):
    """Closed action vocabulary for one repo/PR authority scope."""

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
class NormalizedPullRequestSnapshot:
    """Provider-neutral observed pull-request state supplied by an adapter.

    Hosting-provider payloads must be normalized before they cross into the
    authority layer.  The scope factory therefore never needs to understand a
    GitHub ``head.repo.full_name``, Bitbucket source branch, or GitLab merge-
    request payload.
    """

    state: str
    number: int
    author: str
    head_repo: str
    head_remote: str
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str


class FlowLabel(StrEnum):
    """Immutable information flow control labels (chainlink #871).

    These labels track data sensitivity through the turn lifecycle. Labels
    are monotonic - they can only be added, never removed (except via
    explicit audited admin declassification). This ensures that private/
    confidential data cannot leak to incompatible sinks.
    """

    PRIVATE = "private"
    CONFIDENTIAL = "confidential"
    INTERNAL = "internal"
    PUBLIC = "public"


class Integrity(StrEnum):
    """Server-derived trust classification for ingested content."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class IntegrityEffect(StrEnum):
    """Whether a source participates in the current turn's integrity gate."""

    ACTIVE_INGEST = "active_ingest"
    INFORMATIONAL = "informational"


@dataclass(frozen=True)
class SourceLabel:
    """Server-authoritative provenance for one protected input.

    ``authorized_principals`` is the effective read ACL. Derived service data
    must carry the intersection of its inputs' ACLs; an empty ACL is unknown,
    not public. All identity fields are required for ordinary channel egress.
    """

    principal: str | None
    domain: str | None
    resource_id: str | None
    bridge_instance: str | None
    sensitivity: str
    authorized_principals: frozenset[str] = frozenset()
    source_kind: str = "channel"
    integrity: str = Integrity.UNTRUSTED
    integrity_effect: str = IntegrityEffect.ACTIVE_INGEST

    def __post_init__(self) -> None:
        if self.integrity not in Integrity._value2member_map_:
            raise ValueError(f"invalid source integrity: {self.integrity!r}")
        if self.integrity_effect not in IntegrityEffect._value2member_map_:
            raise ValueError(
                f"invalid source integrity effect: {self.integrity_effect!r}"
            )

    @property
    def is_complete(self) -> bool:
        return bool(
            self.principal
            and self.domain
            and self.resource_id
            and self.bridge_instance
            and self.sensitivity in FlowLabel._value2member_map_
            and self.authorized_principals
        )

    @classmethod
    def derived(
        cls,
        inputs: tuple["SourceLabel", ...],
        *,
        principal: str,
        domain: str,
        resource_id: str,
        bridge_instance: str,
        sensitivity: str,
        source_kind: str = "service",
    ) -> "SourceLabel":
        """Create service-derived provenance without attenuating input trust."""
        acl: frozenset[str] = frozenset()
        if inputs and all(source.is_complete for source in inputs):
            iterator = iter(inputs)
            acl = next(iterator).authorized_principals
            for source in iterator:
                acl &= source.authorized_principals
        integrity = (
            Integrity.TRUSTED
            if inputs and all(source.integrity == Integrity.TRUSTED for source in inputs)
            else Integrity.UNTRUSTED
        )
        integrity_effect = (
            IntegrityEffect.ACTIVE_INGEST
            if any(
                source.integrity == Integrity.UNTRUSTED
                and source.integrity_effect == IntegrityEffect.ACTIVE_INGEST
                for source in inputs
            )
            else IntegrityEffect.INFORMATIONAL
        )
        return cls(
            principal=principal,
            domain=domain,
            resource_id=resource_id,
            bridge_instance=bridge_instance,
            sensitivity=sensitivity,
            authorized_principals=acl,
            source_kind=source_kind,
            integrity=integrity,
            integrity_effect=integrity_effect,
        )


def _dedup_source_labels(sources: Any) -> tuple["SourceLabel", ...]:
    """Coerce ``sources`` to a stable, de-duplicated tuple of ``SourceLabel``.

    ``sources`` is a tuple, never a ``frozenset[SourceLabel]``: a set of models
    crashes pydantic's generic serializer (unhashable dict) on the Any-typed
    runtime path (chainlink #971). Dedup preserves the "unique + append-only"
    contract for direct construction — ``with_source`` stays the incremental
    fast path but callers may also build ``sources=`` directly.
    """
    seen: set[SourceLabel] = set()
    unique: list[SourceLabel] = []
    for source in sources:
        if not isinstance(source, SourceLabel):
            raise TypeError(
                f"sources must contain SourceLabel, got {type(source).__name__}"
            )
        if source in seen:
            continue
        seen.add(source)
        unique.append(source)
    return tuple(unique)


@dataclass(frozen=True)
class InformationFlowLabels:
    """Immutable/monotonic information flow control labels (chainlink #871).

    Tracks data sensitivity from various sources to enforce the sink gate.
    Labels are monotonic - they can only be added, never removed except via
    explicit audited admin declassification action. Unknown labels fail closed.

    Sources:
    - inbound/folded messages
    - recent history
    - automatic memory/session/skill/file injection
    - attachments
    - continuation context
    - protected/partial tool/subagent results
    """

    labels: frozenset[str] = frozenset()
    source_channels: frozenset[str] = frozenset()
    # A TUPLE, not a frozenset (chainlink #971): mimir tools use postponed
    # annotations, so langchain's ``_injected_args_keys`` is empty and the
    # injected ToolRuntime is included in the model_dump that ``_parse_input``
    # runs to enumerate fields. In a real graph run that runtime's
    # ``config["configurable"]["__pregel_runtime"]`` (a langgraph Runtime) carries
    # ``context=AuthContext``; dict values serialize DUCK-TYPED, bypassing
    # type-level serializers, so #1173's opaque AuthContext serializer never
    # fires on that path and a frozenset[SourceLabel] rebuilds a set of
    # serialized dicts -> ``TypeError: unhashable type: 'dict'`` -> the whole
    # turn panics. This is the crash that survived #1173 in production. A tuple
    # fixes the data itself, so every serialization path is safe;
    # ``_dedup_source_labels`` keeps it unique + append-only.
    sources: tuple[SourceLabel, ...] = ()
    created_at: float = field(default_factory=time.monotonic, compare=False)

    def __post_init__(self) -> None:
        # Enforce the invariant at construction: a serialization-safe tuple (never
        # a frozenset[SourceLabel], which re-introduces the #971 turn-crash) that
        # is stably de-duplicated, so direct ``sources=`` construction honors the
        # same unique+append-only contract as ``with_source``.
        object.__setattr__(self, "sources", _dedup_source_labels(self.sources))

    def with_label(self, label: str) -> "InformationFlowLabels":
        """Return new instance with added label (monotonic - only adds)."""
        if label in self.labels:
            return self
        return InformationFlowLabels(
            labels=self.labels | frozenset({label}),
            source_channels=self.source_channels,
            sources=self.sources,
            created_at=self.created_at,
        )

    def with_channel(self, channel: str) -> "InformationFlowLabels":
        """Return new instance with added source channel."""
        if channel in self.source_channels:
            return self
        return InformationFlowLabels(
            labels=self.labels,
            source_channels=self.source_channels | frozenset({channel}),
            sources=self.sources,
            created_at=self.created_at,
        )

    def with_source(self, source: SourceLabel) -> "InformationFlowLabels":
        """Return a carrier with one immutable source record added."""
        if source in self.sources:
            return self
        channels = self.source_channels
        if source.source_kind == "channel" and source.resource_id:
            channels |= frozenset({source.resource_id})
        return InformationFlowLabels(
            labels=self.labels | frozenset({source.sensitivity}),
            source_channels=channels,
            sources=(*self.sources, source),
            created_at=self.created_at,
        )

    @property
    def has_untrusted_active_ingest(self) -> bool:
        """Return the exact integrity-gate predicate for accumulated sources."""
        return any(
            source.integrity == Integrity.UNTRUSTED
            and source.integrity_effect == IntegrityEffect.ACTIVE_INGEST
            for source in self.sources
        )

    def can_flow_to(self, sink: str, allowed_sinks: frozenset[str]) -> bool:
        """Check if labels permit flow to the given sink.

        Unknown labels fail closed (deny). Unknown sinks fail closed (deny).
        Same-principal/same-channel flows pass only when every label is
        destination-compatible.
        """
        if not self.labels:
            return True
        if not allowed_sinks:
            return False
        for label in self.labels:
            if label not in ("private", "confidential", "internal", "public"):
                return False
        return sink in allowed_sinks or "*" in allowed_sinks


@dataclass
class InformationFlowState:
    """Turn-local monotonic IFC state shared by frozen runtime carriers."""

    labels: InformationFlowLabels | None = None
    _declassification: "DeclassificationCapability | None" = field(
        default=None, repr=False, compare=False,
    )
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def current(self, fallback: InformationFlowLabels | None = None) -> InformationFlowLabels | None:
        with self._lock:
            return self.labels if self.labels is not None else fallback

    def has_untrusted_active_ingest(
        self, fallback: InformationFlowLabels | None = None,
    ) -> bool:
        """Evaluate the integrity predicate against the lock-protected live carrier."""
        with self._lock:
            current = self.labels if self.labels is not None else fallback
            return bool(
                isinstance(current, InformationFlowLabels)
                and current.has_untrusted_active_ingest
            )

    def merge(
        self,
        added: InformationFlowLabels,
        fallback: InformationFlowLabels | None = None,
    ) -> InformationFlowLabels:
        """Atomically union labels so concurrent tool results cannot attenuate state."""
        with self._lock:
            current = self.labels if self.labels is not None else fallback
            merged = InformationFlowLabels()
            for carrier in (current, added):
                if not isinstance(carrier, InformationFlowLabels):
                    continue
                for label in carrier.labels:
                    merged = merged.with_label(label)
                for channel in carrier.source_channels:
                    merged = merged.with_channel(channel)
                for source in carrier.sources:
                    merged = merged.with_source(source)
            if current is not None and merged != current:
                self._declassification = None
            self.labels = merged
            return merged

    def approve_sink_once(
        self,
        *,
        fallback: InformationFlowLabels | None,
        sink_category: str,
        destination: str,
        canonical_principal: str,
        lifetime_seconds: float,
        durable_audit: Any,
    ) -> bool:
        """Durably audit and install one capability for the exact live carrier."""
        with self._lock:
            current = self.labels if self.labels is not None else fallback
            if not isinstance(current, InformationFlowLabels) or not current.labels:
                return False
            issued_at = time.monotonic()
            expires_at = issued_at + lifetime_seconds
            if not durable_audit(current, issued_at, expires_at):
                return False
            self._declassification = DeclassificationCapability(
                sink_category=sink_category,
                destination=destination,
                canonical_principal=canonical_principal,
                labels=current.labels,
                source_channels=current.source_channels,
                sources=current.sources,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            return True

    def consume_sink_approval(
        self,
        *,
        current: InformationFlowLabels,
        sink_category: str,
        destination: str,
        canonical_principal: str,
    ) -> bool:
        """Atomically consume a matching, unexpired, one-use sink capability."""
        with self._lock:
            capability = self._declassification
            if capability is None:
                return False
            if time.monotonic() > capability.expires_at:
                self._declassification = None
                return False
            live = self.labels if self.labels is not None else current
            matches = (
                capability.sink_category == sink_category
                and capability.destination == destination
                and capability.canonical_principal == canonical_principal
                and isinstance(live, InformationFlowLabels)
                and capability.labels == live.labels == current.labels
                and capability.source_channels == live.source_channels == current.source_channels
                and capability.sources == live.sources == current.sources
            )
            if not matches:
                return False
            self._declassification = None
            return True


@dataclass(frozen=True)
class DeclassificationCapability:
    """One audited egress capability bound to a live turn and source snapshot."""

    sink_category: str
    destination: str
    canonical_principal: str
    labels: frozenset[str]
    source_channels: frozenset[str]
    sources: tuple[SourceLabel, ...]
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", _dedup_source_labels(self.sources))


@dataclass
class EgressSessionState:
    """Server-owned exact-URL approvals shared by turns in one session."""

    _approved_urls: set[str] = field(default_factory=set, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def approve_url(self, url: str) -> None:
        with self._lock:
            self._approved_urls.add(url)

    def is_url_approved(self, url: str) -> bool:
        with self._lock:
            return url in self._approved_urls

    def approved_urls(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._approved_urls)


@dataclass
class AgentEvent:
    """Inbound event from a bridge, scheduler tick, or HTTP injection.

    Author identity convention (FUTURE_WORK §6.1):
    - ``author``         — platform-prefixed stable id used as the
      matching key (e.g. ``"discord-99"``, ``"slack-U05ALICE"``).
      ``MessageBuffer.cross_author_messages`` compares on this field
      after resolving through ``IdentityResolver`` to a canonical.
    - ``author_display`` — human-readable name for prompt rendering
      (e.g. ``"alice#1234"``, ``"Alice Smith"``). Falls back to
      ``author`` when not set.
    - ``author_id``      — raw platform user id without the prefix
      (e.g. ``"99"``, ``"U05ALICE"``). Diagnostic / cross-reference;
      not the matching key.
    """

    trigger: str                      # "user_message" | "scheduled_tick" | "saga_session_end" | ...
    channel_id: str
    content: str = ""
    author: str | None = None
    author_display: str | None = None
    author_id: str | None = None
    source_id: str | None = None
    # Origin tag for the Recent activity allowlist (SPEC §5.4). Real
    # conversation sources ("slack", "discord", "bluesky", "web", "stdin")
    # default into the recent-messages render; programmatic injections
    # ("api") and synthetic events ("scheduler", "system") stay out unless
    # the operator opts them in via MIMIR_RECENT_SOURCES.
    source: str | None = None
    attachment_names: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # Server-owned service identity. Only trusted internal event constructors
    # set this field; generic HTTP ingress deliberately never copies it from a
    # request body. ``create_auth_context`` validates it against the registered
    # principal for ``trigger`` before granting service authority.
    service_principal: str | None = None
    # Exact immutable service grant selected by a trusted internal constructor.
    # Public ingress never copies this object from request data.
    service_authority: Any = None
    # Optional server-discovered heartbeat authority. Poller scopes are always
    # rebuilt from their trusted payload items and never accepted here.
    repo_pr_action_scope: "RepoPRActionScope | None" = None
    # Server-carried IFC state for continuations/resumed events. This must be
    # propagated from a trusted TurnContext; generic ingress must not accept a
    # client assertion as a declassification or authority signal.
    ifc_labels: "InformationFlowLabels | None" = None
    # Frozen authority inherited by a trusted server-created continuation.
    # Generic ingress constructors must not copy this field from client input.
    continuation_auth_context: "AuthContext | None" = None
    # ACL accumulated from the authoritative turns in a completed channel
    # session. Only the server-owned synthesis constructor sets this carrier.
    source_session_acl: "SessionACL | None" = None


@dataclass(frozen=True)
class SessionACL:
    """Immutable, monotonically intersected ACL for synthesis outputs."""

    owner_principal: str = "legacy_admin"
    origin_channel: str | None = None
    origin_domain: str | None = None
    visibility: str = "legacy_admin"
    provenance_complete: bool = False

    @classmethod
    def from_auth_context(
        cls,
        auth_context: "AuthContext | None",
        *,
        origin_domain: str | None,
        visibility: str,
    ) -> "SessionACL":
        if auth_context is None:
            return cls()
        owner = auth_context.canonical_principal or auth_context.principal
        channel = auth_context.channel_id
        if not owner or not channel or not origin_domain:
            return cls()
        if auth_context.is_service:
            owner = f"service:{owner}"
            visibility = "service"
        if visibility not in {"public", "private", "service"}:
            return cls()
        return cls(
            owner_principal=owner,
            origin_channel=channel,
            origin_domain=origin_domain,
            visibility=visibility,
            provenance_complete=True,
        )

    def intersect(self, other: "SessionACL") -> "SessionACL":
        """Return a no-wider ACL; ambiguous provenance permanently fails closed."""
        if not self.provenance_complete or not other.provenance_complete:
            return SessionACL()
        if (
            self.owner_principal != other.owner_principal
            or self.origin_channel != other.origin_channel
            or self.origin_domain != other.origin_domain
        ):
            return SessionACL()
        rank = {"public": 0, "private": 1, "service": 2, "legacy_admin": 3}
        visibility = max(
            (self.visibility, other.visibility), key=lambda value: rank.get(value, 3)
        )
        return SessionACL(
            owner_principal=self.owner_principal,
            origin_channel=self.origin_channel,
            origin_domain=self.origin_domain,
            visibility=visibility,
            provenance_complete=True,
        )


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
    # A provider-owned immutable ref used only to obtain the observed head.
    # Write scopes leave this unset and continue to fetch/push destination_ref.
    checkout_ref: str | None = None
    scope_id: str = field(init=False)

    def __post_init__(self) -> None:
        provenance = RepoPRScopeProvenance(self.provenance)
        object.__setattr__(self, "provenance", provenance)
        if not isinstance(self.allowed_operations, frozenset):
            raise TypeError("allowed_operations must be a frozenset")
        unknown_actions = self.allowed_operations - frozenset(
            action.value for action in RepoPRAction
        )
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
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "scope_id", hashlib.sha256(encoded).hexdigest())

    @property
    def head_ref(self) -> str:
        return self.destination_ref.removeprefix("refs/heads/")


@dataclass(frozen=True)
class RepoReviewState:
    """Immutable PR authority plus a monotonic, non-authority checkout proof."""

    action_scope: RepoPRActionScope
    checked_out: bool = field(default=False, init=False, compare=False)
    checkout_lease: Any = field(default=None, init=False, repr=False, compare=False)
    git_expected_head: str | None = field(default=None, init=False, repr=False, compare=False)
    full_tested_head: str | None = field(default=None, init=False, repr=False, compare=False)
    conflict_evidence_head: str | None = field(default=None, init=False, repr=False, compare=False)

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

    def mark_checked_out(self) -> None:
        object.__setattr__(self, "checked_out", True)

    def attach_checkout_lease(self, lease: Any) -> None:
        """Activate only a lease issued for this immutable action scope."""
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
        """Record a typed Git mutation's new HEAD without widening its scope."""
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

    def record_full_test(self, scope_id: str, head: str) -> None:
        """Record a successful unselected suite run against the exact checkout HEAD."""
        self._record_checkout_proof(scope_id, head, "full_tested_head")

    def record_conflict_evidence(self, scope_id: str, head: str) -> None:
        """Record structured two-sided preservation evidence in the exact HEAD."""
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

    def revoke_checkout_lease(self, lease: Any) -> None:
        """Revoke the exact attached lease without affecting a replacement."""
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
            (
                state for state in self.review_states
                if (state.repo.lower(), state.pr_number) == target
            ),
            None,
        )

    def resolve_checkout_path(self, path: object) -> RepoReviewState | None:
        """Resolve the active lease that exactly contains an absolute path."""
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


@dataclass
class ServerDiscoveredPRStates:
    """Per-turn cache of review states derived from live provider snapshots."""

    _states: dict[tuple[str, int], RepoReviewState] = field(
        default_factory=dict, init=False, repr=False,
    )
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)
    _remint_attempts: set[tuple[str, int]] = field(
        default_factory=set, init=False, repr=False,
    )

    def resolve(self, repository: str, pull_request: int) -> RepoReviewState | None:
        with self._lock:
            return self._states.get((repository.lower(), pull_request))

    def remember(self, state: RepoReviewState) -> RepoReviewState:
        target = (state.repo.lower(), state.pr_number)
        with self._lock:
            return self._states.setdefault(target, state)

    def begin_remint(self, repository: str, pull_request: int) -> bool:
        """Reserve the sole live-snapshot remediation re-mint for a turn."""
        target = (repository.lower(), pull_request)
        with self._lock:
            if target in self._remint_attempts:
                return False
            self._remint_attempts.add(target)
            return True

    def remember_remint(self, original: RepoReviewState, state: RepoReviewState) -> RepoReviewState:
        """Install newly derived authority without altering the poller scope."""
        target = (original.repo.lower(), original.pr_number)
        replacement_target = (state.repo.lower(), state.pr_number)
        if (
            target != replacement_target
            or original.action_scope.event_type != "pr_changes_requested_stale"
            or state.action_scope.event_type != original.action_scope.event_type
            or state.action_scope.provenance != RepoPRScopeProvenance.SERVER_DISCOVERED
        ):
            raise ValueError("reminted review scope does not match original remediation target")
        with self._lock:
            if target not in self._remint_attempts:
                raise ValueError("reminted review scope was not reserved")
            return self._states.setdefault(target, state)


@dataclass(frozen=True)
class AuthContext:
    """Frozen, server-created authorization context (chainlink #864).

    This context carries immutable authorization state from the server's ingress
    point through the entire turn execution. It is created BEFORE model execution
    and CANNOT be widened or mutated by the model, tools, or downstream handlers.

    The key invariant: authority is derived ONLY from this frozen carrier, NOT from:
    - Model-passed session_id
    - ContextVar fallback heuristics
    - Single-active-turn heuristics

    Fields are immutable (frozen=True) to prevent post-creation widening.

    The ifc_labels field carries per-turn IFC labels on the durable carrier,
    ensuring sink-flow checks survive forked SDK/MCP tasks where the
    _current_turn ContextVar is lost (chainlink #891).
    """

    principal: str | None
    canonical_principal: str | None
    roles: tuple[str, ...]
    event_ingress: str | None
    trigger: str
    channel_id: str | None
    interactivity: "TurnInteractivity | None"
    policy_version: str | None = None
    is_service: bool = False
    service_authority: Any = field(default=None, repr=False)
    enforcement_enabled: bool = False
    ifc_labels: "InformationFlowLabels | None" = None
    domain: str | None = None
    resource_id: str | None = None
    bridge_instance: str | None = None
    # Write provenance selected at ingress. These are deliberately absent from
    # model-facing tool arguments and cannot be changed after construction.
    origin_trigger: str | None = None
    origin_ref: str | None = None
    # Mutable only through its monotonic merge API. Keeping this cell on the
    # frozen carrier lets later forked requests observe post-tool taint without
    # making identity, roles, or any authority field mutable.
    ifc_state: InformationFlowState = field(
        default_factory=InformationFlowState, repr=False, compare=False,
    )
    egress_state: EgressSessionState = field(
        default_factory=EgressSessionState, repr=False, compare=False,
    )
    # Server-created immutable registry for all valid PR items in one trusted
    # poller payload. Each state carries its own monotonic checkout proof.
    repo_pr_scope_registry: RepoPRScopeRegistry | None = field(
        default=None, repr=False, compare=False,
    )
    # Single-scope aliases retained for callers whose trusted event can only
    # carry one PR (notably server-discovered heartbeat turns).
    repo_review_state: RepoReviewState | None = field(
        default=None, repr=False, compare=False,
    )
    repo_pr_action_scope: RepoPRActionScope | None = field(
        default=None, repr=False, compare=False,
    )
    # Standing review authority is resolved lazily from a live server fetch.
    # This cache is per turn and stores only immutable server-issued scopes.
    server_discovered_pr_states: ServerDiscoveredPRStates = field(
        default_factory=ServerDiscoveredPRStates, repr=False, compare=False,
    )
    # Resource ACL for outputs derived by a trusted synthesis turn. This does
    # not grant execution authority; it only attenuates durable output scope.
    source_session_acl: SessionACL | None = None
    # Server-selected SAGA resource for a synthesis turn. Model-supplied
    # session IDs are only selectors and must match this immutable value.
    saga_session_id: str | None = None

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        """Serialize opaquely wherever pydantic dumps an AuthContext.

        AuthContext is injected into tools as ``ToolRuntime[AuthContext]``.
        langchain's tool input-parsing (`_parse_input`) calls ``model_dump()`` on
        the parsed args purely to compute the field set; the delivered values are
        taken from ``getattr`` on the validated model, so the real runtime object
        still reaches the tool regardless of what this serializer emits. But the
        default dataclass serializer would recurse into fields pydantic cannot
        python-serialize (``ifc_state``'s ``threading.Lock``), which raised and
        panicked the ENTIRE turn (chainlink #971). Keep the default *validation*
        schema so instances still validate; override only serialization to a
        stable opaque placeholder. Nothing consumes the serialized form.

        This covers only paths where pydantic consults the AuthContext schema
        (the typed ``runtime.context`` field). It is BYPASSED wherever the
        AuthContext is reached through duck-typed traversal — notably
        ``runtime.config["configurable"]["__pregel_runtime"]`` (a langgraph
        Runtime holding ``context=AuthContext``) during real graph runs — so
        ``InformationFlowLabels.sources`` is a ``tuple`` (not a
        ``frozenset[SourceLabel]``) to make the data itself serialization-safe.
        That tuple is the fix for the crash that survived #1173 in production.
        See ``InformationFlowLabels.sources``.
        """
        from pydantic_core import core_schema

        schema = handler(source)
        schema["serialization"] = core_schema.plain_serializer_function_ser_schema(
            lambda _value: None,
        )
        return schema


@dataclass(frozen=True)
class PromptBlock:
    """Protected prompt content paired with immutable source provenance."""

    content: str
    labels: InformationFlowLabels


@dataclass
class TurnContext:
    """Per-turn state. One instance per run_turn — never shared across turns."""

    turn_id: str
    session_id: str                   # = channel_id (viewer scope, SPEC §4.6)
    trigger: str
    channel_id: str | None
    started_at: float
    # Logical agent name — sourced from ``Config.agent_id`` at run_turn
    # entry. Threaded into TurnRecord + emitted with every event so a
    # cross-process operator running two agents on the same hardware
    # (each in its own process) can filter the merged log streams by
    # agent. ``None`` only in tests that construct TurnContext directly
    # without going through Agent.
    agent_id: str | None = None
    saga_session_id: str | None = None
    saga_atom_ids: list[str] = field(default_factory=list)
    # chainlink #266 slice 6: skill-learning atom IDs injected into this
    # turn's prompt (poller auto_skill_block + non-poller read_file
    # middleware). run_turn folds these into the TurnRecord's
    # ``saga_atom_ids`` so the session-boundary synthesis turn votes them
    # via saga_feedback — but deliberately NOT into the per-turn
    # auto-feedback credit pass, which writes a weight-2.0 boost on every
    # cited atom each successful turn and would inflate every injected
    # learning uniformly (defeating activation ranking). Populated
    # best-effort; empty when no skill loads this turn.
    injected_skill_atom_ids: list[str] = field(default_factory=list)
    # Tool-call budget tracking (SPEC §4.5 follow-on / FUTURE_WORK).
    # Incremented on every ALLOWED PreToolUse; the budget hook denies
    # once at-cap (without incrementing) and warns once when the soft
    # threshold is first crossed. 0 = no budget enforced.
    tool_call_count: int = 0
    tool_call_budget: int = 0
    # Durable hard-denial markers for continuation/recovery paths.
    # Populated only when a NON-exempt tool is refused at/over the cap;
    # allowed calls leave them untouched. ``first_denied_at_count`` records
    # the already-used count seen at the first refusal (normally == budget
    # because denied calls do not increment ``tool_call_count``).
    tool_call_budget_exhausted: bool = False
    tool_call_budget_denied_count: int = 0
    tool_call_budget_denied_tools: list[str] = field(default_factory=list)
    tool_call_budget_first_denied_at_count: int | None = None
    # Framework-owned record of actions refused by an always-on authorization
    # or tool boundary. The model only receives rendered ToolMessages and cannot
    # populate this classification itself.
    hard_boundary_denials: list[dict[str, str]] = field(default_factory=list)
    # CR2 (agent runtime) fix: soft-warning idempotency. Without this
    # flag, the previous ``count == soft_threshold`` trigger could miss
    # a warning if any code path skipped an increment, AND could fire
    # repeatedly if a future change ever decremented the count. One-shot
    # flag means the warning fires exactly once per turn at the first
    # crossing.
    _tool_call_soft_warning_emitted: bool = False
    # chainlink #511: per-turn model-iteration ceiling — 3-tier, belt-and-
    # suspenders alongside the tool-call budget + homeostat.
    # ``IterationGateMiddleware`` counts model iterations (before_model) and
    # escalates: 75% gentle wrap-up nudge (no event), 90% urgent nudge (+event),
    # 100% hard stop (force-end the turn + event). Each one-shot flag fires its
    # tier exactly once. ``iteration_hard_stopped`` tells ``run_turn`` to send a
    # cap notice to the channel (the model never got to deliver). 0 = disabled.
    iteration_count: int = 0
    iteration_budget: int = 0
    _iteration_warn_75_emitted: bool = False
    _iteration_warn_90_emitted: bool = False
    _iteration_cap_emitted: bool = False
    iteration_hard_stopped: bool = False
    # Origin source of the inbound event (carried from AgentEvent.source so
    # outbound assistant replies on the same channel inherit it).
    channel_source: str | None = None
    # Runtime access-control context for tool middleware. Populated by
    # Agent.run_turn from the inbound AgentEvent and Config/IdentityResolver.
    author: str | None = None
    identity_resolver: Any | None = None
    access_control_enforced: bool = False
    # Frozen authorization context (chainlink #864). Created at ingress before
    # model execution and supplied as LangGraph runtime context for ordinary,
    # built-in, and wrapped MCP tools. Immutable - authority derives ONLY from
    # this carrier, NOT from model session_id, ContextVar fallback, or
    # single-active-turn heuristics.
    auth_context: AuthContext | None = None
    # Information flow control labels (chainlink #871). Immutable/monotonic
    # labels tracking data sensitivity from various sources. Initialized before
    # the first model call from inbound/folded messages, recent history,
    # automatic memory/session/skill/file injection, attachments, and
    # continuation context. Propagated to subagents, spawns, continuations,
    # and resumed turns. Blocked at incompatible sinks.
    ifc_labels: InformationFlowLabels | None = None
    # Number of successful send_message deliveries in this turn (incremented
    # only after the bridge confirms ``SendResult.sent``). The forgot-to-send
    # guard emits ``interactive_turn_no_send_message`` when an interactive turn
    # produced final text but this is still 0 — i.e. the reply never went out
    # (0.3.0: send_message is the sole delivery path).
    send_message_count: int = 0
    # Number of successful react tool calls this turn. A react is a valid
    # interactive response (an acknowledgment), so the forgot-to-send guard
    # treats react_count > 0 the same as a delivered send_message — otherwise
    # a react-only reply gets falsely flagged as "no reply" (0.3.2).
    react_count: int = 0
    # Channels that received a CONFIRMED delivery this turn (send_message
    # with SendResult.sent, or a confirmed react — tool or directive).
    # chainlink #423: the forgot-to-send guard is channel-scoped — an
    # interactive turn must deliver to the TRIGGERING channel; a
    # cross-channel send (e.g. an ops-channel alert) doesn't count as
    # replying to the user who asked. The plain counters above stay for
    # observability; this set is what the guard reads.
    delivered_channel_ids: set = field(default_factory=set)
    # Channel-layer state (Phase 6.3) — populated by the agent at run_turn start.
    loop_detector: object | None = None
    last_assistant_message_id: str | None = None
    # Synthesis-turn observability (CR#19). The synthesis prompt instructs
    # the agent to call ``saga_end_session`` (step 3); this flag flips True
    # in the tool handler on success. The agent's post-message hook checks
    # it at synthesis-turn end and emits ``saga_synthesis_skipped_boundary``
    # when False, so silent contract failures (agent didn't follow step 3)
    # become a visible algedonic signal instead of empty session-summary
    # blocks for the next session.
    saga_end_session_called: bool = False
    # Subagent task descriptions captured during the SDK message loop
    # (CR#15). ``TaskStartedMessage`` writes here; ``TaskNotificationMessage``
    # reads to populate the inbox push's ``description`` field. Lives on
    # the ctx (not on the SubagentLifecycleHook) so concurrent turns on
    # different channels don't share state.
    task_descriptions: dict[str, str] = field(default_factory=dict)
    # WikiBacklinksHook snapshot: ``{absolute_page_path: st_mtime}`` taken
    # at ``pre_query``, compared at ``finalize`` to detect which wiki
    # pages were modified during the turn. Same multi-channel-safety
    # rationale as task_descriptions. Empty dict when the hook didn't
    # populate it (e.g. tests that drive ``finalize`` directly).
    wiki_mtime_snapshot: dict[str, float] = field(default_factory=dict)
    # Per-turn saga call audit log. Populated by the
    # ``RecordingSagaClient`` wrapper around every saga method invocation
    # (query / store / feedback / mark_contributions / end_session /
    # contextual rewrite). Surfaces in turns.jsonl so the turn viewer
    # can show "what saga did this turn" without joining to events.jsonl.
    # Each entry: ``SagaCallRecord`` (call type, args summary, result
    # summary, latency_ms, error). Empty when no saga calls fired (e.g.
    # synthetic ticks with no inbound, scheduled callables).
    saga_calls: list[SagaCallRecord] = field(default_factory=list)
    # Durable server-owned ingress provenance copied from ``AgentEvent.extra``
    # when present (for example generic HTTP ``POST /event`` stamping). Tool
    # middleware reads this instead of trusting client-controlled trigger /
    # source / author fields for admin-sensitive decisions.
    event_ingress: str | None = None
    # Server-owned turn classification. Optional so older call sites and
    # fail-closed guards can distinguish "not classified yet" from an explicit
    # interactive/non-interactive decision.
    interactivity: TurnInteractivity | None = None


@dataclass
class SagaCallRecord:
    """One saga API call captured during a turn.

    Recorded by ``RecordingSagaClient`` (mimir/saga_client.py) which
    wraps the underlying ``SagaStore`` and appends
    to ``TurnContext.saga_calls`` on every method invocation. The
    rollup writes these into ``turns.jsonl`` so the turn viewer can
    display saga's per-turn behavior inline without joining to
    events.jsonl.

    Field rationale:
    - ``call_type`` — saga method name (``query`` / ``store`` /
      ``feedback`` / ``mark_contributions`` / ``end_session`` /
      ``rewrite``). ``rewrite`` is the contextual-rewrite path that
      fires inside ``query`` when a non-empty ``context`` is passed.
    - ``args`` — input summary as a JSON-able dict. Strings are
      truncated to 200 chars to bound row size. Full content lives
      in events.jsonl if needed.
    - ``result`` — output summary (atom IDs retrieved, atom ID stored,
      etc.). Bounded for the same reason.
    - ``latency_ms`` — wall-clock duration of the call.
    - ``t_ms`` — wall-clock offset from ``ctx.started_at`` to the
      moment the call STARTED (not finished). Lets the turn viewer
      interleave saga calls with SDK events on a single chronological
      timeline. ``None`` when the recorder couldn't resolve the active
      ctx (e.g. saga calls fired outside any turn — consolidation cron,
      decay sweeps).
    - ``error`` — exception message if the call raised, else ``None``.
      An errored call still produces a record so the turn viewer can
      surface failures.
    """

    call_type: str
    args: dict
    result: dict
    latency_ms: float
    error: str | None = None
    t_ms: float | None = None

    def to_dict(self) -> dict:
        out = {
            "call_type": self.call_type,
            "args": self.args,
            "result": self.result,
            "latency_ms": round(self.latency_ms, 2),
        }
        if self.t_ms is not None:
            out["t_ms"] = round(self.t_ms, 2)
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class TurnRecord:
    """One JSONL record per agent turn (SPEC §10.2)."""

    ts: str
    turn_id: str
    session_id: str
    saga_session_id: str | None
    trigger: str
    channel_id: str | None
    input: str
    # Logical agent name — sourced from ``Config.agent_id``. Tagging
    # every turn record lets a cross-process operator running two
    # agents filter merged turns.jsonl output by agent without grepping
    # by MIMIR_HOME path. ``None`` on records written by code paths
    # predating this field — the turn viewer treats absent agent_id as
    # "unknown / single-agent legacy run".
    agent_id: str | None = None
    # Monotonically increasing turn sequence number, assigned by TurnLogger on
    # write. Survives retention trimming (the high-water mark is re-seeded from
    # the newest retained record), so the latest record's ``seq`` is the running
    # turn total surfaced in the web dossier. ``None`` on legacy records until
    # TurnLogger backfills them on startup.
    seq: int | None = None
    saga_atom_ids: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    # chainlink #376: user messages that arrived mid-turn and were FOLDED into
    # this turn at a before_model boundary. Each entry is ``{"t_ms": float,
    # "text": str}`` (PR 4) — the rendered text the model saw plus a
    # start-relative fold offset (same axis as event/saga ``t_ms``) so the turn
    # viewer can place it on the timeline. One entry per fold, in fold order.
    # ``input`` stays the original turn prompt; these are the additional inputs
    # this single turn absorbed. Empty for the overwhelming majority of turns.
    # Threaded here so the durable surfaces — turn log, synthesis summary, turn
    # viewer — report what the turn consumed, not just the live message list.
    # (PR 3 shipped this as ``list[str]``; readers tolerate both.)
    injected_inputs: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None
    # SDK ResultMessage capture (Phase 8 — resume detection + cost). Populated
    # from the final ``ResultMessage`` the SDK emits per turn. ``None`` when
    # no ResultMessage was received (e.g. query() crashed mid-stream).
    result_subtype: str | None = None
    result_is_error: bool | None = None
    stop_reason: str | None = None
    num_turns: int | None = None           # SDK's internal model-turn count
    total_cost_usd: float | None = None    # None for non-Anthropic gateways
    usage: dict[str, Any] | None = None    # input/output/cache token counts
    # Final allowed-call count from TurnContext. Kept outside ``events`` because
    # that array is size-bounded and cannot support accurate budget sizing.
    tool_call_count: int = 0
    permission_denials: list[Any] = field(default_factory=list)
    # Discriminator for synthetic, non-conversational records (chainlink #60).
    # ``None`` for ordinary agent turns (the existing case). Set to
    # Legacy logs may contain ``"claude_code_spawn"`` records. Keep the
    # discriminator readable for persisted turn-log compatibility.
    kind: str | None = None
    # Inline saga call audit. Each entry is a ``SagaCallRecord.to_dict()``
    # populated by ``RecordingSagaClient`` during the turn. Empty list
    # for turns that didn't touch saga (synthetic ticks, no-op heartbeats,
    # synthesis turns that didn't call back). Surfaces in the turn viewer
    # so "what saga did this turn" is visible inline without joining to
    # events.jsonl.
    saga_calls: list[dict[str, Any]] = field(default_factory=list)
    # Server-owned turn classification carried into the durable turn log.
    # Optional for backward compatibility and fail-closed downstream guards.
    interactivity: TurnInteractivity | None = None


def make_turn_id() -> str:
    # CR2 (agent runtime) fix: was ``hex[:12]`` = 48 bits. The
    # ``_active_turns`` registry (and the budget hook's
    # ``client_cell.turn_id`` foreign key) is keyed on this id;
    # birthday-bound 50% collision arrived at ~16M turns. With 64
    # bits, 50% collision is ~4B turns — well past the lifetime of
    # any single mimir process. The id is a key, not a display
    # string, so the brevity-vs-collision trade-off favors safety.
    return uuid.uuid4().hex[:16]


def make_process_session_id() -> str:
    """events.jsonl session_id — one per process lifetime (open-strix convention)."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
