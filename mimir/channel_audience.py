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
    _session_store: SessionStore | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _session_audiences: dict[
        tuple[str, str], tuple[tuple[object, object], Audience]
    ] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    def __post_init__(self) -> None:
        if self.identity_resolver is None:
            resolver = IdentityResolver(self.home)
            resolver.reload()
            object.__setattr__(self, "identity_resolver", resolver)

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
                key = (session_id, principal)
                store = self._session_store
                if store is None:
                    store = SessionStore(self.home)
                    object.__setattr__(self, "_session_store", store)
                journal, metadata = store.paths(session_id)

                def signature(path: Path) -> object:
                    try:
                        info = path.lstat()
                    except OSError:
                        return None
                    return (info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid)

                signatures = (signature(metadata), signature(journal))
                cached = self._session_audiences.get(key)
                if cached is not None and cached[0] == signatures:
                    return cached[1]
                try:
                    record = store.load_owned(session_id, principal)
                    audience: Audience = frozenset({record.owner_principal})
                except Exception:
                    audience = None
                self._session_audiences[key] = (signatures, audience)
                return audience
            self.identity_resolver.reload_if_changed()
            identity = self.identity_resolver.identity(principal)
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
