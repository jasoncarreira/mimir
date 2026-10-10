"""Durable pairing requests resolved by authenticated operator bridge replies."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import approval_requests
from .identities import IdentityResolver
from .identities_populator import approve_pairing, reject_pairing
from .operator_approval import _is_authenticated_operator

_PAIR_REPLY = re.compile(r"^(approve|decline)\s+(pair-[a-z2-7]{4})$", re.I)


def sync_pending(home: Path, channel: str, resolver: IdentityResolver | None) -> None:
    """Re-register live, unexpired IDs on boot, creation and operator replies."""
    if not channel or not isinstance(resolver, IdentityResolver):
        return
    resolver.reload_if_changed()
    now = datetime.now(timezone.utc)
    active: dict[str, tuple[str, float]] = {}
    for ident in resolver.all_identities():
        pairing = ident.pairing
        if pairing is None or pairing.status != "pending" or not pairing.request_id:
            continue
        if not re.fullmatch(r"pair-[a-z2-7]{4}", pairing.request_id):
            continue
        try:
            requested = datetime.fromisoformat(pairing.requested_at or "")
        except ValueError:
            continue
        if requested.tzinfo is None:
            continue
        remaining = (requested + timedelta(days=7) - now).total_seconds()
        if remaining > 0 and pairing.request_id not in active:
            active[pairing.request_id] = (ident.canonical, remaining)

    registered = {entry.approval_id: entry for entry in approval_requests.pending(channel)
                  if entry.kind == "pair"}
    for request_id in registered.keys() - active.keys():
        approval_requests.cancel(request_id, expired=True)
    for request_id, (canonical, remaining) in active.items():
        if request_id in registered:
            continue

        def resolve(decision: str, edit: str | None, event: Any, identity: Any,
                    resolved_at: float, approval_event: Any, reply_source: Any,
                    *, target: str = canonical, expected: str = request_id) -> str:
            event.extra["_pairing_action"] = (target, expected, decision)
            return "granted" if decision == "approve" else "declined"

        try:
            approval_requests.register(
                kind="pair", approval_id=request_id, channel_id=channel,
                description=f"pairing {canonical}", expires_at=time.monotonic() + remaining,
                resolver=resolve, inject_into_turn=False,
            )
        except ValueError:
            # A resolved ID remains reserved by the registry for replay protection.
            continue


async def complete_reply(home: Path, channel: str, event: Any, resolution: Any,
                         resolver: IdentityResolver | None) -> str | None:
    """Commit a registry-selected reply and return a server-owned notice."""
    action = event.extra.pop("_pairing_action", None)
    entry = resolution.entry
    if (entry is None or entry.kind != "pair" or entry.channel_id != channel
            or event.channel_id != channel or not _is_authenticated_operator(event, resolver)
            or _PAIR_REPLY.fullmatch((event.content or "").strip()) is None
            or action is None or action[1] != entry.approval_id):
        if entry is not None and entry.kind == "pair":
            approval_requests.restore_uncompleted(entry)
        return resolution.message or "no pending request"
    canonical, request_id, decision = action
    try:
        if decision == "approve":
            changed = await asyncio.to_thread(
                approve_pairing, home, canonical, roles=("user",),
                pending_only=True, request_id=request_id,
            )
        else:
            changed = await asyncio.to_thread(reject_pairing, home, canonical, request_id=request_id)
    except Exception:
        approval_requests.restore_uncompleted(entry)
        raise
    if not changed:
        return f"no pending request {request_id}"
    await asyncio.to_thread(resolver.reload)
    return (f"approved {request_id}: {canonical} (user)" if decision == "approve"
            else f"declined {request_id}: {canonical}")
