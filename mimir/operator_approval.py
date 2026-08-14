"""Server-owned operator approval requests and one-shot consent records."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .worklink.continuation import (
    HTTP_EVENT_INGRESS_EXTRA_KEY,
    HTTP_EVENT_INGRESS_EXTRA_VALUE,
)

if TYPE_CHECKING:
    from .identities import IdentityResolver
    from .models import AgentEvent


APPROVAL_TIMEOUT_SECONDS = 300.0
_APPROVE_REPLIES = frozenset({"approve"})
_DECLINE_REPLIES = frozenset({"decline"})


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    channel_id: str
    tool_name: str
    target: str
    requesting_principal: str | None
    expires_at: float


@dataclass(frozen=True)
class ApprovalGrant:
    request_id: str
    channel_id: str
    tool_name: str
    target: str
    operator_principal: str
    granted_at: float


_PENDING: dict[str, ApprovalRequest] = {}
_GRANTS: dict[str, ApprovalGrant] = {}
_LOCK = threading.Lock()


def create_request(
    *,
    channel_id: str,
    tool_name: str,
    target: str,
    requesting_principal: str | None,
    now: float | None = None,
) -> tuple[ApprovalRequest | None, str]:
    """Create one pending request for an active operator channel."""
    now = time.monotonic() if now is None else now
    with _LOCK:
        _discard_expired_locked(now)
        if channel_id in _PENDING:
            return None, "request_already_pending"
        if any(grant.channel_id == channel_id for grant in _GRANTS.values()):
            return None, "grant_already_recorded"
        request = ApprovalRequest(
            request_id=uuid.uuid4().hex,
            channel_id=channel_id,
            tool_name=tool_name,
            target=target,
            requesting_principal=requesting_principal,
            expires_at=now + APPROVAL_TIMEOUT_SECONDS,
        )
        _PENDING[channel_id] = request
        return request, "pending"


def cancel_request(request_id: str) -> None:
    """Cancel ``request_id`` without affecting a newer request."""
    with _LOCK:
        for channel_id, request in tuple(_PENDING.items()):
            if request.request_id == request_id:
                _PENDING.pop(channel_id, None)
                return


def pending_request(channel_id: str, *, now: float | None = None) -> ApprovalRequest | None:
    now = time.monotonic() if now is None else now
    with _LOCK:
        _discard_expired_locked(now)
        return _PENDING.get(channel_id)


def recorded_grant(
    channel_id: str, tool_name: str, target: str, *, now: float | None = None,
) -> ApprovalGrant | None:
    """Return the exact unconsumed grant, if one exists."""
    now = time.monotonic() if now is None else now
    with _LOCK:
        _discard_expired_locked(now)
        return next((
            grant
            for grant in _GRANTS.values()
            if grant.channel_id == channel_id
            and grant.tool_name == tool_name
            and grant.target == target
        ), None)


def consume_grant(channel_id: str, tool_name: str, target: str) -> ApprovalGrant | None:
    """Consume one exact grant. This leaf does not use it to widen any policy."""
    with _LOCK:
        for request_id, grant in tuple(_GRANTS.items()):
            if (
                grant.channel_id == channel_id
                and grant.tool_name == tool_name
                and grant.target == target
            ):
                return _GRANTS.pop(request_id)
    return None


def record_authenticated_response(
    event: "AgentEvent", resolver: "IdentityResolver | None", *, now: float | None = None,
) -> str:
    """Apply an authenticated bridge reply to the channel's pending request.

    This is called only by the dispatcher's authorized injection path. Message
    text selects approve/decline, but identity and ingress provenance come from
    the server-owned ``AgentEvent`` and ``IdentityResolver``.
    """
    now = time.monotonic() if now is None else now
    reply = " ".join((event.content or "").strip().lower().split())
    with _LOCK:
        _discard_expired_locked(now)
        request = _PENDING.get(event.channel_id)
        if request is None:
            return "no_pending_request"
        if reply not in _APPROVE_REPLIES | _DECLINE_REPLIES:
            return "not_an_approval_response"
        if not _is_authenticated_operator(event, resolver):
            return "unauthenticated_operator"
        _PENDING.pop(event.channel_id, None)
        if reply in _DECLINE_REPLIES:
            return "declined"
        canonical = resolver.resolve(event.author) if resolver is not None else None
        if not canonical:
            return "unauthenticated_operator"
        _GRANTS[request.request_id] = ApprovalGrant(
            request_id=request.request_id,
            channel_id=request.channel_id,
            tool_name=request.tool_name,
            target=request.target,
            operator_principal=canonical,
            granted_at=now,
        )
        return "granted"


def clear_channel(channel_id: str) -> None:
    """Drop all approval state when its in-flight turn ends or is replaced."""
    with _LOCK:
        _PENDING.pop(channel_id, None)
        for request_id, grant in tuple(_GRANTS.items()):
            if grant.channel_id == channel_id:
                _GRANTS.pop(request_id, None)


def _is_authenticated_operator(
    event: "AgentEvent", resolver: "IdentityResolver | None",
) -> bool:
    if (
        resolver is None
        or event.trigger != "user_message"
        or not event.author
        or not (event.source or "").strip()
        or (event.source or "").strip().lower() in {"api", "stdin", "web"}
        or event.extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) == HTTP_EVENT_INGRESS_EXTRA_VALUE
    ):
        return False
    access = resolver.access_metadata(event.author)
    return access.is_admin and not access.is_service and resolver.resolve(event.author) is not None


def _discard_expired_locked(now: float) -> None:
    for channel_id, request in tuple(_PENDING.items()):
        if request.expires_at <= now:
            _PENDING.pop(channel_id, None)
