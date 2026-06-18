"""Admin user/key management for the Users page (github #563, #726).

Backs the admin-only Users page: list identities and mint/rotate/revoke
per-user web keys. Every route is admin-gated by the ``/api/v1/admin/`` prefix
in the auth middleware (server.py). These helpers add a second, value-blind
guarantee: the *only* key material that ever leaves the server is the raw key
returned ONCE at mint time. The list never returns a raw key OR its hash —
just whether a key exists.
"""

from __future__ import annotations

from typing import Any

from .identities import WEB_KEY_ALIAS_PREFIX, IdentityResolver


def build_users_payload(resolver: IdentityResolver) -> dict[str, Any]:
    """List identities for the admin Users page.

    Returns NO key material — not the raw key, not the ``webkey:`` hash —
    only ``has_web_key`` (whether one is set), so the page can show login
    status + offer rotate/revoke without ever exposing credential bytes."""
    users: list[dict[str, Any]] = []
    for ident in resolver.all_identities():
        users.append(
            {
                "canonical": ident.canonical,
                "display_name": ident.display_name,
                "roles": list(ident.access.roles),
                "is_admin": ident.access.is_admin,
                "has_web_key": any(
                    alias.startswith(WEB_KEY_ALIAS_PREFIX) for alias in ident.aliases
                ),
            }
        )
    return {"users": users}


def roles_for_request(role: Any) -> list[str] | None:
    """Map a Users-page ``role`` field to ``access.roles`` for issue_web_key.

    ``"admin"`` → ``["user", "admin"]`` (admin implies user), ``"user"`` →
    ``["user"]``, ``None``/absent → ``None`` (rotate the key, leave roles
    untouched). Raises ValueError on any other value so the endpoint can 400."""
    if role is None:
        return None
    if role == "admin":
        return ["user", "admin"]
    if role == "user":
        return ["user"]
    raise ValueError(f"invalid role {role!r}; expected 'user', 'admin', or omitted")
