"""Server-owned, operator-only ingest trust decisions for chat and ACP."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any

from . import approval_requests
from .event_logger import log_durable_event_sync
from .models import AuthContext, InformationFlowLabels, InformationFlowState, SourceKind
from .operator_approval import _is_authenticated_operator

_TRUST_REPLY = re.compile(r"^(trust|keep)\s+(trust-[a-z2-7]{4})$", re.I)
_DOMAINS = frozenset({
    "filesystem", "channel", "repository", "client_provider", "mcp",
    "shell", "web", "memory", "saga", "channel_history",
    "channel_metadata", "schedule_metadata", "shell_jobs", "turn_history",
    "commitments", "worklink", "coding_worker", "usage", "schedules",
    "self_state", "recent_activity", "channel_memory", "proposals",
    "identities", "skills", "feedback",
})
TRUST_LIFETIME_SECONDS = 10 * 60


def operator_ingest_allowed(auth: Any, *, surface: str) -> bool:
    """Match the former live-clear identity and ingress boundary."""
    return (
        isinstance(auth, AuthContext)
        and "admin" in auth.roles
        and isinstance(auth.principal, str) and bool(auth.principal.strip())
        and isinstance(auth.canonical_principal, str) and bool(auth.canonical_principal.strip())
        and not auth.is_service and "service" not in auth.roles
        and auth.service_authority is None
        and not auth.principal.strip().lower().startswith("service:")
        and not auth.canonical_principal.strip().lower().startswith("service:")
        and auth.trigger == "user_message"
        and auth.origin_trigger in (None, "user_message", "acp_session")
        and auth.event_ingress is None
        and (auth.origin_trigger == "acp_session") == (surface == "acp")
        and isinstance(auth.ifc_state, InformationFlowState)
    )


def source_groups(snapshot: InformationFlowLabels) -> list[dict[str, Any]]:
    """Only fixed-taxonomy counts, never source-supplied text or IDs."""
    counts = Counter((
        source.domain if source.domain in _DOMAINS and source.domain_qualifier is None else "other",
        str(source.source_kind) if source.source_kind in SourceKind._value2member_map_ else "other",
    ) for source in snapshot.sources if source.has_untrusted_active_ingest)
    return [dict(domain=domain, source_kind=kind, count=count)
            for (domain, kind), count in sorted(counts.items())]


def summary(snapshot: InformationFlowLabels) -> str:
    groups = source_groups(snapshot)
    return ", ".join(f"{item['domain']}: {item['count']} {item['source_kind']}"
                     for item in groups) or "unknown sources"


def audit_decision(auth: AuthContext, snapshot: InformationFlowLabels, *,
                   surface: str, cleared: bool) -> bool:
    try:
        log_durable_event_sync(
            "ifc_ingest_taint_cleared" if cleared else "ifc_ingest_taint_clear_declined",
            surface=surface,
            authenticated_admin={"principal": auth.principal,
                                 "canonical_principal": auth.canonical_principal},
            source_groups=source_groups(snapshot),
            source_count=sum(item["count"] for item in source_groups(snapshot)),
        )
        return True
    except Exception:
        return False


async def complete_reply(event: Any, resolution: Any, resolver: Any) -> str:
    """The sole chat setter: an authenticated, exact-channel, one-shot reply."""
    entry = resolution.entry
    action = event.extra.pop("_trust_action", None)
    match = _TRUST_REPLY.fullmatch((event.content or "").strip())
    if (entry is None or entry.kind != "trust" or entry.channel_id != event.channel_id
            or match is None or match.group(2).lower() != entry.approval_id
            or not _is_authenticated_operator(event, resolver, allow_web=True)
            or action is None or action[0] != entry.approval_id):
        if entry is not None and entry.kind == "trust":
            approval_requests.restore_uncompleted(entry)
        return resolution.message or "no pending request"
    _, decision, auth, expected, ordinal, ctx = action
    if (not operator_ingest_allowed(auth, surface="chat")
            or resolver.resolve(event.author) != auth.canonical_principal
            or auth.channel_id != event.channel_id):
        approval_requests.restore_uncompleted(entry)
        return "no pending request"
    if decision == "keep":
        audit_decision(auth, expected, surface="chat", cleared=False)
        return f"kept untrusted content {entry.approval_id}"
    if not auth.ifc_state.acknowledge_ingest_snapshot(
        expected=expected,
        expected_ordinal=ordinal,
        durable_audit=lambda _sources: audit_decision(auth, expected, surface="chat", cleared=True),
    ):
        return f"trust request expired or new content arrived {entry.approval_id}"
    ctx.trust_cleared_notice = True
    return f"trusted {entry.approval_id}; this channel is clear. Retry the refused call."


def register_chat_request(ctx: Any, auth: Any) -> str | None:
    """Called only for a #1937 veto, from the server's authorization middleware."""
    if (not operator_ingest_allowed(auth, surface="chat")
            or getattr(ctx, "auth_context", None) is None
            or ctx.auth_context.ifc_state is not auth.ifc_state
            or ctx.channel_id != auth.channel_id
            or ctx.trigger != "user_message"
            or ctx.channel_source not in {"discord", "slack", "web"}
            or getattr(ctx, "operator_ingress_event", None) is None
            or not _is_authenticated_operator(ctx.operator_ingress_event,
                                               ctx.identity_resolver, allow_web=True)):
        return None
    expected, ordinal = auth.ifc_state.source_snapshot(auth.ifc_labels)
    if not isinstance(expected, InformationFlowLabels) or not auth.ifc_state.has_untrusted_active_ingest():
        return None
    existing = next((item for item in approval_requests.pending(auth.channel_id)
                     if item.kind == "trust"), None)
    if existing is not None:
        return existing.approval_id

    def resolve(decision: str, edit: str | None, event: Any, identity: Any,
                resolved_at: float, approval_event: Any, reply_source: Any) -> str:
        event.extra["_trust_action"] = (entry.approval_id, decision, auth, expected, ordinal, ctx)
        return "granted" if decision == "trust" else "declined"

    entry = approval_requests.register(
        kind="trust", channel_id=auth.channel_id, description="ingest trust",
        expires_at=time.monotonic() + TRUST_LIFETIME_SECONDS,
        resolver=resolve, inject_into_turn=False,
    )
    sender = getattr(ctx, "trust_notice_sender", None)
    loop = getattr(ctx, "trust_owner_loop", None)
    if sender is None or loop is None or not loop.is_running():
        approval_requests.cancel(entry.approval_id)
        return None
    notice = (f"This turn read untrusted content ({summary(expected)}). "
              f"Reply `trust {entry.approval_id}` to clear it for this channel, "
              f"or `keep {entry.approval_id}` to decline.")
    asyncio.run_coroutine_threadsafe(sender(auth.channel_id, notice), loop)
    return entry.approval_id
