from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .acp.session_store import SessionStore
from .identities import IdentityResolver
from .models import OwnerAttestation, _mint_owner_attestation

Audience = frozenset[str] | None


class ChannelAudienceProvider(Protocol):
    def audience_for(
        self,
        channel_id: str | None,
        *,
        principal: str | None,
    ) -> Audience: ...


@dataclass(frozen=True, slots=True)
class ServerChannelAudienceProvider:
    home: Path
    identity_resolver: IdentityResolver | None = field(
        default=None, repr=False, kw_only=True
    )

    def audience_for(
        self,
        channel_id: str | None,
        *,
        principal: str | None,
    ) -> Audience:
        if not channel_id or not principal:
            return None
        try:
            if channel_id.startswith("acp:"):
                session_id = channel_id.removeprefix("acp:")
                record = SessionStore(self.home).load_owned(session_id, principal)
                return frozenset({record.owner_principal})
            resolver = IdentityResolver(self.home)
            resolver.reload()
            identity = resolver.identity(principal)
            if identity is None or channel_id not in identity.dm_channels.values():
                return None
            return frozenset({identity.canonical})
        except Exception:
            return None


def attest_owner(
    resolver: IdentityResolver | None,
    author: str | None,
    source_channel: str,
) -> OwnerAttestation | None:
    if resolver is None or not author or not source_channel:
        return None
    identity = resolver.identity(author)
    if identity is None:
        return None
    return _mint_owner_attestation(identity.canonical, author, source_channel)
