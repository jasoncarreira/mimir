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

    if len(acls) == 1:
        return acls[0]

    first = acls[0]

    owner_principal = first.owner_principal
    origin_domain = first.origin_domain
    visibility = first.visibility
    provenance = dict(first.provenance)

    for acl in acls[1:]:
        if acl.owner_principal != owner_principal:
            owner_principal = OwnerPrincipal.LEGACY_ADMIN

        if acl.origin_domain != origin_domain:
            origin_domain = None

        if acl.visibility != visibility:
            vis_order = [
                Visibility.PUBLIC,
                Visibility.PRIVATE,
                Visibility.SERVICE,
                Visibility.LEGACY_ADMIN,
            ]
            vis_idx = max(
                vis_order.index(v) if v in vis_order else 0
                for v in [visibility, acl.visibility]
            )
            visibility = vis_order[vis_idx]

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
    "intersect_acl",
    "intersect_acl_from_rows",
]
