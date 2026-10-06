"""Server-side registry for typed operator approval replies."""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .identities import IdentityResolver
    from .models import AgentEvent, SourceLabel


_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_RECENT_SECONDS = 24 * 60 * 60
_REPLY = re.compile(r"^(approve|decline)(?:\s+([a-z]+-[a-z2-7]{4})(?:\s*:\s*(.*))?)?$", re.I | re.S)
Resolver = Callable[[str, str | None, "AgentEvent", "IdentityResolver | None", float, "AgentEvent | None", "SourceLabel | None"], str]


@dataclass(frozen=True)
class ApprovalEntry:
    approval_id: str
    kind: str
    channel_id: str
    description: str
    created_at: float
    expires_at: float
    resolver: Resolver
    prompt_message_id: str | None = None
    supports_edits: bool = False
    inject_into_turn: bool = True


@dataclass(frozen=True)
class Resolution:
    status: str
    entry: ApprovalEntry | None = None
    message: str | None = None


_PENDING: dict[str, ApprovalEntry] = {}
# Keep the originating channel with each reserved ID; other channels must
# receive the same answer as an unknown ID.
_RECENT: dict[str, tuple[float, str]] = {}
_EXPIRED: set[str] = set()
_LOCK = threading.Lock()


def mint_id(kind: str, *, excluded: frozenset[str] = frozenset()) -> str:
    """Mint an unused typed ID, including callers' durable reservations."""
    if not re.fullmatch(r"[a-z]+", kind):
        raise ValueError("invalid approval kind")
    with _LOCK:
        _expire(time.monotonic())
        for _ in range(1024):
            candidate = f"{kind}-" + "".join(secrets.choice(_ALPHABET) for _ in range(4))
            if candidate not in excluded and candidate not in _PENDING and candidate not in _RECENT:
                return candidate
    raise RuntimeError("approval IDs exhausted")


def _expire(now: float) -> None:
    for approval_id, entry in tuple(_PENDING.items()):
        if entry.expires_at <= now:
            _PENDING.pop(approval_id, None)
            _RECENT[approval_id] = (now + _RECENT_SECONDS, entry.channel_id)
            _EXPIRED.add(approval_id)
    for approval_id, (until, _channel_id) in tuple(_RECENT.items()):
        if until <= now:
            _RECENT.pop(approval_id, None)
            _EXPIRED.discard(approval_id)


def register(
    *, kind: str, channel_id: str, description: str, expires_at: float,
    resolver: Resolver, now: float | None = None, approval_id: str | None = None,
    supports_edits: bool = False, inject_into_turn: bool = True,
) -> ApprovalEntry:
    """Register an in-memory request (persistent kinds re-register on boot)."""
    now = time.monotonic() if now is None else now
    if not re.fullmatch(r"[a-z]+", kind) or not channel_id or expires_at <= now:
        raise ValueError("invalid approval request")
    with _LOCK:
        _expire(now)
        if approval_id is None:
            for _ in range(1024):
                candidate = f"{kind}-" + "".join(secrets.choice(_ALPHABET) for _ in range(4))
                if candidate not in _PENDING and candidate not in _RECENT:
                    approval_id = candidate
                    break
            else:
                raise RuntimeError("approval IDs exhausted")
        if (not re.fullmatch(rf"{kind}-[a-z2-7]{{4}}", approval_id)
                or approval_id in _PENDING or approval_id in _RECENT):
            raise ValueError("approval ID unavailable")
        entry = ApprovalEntry(approval_id, kind, channel_id, " ".join(description.split())[:160], now, expires_at,
                              resolver, supports_edits=supports_edits, inject_into_turn=inject_into_turn)
        _PENDING[approval_id] = entry
        return entry


def set_prompt_message_id(approval_id: str, message_id: str | None) -> None:
    if message_id is None:
        return
    with _LOCK:
        if approval_id in _PENDING:
            _PENDING[approval_id] = replace(_PENDING[approval_id], prompt_message_id=str(message_id))


