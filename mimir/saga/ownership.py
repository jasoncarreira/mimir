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


__all__ = [
    "Visibility",
    "OwnerPrincipal",
    "Ownership",
    "DEFAULT_VISIBILITY",
    "DEFAULT_OWNER",
    "is_user_accessible",
]
