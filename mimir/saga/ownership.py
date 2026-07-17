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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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

    Platform/maintenance services (scheduler, poller, synthesis, system) get
    broad internal read access. Tenant isolation is enforced on their OUTPUTS
    via derivation-ACL intersection (#884) and egress/IFC (#871), NOT by
    restricting maintenance reads. This design decision is intentional:
    - Tenant isolation is enforced by user-facing read scope, derivation-ACL
      intersection on consolidated artifacts, and egress/IFC. A user never sees
      an autonomous turn's raw reads, only its ACL-carrying outputs.
    - A static per-principal readable_domain does not represent WHICH tenant
      a given autonomous op is acting for (dynamic: the session that ended,
      the poll's account), so it aligns with no tenant boundary.
    - readable_domains is reserved for narrow external integration services.

    Attributes:
        principal: The caller's principal identifier (e.g., "user:123")
        is_admin: Whether the caller has admin role
        is_service: Whether the caller is a trusted service
        is_platform_service: Whether the caller is a platform/maintenance service
            with full internal read (scheduler, poller, synthesis, upgrade).
            These services can read the complete internal corpus without becoming
            admins. Tenant isolation is enforced on their outputs.
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
    public and owned rows. readable_domains is reserved for narrow external
    integration services.
    """
    if scope.is_admin:
        return ("1=1", [])

    # Platform/maintenance services (scheduler, poller, synthesis, system) get
    # the full internal read view. This does not widen role or mutation authority;
    # tenant isolation remains enforced on derived outputs.
    if scope.is_platform_service:
        return ("1=1", [])

    # Every grant is an alternative.  Combining the owner grant with the public
    # grant using AND collapses ``private + owned`` to public-only and makes a
    # user's own private rows unreadable.  Keep one OR group so no grant narrows
    # another grant accidentally.
    grants = [f"{table}.visibility = ?"]
    params = [Visibility.PUBLIC.value]

    if scope.principal:
        if scope.principal not in RESERVED_SENTINEL_PRINCIPALS:
            grants.append(f"{table}.owner_principal = ?")
            params.append(scope.principal)

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


PLATFORM_SERVICE_TRIGGERS: frozenset[str] = frozenset({
    "scheduled_tick",
    "poller",
    "saga_session_end",
    "upgrade",
})


def get_authorization_scope(auth_context: Any) -> AuthorizationScope:
    """Build AuthorizationScope from an auth_context (chainlink #883, #897).

    Extracts the relevant authorization information from an auth_context
    object for use in SAGA read authorization.

    A missing carrier grants nothing beyond explicitly public rows. Internal
    system reads that need wider access must pass an explicit server-created
    admin or trusted-service context; omission is never ambient authority.

    Platform/maintenance services (scheduled_tick, poller, saga_session_end, upgrade)
    get full internal read access via is_platform_service=True without acquiring
    admin role. Tenant isolation is enforced on their outputs via #884 + #871.

    Args:
        auth_context: AuthContext from mimir.models or similar

    Returns:
        AuthorizationScope with caller's authorization details
    """
    if isinstance(auth_context, AuthorizationScope):
        return auth_context
    if auth_context is None:
        return AuthorizationScope()

    from mimir.access_control import (
        get_trusted_service_from_auth_context,
        is_admin as check_is_admin,
    )

    principal = getattr(auth_context, "canonical_principal", None) or getattr(auth_context, "principal", None)
    is_admin = check_is_admin(auth_context)
    service = get_trusted_service_from_auth_context(auth_context)

    if service:
        trigger = getattr(auth_context, "trigger", None)
        is_platform = trigger in PLATFORM_SERVICE_TRIGGERS
        return AuthorizationScope(
            principal=principal,
            is_admin=False,
            is_service=True,
            is_platform_service=is_platform,
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
    - visibility: most restrictive wins (public < private < service < legacy_admin).
    - provenance: union of all source provenances; if any source lacks
      provenance (empty dict), result has empty provenance (becomes legacy_admin).

    A source is considered "ambiguous" if:
    - Its owner_principal is legacy_admin (pre-v7 data)
    - Its visibility is legacy_admin
    - Its provenance is empty or missing
    - It has mixed owner/domain with other sources

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

        visibility = vis_order[
            max(_visibility_rank(visibility), _visibility_rank(acl.visibility))
        ]

        if not acl.provenance:
            provenance = {}
        elif provenance:
            provenance = {**provenance, **acl.provenance}

    if (
        owner_principal == OwnerPrincipal.LEGACY_ADMIN
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
        origin_channel=first.origin_channel,
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
    "intersect_acl",
    "intersect_acl_from_rows",
]