def cancel(approval_id: str, *, expired: bool = False) -> None:
    with _LOCK:
        entry = _PENDING.pop(approval_id, None)
        if entry is not None:
            _RECENT[approval_id] = (time.monotonic() + _RECENT_SECONDS, entry.channel_id)
            if expired:
                _EXPIRED.add(approval_id)


def restore_uncompleted(entry: ApprovalEntry) -> None:
    """Undo only this entry's transient resolution after durable completion failed."""
    with _LOCK:
        recent = _RECENT.get(entry.approval_id)
        if (recent is not None and recent[1] == entry.channel_id
                and entry.approval_id not in _PENDING
                and entry.expires_at > time.monotonic()):
            _RECENT.pop(entry.approval_id)
            _EXPIRED.discard(entry.approval_id)
            _PENDING[entry.approval_id] = entry


def pending(channel_id: str, *, now: float | None = None) -> tuple[ApprovalEntry, ...]:
    now = time.monotonic() if now is None else now
    with _LOCK:
        _expire(now)
        return tuple(entry for entry in _PENDING.values() if entry.channel_id == channel_id)


def is_non_turn_bound_reply(event: AgentEvent) -> bool:
    """Route named standalone requests (and stale/unknown IDs) before the model.

    Turn-bound requests must remain on the live-turn approval path. Unknown
    IDs are consumed with a registry refusal, never interpreted by the model.
    Authentication and channel binding are still enforced by ``resolve``.
    """
    match = _REPLY.fullmatch((event.content or "").strip())
    if match is None or match.group(2) is None:
        return False
    with _LOCK:
        entry = _PENDING.get(match.group(2).lower())
        return entry is None or not entry.inject_into_turn


def resolve(
    event: AgentEvent, identity_resolver: IdentityResolver | None, *,
    now: float | None = None, approval_event: AgentEvent | None = None,
    reply_source: SourceLabel | None = None,
) -> Resolution:
    """Only authenticated bridge-origin operator events can resolve requests."""
    from .operator_approval import _is_authenticated_operator

    if not _is_authenticated_operator(event, identity_resolver):
        return Resolution("unauthenticated_operator")
    now = time.monotonic() if now is None else now
    match = _REPLY.fullmatch((event.content or "").strip())
    if match is None:
        return Resolution("not_an_approval_response")
    decision, named_id, edit = match.groups()
    named_id = named_id.lower() if named_id else None
    with _LOCK:
        _expire(now)
        entries = [entry for entry in _PENDING.values() if entry.channel_id == event.channel_id]
        if named_id:
            entry = _PENDING.get(named_id)
            if entry is None or entry.channel_id != event.channel_id:
                recent = _RECENT.get(named_id)
                if (recent is not None and recent[1] == event.channel_id
                        and entry is None and named_id not in _EXPIRED):
                    return Resolution("already_resolved", message=f"already resolved {named_id}")
                return Resolution("no_pending_request", message=f"no pending request {named_id}")
        else:
            reference = event.extra.get("reply_to_message_id") if event.source == "discord" else None
            referenced = next((e for e in entries if reference is not None
                               and e.prompt_message_id == str(reference)), None)
            if referenced is not None:
                entry = referenced
            elif len(entries) == 1:
                entry = entries[0]
            elif len(entries) > 1:
                listing = "\n".join(f"{e.approval_id}: {e.description}" for e in entries)
                return Resolution("ambiguous", message=f"Pending approvals:\n{listing}")
            else:
                return Resolution("no_pending_request")
        if edit is not None and not entry.supports_edits:
            return Resolution("not_an_approval_response")
        _PENDING.pop(entry.approval_id)
        _RECENT[entry.approval_id] = (now + _RECENT_SECONDS, entry.channel_id)
    status = entry.resolver(decision.lower(), edit, event, identity_resolver, now,
                            approval_event, reply_source)
    return Resolution(status, entry)
