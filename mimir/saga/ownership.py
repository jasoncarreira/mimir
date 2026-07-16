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
        """Convert to column values for SQL insertion."""
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
    """Authorization scope for SAGA read operations (chainlink #883).

    Captures the caller's identity and permissions to determine what
    resources they can read from SAGA (atoms, sessions, triples).

    Attributes:
        principal: The caller's principal identifier (e.g., "user:123")
        is_admin: Whether the caller has admin role
        is_service: Whether the caller is a trusted service
        readable_domains: Tuple of domain names the service can read
        service_canonical: Canonical name of the service (if is_service)
    """
    principal: str | None = None
    is_admin: bool = False
    is_service: bool = False
    readable_domains: tuple[str, ...] = ()
    service_canonical: str | None = None


def _authorization_predicate(
    scope: AuthorizationScope,
    *,
    table: str,
) -> tuple[str, list]:
    """Build the shared owner/visibility/domain predicate for a resource table."""
    if scope.is_admin:
        return ("1=1", [])

    # Every grant is an alternative.  Combining the owner grant with the public
    # grant using AND collapses ``private + owned`` to public-only and makes a
    # user's own private rows unreadable.  Keep one OR group so no grant narrows
    # another grant accidentally.
    grants = [f"{table}.visibility = ?"]
    params: list = [Visibility.PUBLIC.value]

    if scope.principal:
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
    admins can read everything; trusted services can read public rows, rows in
    their readable domains, and their owned rows; regular users can read public
    rows and their own rows.  Capability names never widen readable domains.
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
    """Build AuthorizationScope from an auth_context (chainlink #883).

    Extracts the relevant authorization information from an auth_context
    object for use in SAGA read authorization.

    A missing carrier grants nothing beyond explicitly public rows. Internal
    system reads that need wider access must pass an explicit server-created
    admin or trusted-service context; omission is never ambient authority.

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

    principal = getattr(auth_context, "principal", None)
    is_admin = check_is_admin(auth_context)
    service = get_trusted_service_from_auth_context(auth_context)

    if service:
        return AuthorizationScope(
            principal=principal,
            is_admin=False,
            is_service=True,
            readable_domains=service.readable_domains,
            service_canonical=service.canonical,
        )

    return AuthorizationScope(
        principal=principal,
        is_admin=is_admin,
        is_service=False,
        readable_domains=(),
        service_canonical=None,
    )


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
]
