"""Ownership and visibility value objects for SAGA (chainlink #881).

Defines the ownership model where:
- owner_principal: who owns the atom (system, service, legacy_admin, or user-id)
- origin_channel: where the atom originated (channel ID, session ID, etc.)
- origin_domain: domain/namespace of origin
- visibility: who can read the atom (public, private, service, legacy_admin)

Pre-existing rows that cannot prove ownership migrate to legacy_admin scope,
which is service/admin-only and not readable by regular users.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


log = logging.getLogger(__name__)


class Visibility(StrEnum):
    """Visibility levels for atoms, sessions, observations, and triples.

    'legacy_admin' is the fail-closed default for pre-v7 data that cannot
    prove ownership. Regular users cannot read legacy_admin-scoped content.
    """
    PUBLIC = "public"
    PRIVATE = "private"
    SERVICE = "service"
    LEGACY_ADMIN = "legacy_admin"


class OwnerPrincipal(StrEnum):
    """Owner principal types.

    'legacy_admin' is the default for pre-v7 data that cannot prove provenance.
    """
    SYSTEM = "system"
    SERVICE = "service"
    LEGACY_ADMIN = "legacy_admin"


DEFAULT_VISIBILITY = Visibility.LEGACY_ADMIN
DEFAULT_OWNER = OwnerPrincipal.LEGACY_ADMIN

RESERVED_SENTINEL_PRINCIPALS: frozenset[str] = frozenset({
    OwnerPrincipal.LEGACY_ADMIN,
    OwnerPrincipal.SERVICE,
    OwnerPrincipal.SYSTEM,
})


@dataclass(frozen=True)
class Ownership:
    """Ownership metadata for atoms, sessions, observations, and triples.

    Attributes:
        owner_principal: Who owns this entity (system, service, user-id)
        origin_channel: Channel/source where the entity originated
        origin_domain: Domain/namespace of origin
        visibility: Who can read this entity
        provenance: Additional provenance data (JSON-like dict)
    """
    owner_principal: str = OwnerPrincipal.LEGACY_ADMIN
    origin_channel: str | None = None
    origin_domain: str | None = None
    visibility: Visibility = Visibility.LEGACY_ADMIN
    provenance: dict[str, Any] = field(default_factory=dict)

    def is_legacy_admin_only(self) -> bool:
        """Check if this entity is only accessible to admins/services."""
        return self.visibility == Visibility.LEGACY_ADMIN

    def to_columns(self) -> dict[str, str | None]:
        """Convert to column values for SQL insertion.

        PRODUCTION-DEAD (chainlink #895): retained for API stability; current
        production writers pass ownership columns directly.
        """
        return {
            "owner_principal": str(self.owner_principal),
            "origin_channel": self.origin_channel,
            "origin_domain": self.origin_domain,
            "visibility": str(self.visibility),
            "provenance": json.dumps(
                self.provenance, sort_keys=True, separators=(",", ":")
            ),
        }


def is_user_accessible(visibility: str) -> bool:
    """Check if a visibility level allows regular user access.

    Visibility alone can only prove public access. Private rows require an
    owner match, while service and legacy-admin rows require a trusted service
    or admin principal; those checks belong in the authorization layer.
    """
    return visibility == Visibility.PUBLIC


@dataclass(frozen=True)
class AuthorizationScope:
    """Authorization scope for SAGA read operations (chainlink #883, #897).

    Captures the caller's identity and permissions to determine what
    resources they can read from SAGA (atoms, sessions, triples).

    Trusted services receive broad internal read access only when their frozen
    ``ServicePrincipal`` explicitly declares it. Tenant isolation is enforced
    on broad readers' OUTPUTS via derivation-ACL intersection (#884) and
    egress/IFC (#871), NOT by restricting those declared reads:
    - Tenant isolation is enforced by user-facing read scope, derivation-ACL
      intersection on consolidated artifacts, and egress/IFC. A user never sees
      an autonomous turn's raw reads, only its ACL-carrying outputs.
    - Services without that declaration receive public and owned rows plus
      their explicitly declared readable domains.

    Attributes:
        principal: The caller's principal identifier (e.g., "user:123")
        is_admin: Whether the caller has admin role
        is_service: Whether the caller is a trusted service
        is_platform_service: Whether the trusted principal explicitly declares
            full internal SAGA read. This does not make it an admin.
        readable_domains: Tuple of domain names the service can read
        service_canonical: Canonical name of the service (if is_service)
    """
    principal: str | None = None
    is_admin: bool = False
    is_service: bool = False
    is_platform_service: bool = False
    readable_domains: tuple[str, ...] = ()
    service_canonical: str | None = None


def _authorization_predicate(
    scope: AuthorizationScope,
    *,
    table: str,
) -> tuple[str, list]:
    """Build the shared owner/visibility/domain predicate for a resource table.

    Platform/maintenance services (is_platform_service=True) get full internal
    read access, matching the admin read predicate without acquiring admin role
    or mutation authority. Tenant isolation is enforced on their OUTPUTS via
    #884 + #871.

    Regular services with readable_domains get domain-restricted access plus
    public and owned rows.
    """
    if scope.is_admin:
        return ("1=1", [])

    # Explicit full-corpus authority does not widen role or mutation authority;
    # tenant isolation remains enforced on derived outputs.
    if scope.is_platform_service:
        return ("1=1", [])

    # Every grant is an alternative.  Combining the owner grant with the public
    # grant using AND collapses ``private + owned`` to public-only and makes a
    # user's own private rows unreadable.  Keep one OR group so no grant narrows
    # another grant accidentally.
    grants = [f"{table}.visibility = ?"]
    params = [Visibility.PUBLIC.value]

    owner_principal = (
        f"service:{scope.service_canonical}"
        if scope.is_service and scope.service_canonical
        else scope.principal
    )
    if owner_principal:
        if owner_principal not in RESERVED_SENTINEL_PRINCIPALS:
            grants.append(f"{table}.owner_principal = ?")
            params.append(owner_principal)

    if scope.is_service and scope.readable_domains:
        domains = list(scope.readable_domains)
        placeholders = ",".join(["?"] * len(domains))
        grants.append(f"{table}.origin_domain IN ({placeholders})")
        params.extend(domains)

    return (f"({' OR '.join(grants)})", params)


def authorization_predicate(
    scope: AuthorizationScope,
    table: str = "atoms",
) -> tuple[str, list]:
    """Generate the parameterized SAGA read predicate for an atom-like table.

    Authorization happens in SQL before content/existence is exposed:
    - Admins can read everything (1=1).
    - Platform services (is_platform_service=True) get broad internal read:
      public + service + legacy_admin + owned rows. This allows autonomous
      turns to recall the agent's own memory including legacy_admin corpus.
      Tenant isolation is enforced on outputs via #884 + #871.
    - Regular services with readable_domains get domain-restricted access
      plus public and owned rows.
    - Regular users can read public rows and their own rows.
    Capability names never widen readable domains.
    """
    return _authorization_predicate(scope, table=table)


def authorization_predicate_for_triples(
    scope: AuthorizationScope,
    table: str = "triples",
) -> tuple[str, list]:
    """Generate the parameterized read predicate for a triple resource table."""
    return _authorization_predicate(scope, table=table)


def authorization_predicate_for_sessions(
    scope: AuthorizationScope,
    table: str = "sessions",
) -> tuple[str, list]:
    """Generate the parameterized read predicate for a session table."""
    return _authorization_predicate(scope, table=table)


def get_authorization_scope(auth_context: Any) -> AuthorizationScope:
    """Build AuthorizationScope from an auth_context (chainlink #883, #897).

    Extracts the relevant authorization information from an auth_context
    object for use in SAGA read authorization.

    A missing carrier grants nothing beyond explicitly public rows. Internal
    system reads that need wider access must pass an explicit server-created
    admin or trusted-service context; omission is never ambient authority.

    Full-corpus read is copied from the resolved trusted principal's declared
    authority. Trigger strings do not grant read authority.

    Args:
        auth_context: Frozen, server-created ``mimir.models.AuthContext``

    Returns:
        AuthorizationScope with caller's authorization details
    """
    from mimir.models import AuthContext

    # AuthorizationScope is a query value object, not an authority carrier. It
    # is publicly constructible, so accepting one here would let a caller assert
    # is_admin/is_platform_service. Arbitrary duck-typed carriers fail closed for
    # the same reason; read authority must come from the frozen server carrier.
    if type(auth_context) is not AuthContext:
        return AuthorizationScope()

    from mimir.access_control import (
        get_trusted_service_from_auth_context,
        is_admin as check_is_admin,
    )

    principal = getattr(auth_context, "canonical_principal", None) or getattr(auth_context, "principal", None)
    is_admin = check_is_admin(auth_context)
    service = get_trusted_service_from_auth_context(auth_context)

    if service:
        return AuthorizationScope(
            principal=principal,
            is_admin=False,
            is_service=True,
            is_platform_service=service.saga_full_corpus_read,
            readable_domains=service.readable_domains,
            service_canonical=service.canonical,
        )

    return AuthorizationScope(
        principal=principal,
        is_admin=is_admin,
        is_service=False,
        is_platform_service=False,
        readable_domains=(),
        service_canonical=None,
    )


_SHADOW_COUNT_LIMIT = 1000
_SHADOW_TYPE_LIMIT = 8
_SHADOW_SURFACE_LIMIT = 16
_SHADOW_TURN_ACCUMULATORS: dict[str, "_ShadowReadObservation"] = {}
_SHADOW_TURN_ACCUMULATORS_LOCK = threading.Lock()


@dataclass
class _ShadowReadObservation:
    """Non-sensitive counts accumulated for one operation or whole turn."""

    auth_context: Any
    strict_scope: AuthorizationScope
    enforcement_enabled: bool
    resource_counts: dict[str, int] = field(default_factory=dict)
    resource_type_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    probe_failure_counts: dict[str, int] = field(default_factory=dict)
    counts_truncated: bool = False

    def merge_operation(self, authorization: "SagaReadAuthorization") -> None:
        surface = authorization.surface
        type_counts = {
            resource_type: len(ids)
            for resource_type, ids in authorization._would_deny.items()
        }
        operation_count = sum(type_counts.values())
        if operation_count:
            current = self.resource_counts.get(surface, 0)
            self.resource_counts[surface] = min(
                current + operation_count, _SHADOW_COUNT_LIMIT
            )
            by_type = self.resource_type_counts.setdefault(surface, {})
            for resource_type, count in type_counts.items():
                by_type[resource_type] = min(
                    by_type.get(resource_type, 0) + count, _SHADOW_COUNT_LIMIT
                )
            if current + operation_count > _SHADOW_COUNT_LIMIT:
                self.counts_truncated = True
        if authorization._probe_failure_count:
            current_failures = self.probe_failure_counts.get(surface, 0)
            self.probe_failure_counts[surface] = min(
                current_failures + authorization._probe_failure_count,
                _SHADOW_COUNT_LIMIT,
            )
            if (
                current_failures + authorization._probe_failure_count
                > _SHADOW_COUNT_LIMIT
            ):
                self.counts_truncated = True
        self.counts_truncated = (
            self.counts_truncated or authorization._counts_truncated
        )


def _shadow_event_payload(
    observation: _ShadowReadObservation,
    *,
    turn_id: str | None,
) -> dict[str, Any]:
    """Build the bounded, non-sensitive event shared by both emission paths."""
    from mimir.models import AuthContext

    context = (
        observation.auth_context
        if type(observation.auth_context) is AuthContext
        else None
    )
    surfaces = sorted(
        set(observation.resource_counts) | set(observation.probe_failure_counts)
    )
    visible_surfaces = surfaces[:_SHADOW_SURFACE_LIMIT]
    resource_counts = {
        surface: observation.resource_counts.get(surface, 0)
        for surface in visible_surfaces
        if observation.resource_counts.get(surface, 0)
    }
    resource_type_counts = {
        surface: dict(
            sorted(observation.resource_type_counts.get(surface, {}).items())[
                :_SHADOW_TYPE_LIMIT
            ]
        )
        for surface in visible_surfaces
        if observation.resource_type_counts.get(surface)
    }
    probe_failure_counts = {
        surface: observation.probe_failure_counts.get(surface, 0)
        for surface in visible_surfaces
        if observation.probe_failure_counts.get(surface, 0)
    }
    total = sum(resource_counts.values())
    probe_failures = sum(probe_failure_counts.values())
    principal = None
    if context is not None:
        principal = context.canonical_principal or context.principal
    caller_kind = (
        "service" if observation.strict_scope.is_service
        else "admin" if observation.strict_scope.is_admin
        else "user" if principal
        else "unknown"
    )
    mode = "enforced" if observation.enforcement_enabled else "shadow"
    would_block = total > 0 and mode == "shadow"
    blocked = total > 0 and mode == "enforced"
    payload = {
        "surface": visible_surfaces[0] if len(visible_surfaces) == 1 else "multiple",
        "surfaces": visible_surfaces,
        "allowed": not blocked,
        "status": (
            "blocked" if blocked else "would_block" if would_block else "probe_failed"
        ),
        "reason": (
            "saga_read_policy_excluded_candidates"
            if blocked
            else "saga_read_policy_would_exclude_candidates"
            if would_block
            else "saga_read_policy_probe_failed"
        ),
        "mode": mode,
        "enforcement_enabled": observation.enforcement_enabled,
        "is_shadow_decision": not observation.enforcement_enabled,
        "would_block": would_block,
        "risk_direction": "over_serving",
        "observation_stage": "pre_rrf_candidates",
        "resource_count": min(total, _SHADOW_COUNT_LIMIT),
        # Keying this field by surface makes the cutover report's required
        # surface/caller/trigger breakdown obtainable without retaining IDs.
        "resource_counts": resource_counts,
        "resource_type_counts": resource_type_counts,
        "resource_types": sorted(
            {
                resource_type
                for by_type in resource_type_counts.values()
                for resource_type in by_type
            }
        ),
        "probe_failure_count": min(probe_failures, _SHADOW_COUNT_LIMIT),
        "probe_failure_counts": probe_failure_counts,
        "counts_truncated": (
            observation.counts_truncated
            or len(surfaces) > _SHADOW_SURFACE_LIMIT
            or total > _SHADOW_COUNT_LIMIT
            or probe_failures > _SHADOW_COUNT_LIMIT
        ),
        "principal": principal,
        "caller_kind": caller_kind,
        # Retain the original name while downstream reports migrate to the more
        # explicit caller_kind terminology from chainlink #1115.
        "principal_kind": caller_kind,
        "roles": list(context.roles[:16]) if context is not None else [],
        "service_principal": observation.strict_scope.service_canonical,
        "trigger": context.trigger if context is not None else None,
        "event_ingress": context.event_ingress if context is not None else None,
        "policy_version": context.policy_version if context is not None else None,
    }
    if turn_id:
        payload["turn_id"] = turn_id
        payload["aggregation"] = "one_event_per_turn_by_surface"
        payload["sampling"] = "none"
    else:
        payload["aggregation"] = "one_event_per_read_operation"
        payload["sampling"] = "none"
    return payload


def _emit_saga_event(event_type: str, **payload: Any) -> None:
    """Emit SAGA telemetry best-effort through the package's single seam."""
    try:
        from mimir.event_logger import log_event_sync

        log_event_sync(event_type, **payload)
    except Exception as exc:  # noqa: BLE001 - telemetry cannot affect SAGA results
        log.debug("saga event failed event_type=%s: %s", event_type, exc)


def _emit_shadow_observation(
    observation: _ShadowReadObservation,
    *,
    turn_id: str | None,
) -> None:
    """Emit best-effort telemetry without changing the read result."""
    _emit_saga_event(
        "saga_read_would_block",
        **_shadow_event_payload(observation, turn_id=turn_id),
    )


def finalize_shadow_read_turn(turn: Any) -> None:
    """Flush one aggregate after all SAGA read surfaces in a turn complete."""
    turn_id = getattr(turn, "turn_id", None)
    if not turn_id:
        return
    with _SHADOW_TURN_ACCUMULATORS_LOCK:
        observation = _SHADOW_TURN_ACCUMULATORS.pop(turn_id, None)
    if observation is not None:
        _emit_shadow_observation(observation, turn_id=turn_id)


@dataclass
class SagaReadAuthorization:
    """Strict SAGA read policy plus the operation's effective selection mode.

    The strict scope is always derived from the immutable server carrier and is
    never widened. Compatibility mode changes only the SQL predicate used to
    select rows; the strict predicate remains available for the counterfactual
    shadow decision.
    """

    auth_context: Any
    surface: str
    strict_scope: AuthorizationScope = field(init=False)
    enforcement_enabled: bool = field(init=False)
    _would_deny: dict[str, set[Any]] = field(default_factory=dict, init=False)
    _counts_truncated: bool = field(default=False, init=False)
    _probe_failure_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        from mimir.models import AuthContext

        self.strict_scope = get_authorization_scope(self.auth_context)
        # Deliberately fail toward compatibility/over-serving: only the exact,
        # frozen server carrier can opt into enforcement. If AuthContext ever
        # stops being frozen, strict filtering becomes unreachable rather than
        # accepting a mutable authority carrier; shadow telemetry exposes that.
        self.enforcement_enabled = bool(
            type(self.auth_context) is AuthContext
            and AuthContext.__dataclass_params__.frozen
            and self.auth_context.enforcement_enabled is True
        )

    def strict_predicate(self, table: str) -> tuple[str, list]:
        return authorization_predicate(self.strict_scope, table=table)

    def selection_predicate(self, table: str) -> tuple[str, list]:
        if not self.enforcement_enabled:
            return "1=1", []
        return self.strict_predicate(table)

    def observe_selected(
        self,
        conn: sqlite3.Connection,
        resource_type: str,
        table: str,
        resource_ids: list[Any] | tuple[Any, ...] | set[Any],
        *,
        id_column: str = "id",
    ) -> None:
        """Record selected rows that strict enforcement would have removed."""
        try:
            strict_where, strict_params = self.strict_predicate(table)
            denied = self._would_deny.setdefault(resource_type, set())
            chunk: list[Any] = []

            def inspect_chunk() -> None:
                if not chunk:
                    return
                unique = list(dict.fromkeys(chunk))
                placeholders = ",".join("?" for _ in unique)
                rows = conn.execute(
                    f"SELECT {table}.{id_column} FROM {table} "
                    f"WHERE {table}.{id_column} IN ({placeholders}) AND {strict_where}",
                    [*unique, *strict_params],
                ).fetchall()
                allowed = {row[0] for row in rows}
                for resource_id in unique:
                    if resource_id in allowed or resource_id in denied:
                        continue
                    if len(denied) < _SHADOW_COUNT_LIMIT:
                        denied.add(resource_id)
                    else:
                        self._counts_truncated = True

            for resource_id in resource_ids:
                chunk.append(resource_id)
                if len(chunk) == 400:
                    inspect_chunk()
                    chunk.clear()
            inspect_chunk()
            if not denied:
                self._would_deny.pop(resource_type, None)
        except Exception as exc:  # noqa: BLE001 - shadow evaluation is telemetry
            if not self._would_deny.get(resource_type):
                self._would_deny.pop(resource_type, None)
            self.observe_probe_failure()
            log.debug("saga shadow read evaluation failed: %s", exc)

    def observe_probe_failure(self) -> None:
        """Count a failed strict counterfactual without exposing failure detail."""
        if self._probe_failure_count == _SHADOW_COUNT_LIMIT:
            self._counts_truncated = True
            return
        self._probe_failure_count += 1

    def observe_would_deny(self, resource_type: str, resource_ids: set[Any]) -> None:
        """Record counterfactual denials already computed by a read adapter."""
        if resource_ids:
            denied = self._would_deny.setdefault(resource_type, set())
            for resource_id in resource_ids:
                if len(denied) < _SHADOW_COUNT_LIMIT:
                    denied.add(resource_id)
                elif resource_id not in denied:
                    self._counts_truncated = True

    def finalize(self) -> None:
        """Accumulate per-turn telemetry or emit one standalone read event."""
        if not self._would_deny and not self._probe_failure_count:
            return
        try:
            from mimir._context import get_current_turn

            turn = get_current_turn()
            turn_id = getattr(turn, "turn_id", None)
            if turn_id:
                with _SHADOW_TURN_ACCUMULATORS_LOCK:
                    observation = _SHADOW_TURN_ACCUMULATORS.get(turn_id)
                    if observation is None:
                        observation = _ShadowReadObservation(
                            auth_context=self.auth_context,
                            strict_scope=self.strict_scope,
                            enforcement_enabled=self.enforcement_enabled,
                        )
                        _SHADOW_TURN_ACCUMULATORS[turn_id] = observation
                    observation.merge_operation(self)
                    if len(_SHADOW_TURN_ACCUMULATORS) > 4096:
                        # Normal turn teardown flushes entries. This is only a
                        # safety bound for abnormal turns that never reset.
                        for stale in list(_SHADOW_TURN_ACCUMULATORS)[:2048]:
                            _SHADOW_TURN_ACCUMULATORS.pop(stale, None)
                return
        except Exception as exc:  # noqa: BLE001 - telemetry cannot affect reads
            log.debug("saga shadow read accumulation failed: %s", exc)
            turn_id = None

        observation = _ShadowReadObservation(
            auth_context=self.auth_context,
            strict_scope=self.strict_scope,
            enforcement_enabled=self.enforcement_enabled,
        )
        observation.merge_operation(self)
        _emit_shadow_observation(observation, turn_id=turn_id)


def intersect_acl(acls: list[Ownership]) -> Ownership:
    """Intersect multiple ACLs to compute the most restrictive common authority.

    This is a fail-closed operation: the result is the intersection of all
    input ACLs. If any source has ambiguous, missing, or legacy provenance,
    the result defaults to service/admin-only (legacy_admin visibility).

    Intersection rules:
    - owner_principal: all sources must have the same non-legacy owner.
      Mixed owners → legacy_admin.
    - origin_domain: all sources must have the same domain.
      Mixed domains → None (becomes legacy_admin).
    - origin_channel: all sources must have the same channel.
      Mixed channels → None (becomes legacy_admin).
    - visibility: most restrictive wins (public < private < service < legacy_admin).
    - provenance: union of all source provenances; if any source lacks
      provenance (empty dict), result has empty provenance (becomes legacy_admin).

    A source is considered "ambiguous" if:
    - Its owner_principal is legacy_admin (pre-v7 data)
    - Its visibility is legacy_admin
    - Its provenance is empty or missing
    - It has mixed owner/domain/channel with other sources

    Args:
        acls: List of Ownership objects to intersect.

    Returns:
        The intersected Ownership. Always valid (never None), but may have
        restrictive visibility/owner indicating service/admin-only access.
    """
    if not acls:
        return Ownership()

    first = acls[0]

    owner_principal = first.owner_principal
    origin_channel = first.origin_channel
    channel_mismatch = False
    origin_domain = first.origin_domain
    provenance = dict(first.provenance)
    vis_order = [
        Visibility.PUBLIC,
        Visibility.PRIVATE,
        Visibility.SERVICE,
        Visibility.LEGACY_ADMIN,
    ]

    def _visibility_rank(value: str) -> int:
        try:
            return vis_order.index(value)
        except ValueError:
            return len(vis_order) - 1

    visibility = vis_order[_visibility_rank(first.visibility)]

    for acl in acls[1:]:
        if acl.owner_principal != owner_principal:
            owner_principal = OwnerPrincipal.LEGACY_ADMIN

        if acl.origin_domain != origin_domain:
            origin_domain = None

        if acl.origin_channel != origin_channel:
            channel_mismatch = True

        visibility = vis_order[
            max(_visibility_rank(visibility), _visibility_rank(acl.visibility))
        ]

        if not acl.provenance:
            provenance = {}
        elif provenance:
            provenance = {**provenance, **acl.provenance}

    if (
        owner_principal == OwnerPrincipal.LEGACY_ADMIN
        or channel_mismatch
        or origin_domain is None
        or not provenance
        or visibility == Visibility.LEGACY_ADMIN
    ):
        return Ownership(
            owner_principal=OwnerPrincipal.LEGACY_ADMIN,
            origin_channel=None,
            origin_domain=None,
            visibility=Visibility.LEGACY_ADMIN,
            provenance={},
        )

    return Ownership(
        owner_principal=owner_principal,
        origin_channel=origin_channel,
        origin_domain=origin_domain,
        visibility=visibility,
        provenance=provenance,
    )


def intersect_acl_from_rows(
    rows: list[dict],
    owner_col: str = "owner_principal",
    domain_col: str = "origin_domain",
    visibility_col: str = "visibility",
    provenance_col: str = "provenance",
) -> Ownership:
    """Intersect ACLs from database rows.

    A convenience wrapper around intersect_acl that extracts ownership
    fields from database row dictionaries.

    Handles missing columns gracefully (treats missing as legacy_admin).
    """
    if not rows:
        return Ownership()

    def row_to_ownership(row: dict) -> Ownership:
        try:
            provenance = {}
            prov_str = row.get(provenance_col)
            if prov_str:
                if isinstance(prov_str, str):
                    provenance = json.loads(prov_str)
                elif isinstance(prov_str, dict):
                    provenance = prov_str
        except (json.JSONDecodeError, TypeError):
            provenance = {}

        owner = row.get(owner_col, OwnerPrincipal.LEGACY_ADMIN)
        if not owner:
            owner = OwnerPrincipal.LEGACY_ADMIN

        domain = row.get(domain_col)

        visibility = row.get(visibility_col, Visibility.LEGACY_ADMIN)
        if not visibility:
            visibility = Visibility.LEGACY_ADMIN

        return Ownership(
            owner_principal=owner,
            origin_channel=row.get("origin_channel"),
            origin_domain=domain,
            visibility=visibility,
            provenance=provenance,
        )

    return intersect_acl([row_to_ownership(r) for r in rows])



__all__ = [
    "Visibility",
    "OwnerPrincipal",
    "Ownership",
    "DEFAULT_VISIBILITY",
    "DEFAULT_OWNER",
    "is_user_accessible",
    "AuthorizationScope",
    "authorization_predicate",
    "authorization_predicate_for_triples",
    "authorization_predicate_for_sessions",
    "get_authorization_scope",
    "SagaReadAuthorization",
    "finalize_shadow_read_turn",
    "intersect_acl",
    "intersect_acl_from_rows",
]
