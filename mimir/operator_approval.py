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
    from .models import AgentEvent, InformationFlowLabels, InformationFlowState, SourceLabel


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
    turn_id: str | None = None
    sink_category: str | None = None
    request_carrier: "InformationFlowLabels | None" = None
    ifc_state: "InformationFlowState | None" = None
    request_source_arrival_ordinal: int | None = None


@dataclass(frozen=True)
class ApprovalGrant:
    request_id: str
    channel_id: str
    tool_name: str
    target: str
    operator_principal: str
    granted_at: float
    requesting_principal: str | None = None
    turn_id: str | None = None
    sink_category: str | None = None
    request_carrier: "InformationFlowLabels | None" = None
    ifc_state: "InformationFlowState | None" = None
    request_source_arrival_ordinal: int | None = None
    approval_event: "AgentEvent | None" = None
    reply_source: "SourceLabel | None" = None
    request_expires_at: float | None = None


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
    turn_id: str | None = None,
    sink_category: str | None = None,
    request_carrier: "InformationFlowLabels | None" = None,
    ifc_state: "InformationFlowState | None" = None,
    request_source_arrival_ordinal: int | None = None,
) -> tuple[ApprovalRequest | None, str]:
    """Create one pending request for an active operator channel."""
    now = time.monotonic() if now is None else now
    if sink_category is not None and (
        not turn_id
        or not requesting_principal
        or request_carrier is None
        or ifc_state is None
        or request_source_arrival_ordinal is None
    ):
        return None, "invalid_category_binding"
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
            turn_id=turn_id,
            sink_category=sink_category,
            request_carrier=request_carrier,
            ifc_state=ifc_state,
            request_source_arrival_ordinal=request_source_arrival_ordinal,
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


def consume_grant(
    channel_id: str,
    tool_name: str,
    target: str,
    *,
    request_id: str | None = None,
    turn_id: str | None = None,
    requesting_principal: str | None = None,
    sink_category: str | None = None,
    request_carrier: "InformationFlowLabels | None" = None,
    ifc_state: "InformationFlowState | None" = None,
    request_source_arrival_ordinal: int | None = None,
    approval_event: "AgentEvent | None" = None,
    reply_source: "SourceLabel | None" = None,
) -> ApprovalGrant | None:
    """Take one grant, spending category grants even when a binding mismatches."""
    with _LOCK:
        grant_id = next((
            candidate_id
            for candidate_id, grant in _GRANTS.items()
            if grant.channel_id == channel_id
            and grant.tool_name == tool_name
            and grant.target == target
        ), None)
        if grant_id is None and request_id is not None:
            candidate = _GRANTS.get(request_id)
            if candidate is not None and candidate.sink_category is not None:
                grant_id = request_id
        if grant_id is None:
            return None
        taken = _GRANTS.pop(grant_id)
        if taken.sink_category is None:
            return taken
        matches = (
            channel_id == taken.channel_id
            and tool_name == taken.tool_name
            and target == taken.target
            and request_id == taken.request_id
            and turn_id == taken.turn_id
            and requesting_principal == taken.requesting_principal
            and sink_category == taken.sink_category
            and request_carrier == taken.request_carrier
            and ifc_state is taken.ifc_state
            and request_source_arrival_ordinal == taken.request_source_arrival_ordinal
            and approval_event is taken.approval_event
            and reply_source == taken.reply_source
        )
        return taken if matches else None


def record_authenticated_response(
    event: "AgentEvent",
    resolver: "IdentityResolver | None",
    *,
    now: float | None = None,
    approval_event: "AgentEvent | None" = None,
    reply_source: "SourceLabel | None" = None,
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
        if request.sink_category is not None and reply_source is None:
            return "invalid_category_response_binding"
        _GRANTS[request.request_id] = ApprovalGrant(
            request_id=request.request_id,
            channel_id=request.channel_id,
            tool_name=request.tool_name,
            target=request.target,
            operator_principal=canonical,
            granted_at=now,
            requesting_principal=request.requesting_principal,
            turn_id=request.turn_id,
            sink_category=request.sink_category,
            request_carrier=request.request_carrier,
            ifc_state=request.ifc_state,
            request_source_arrival_ordinal=request.request_source_arrival_ordinal,
            approval_event=approval_event if approval_event is not None else event,
            reply_source=reply_source,
            request_expires_at=request.expires_at,
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
    for request_id, grant in tuple(_GRANTS.items()):
        if grant.request_expires_at is not None and grant.request_expires_at <= now:
            _GRANTS.pop(request_id, None)
