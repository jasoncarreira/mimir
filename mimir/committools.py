"""Commitment-facing MCP tools (Phase 2c).

Four tools so the agent can act on its own commitments from inside a
turn — until Phase 2c shipped, only the operator could resolve
commitments via the ``mimir commitments`` CLI. The agent saw algedonic
events for time-anchored due/expired commitments and the Phase 3
``## Upcoming commitments`` prompt block, but had no way to mark one
done short of asking the operator.

  - commitment_complete — agent followed through (terminal)
  - commitment_snooze   — push to a later time (relative ``for_days``
                          or absolute ``until_unix``)
  - commitment_dismiss  — drop as no longer relevant (terminal)
  - commitment_list     — list active records for the current channel
                          + unbound; useful when the prompt block was
                          truncated by ``max_entries`` or the agent
                          wants to filter by status

All four wrap ``CommitmentsStore`` lifecycle methods. The store's
``_can_apply`` guard handles the already-terminal case — failures come
back as ``is_error`` text blocks (same convention as saga/schedule
tools).
"""

from __future__ import annotations

import json
import time
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._tool_helpers import _content_block, _need, _safe
from .commitments.store import CommitmentsStore


def build_commitment_tools(store: CommitmentsStore) -> list[SdkMcpTool]:
    @tool(
        "commitment_complete",
        "Mark a commitment as completed — you followed through. "
        "Terminal: a completed commitment cannot be re-opened. Pass the "
        "commitment id (e.g. ``c-abc123def0``) shown in the ``## "
        "Upcoming commitments`` prompt block. Optional ``message_id`` "
        "links the completion back to the message that delivered the "
        "promised follow-up. Fails if the commitment is unknown or "
        "already terminal.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "message_id": {"type": "string"},
            },
            "required": ["id"],
        },
    )
    @_safe("commitment_complete")
    async def commitment_complete(args: dict[str, Any]) -> dict[str, Any]:
        cid = _need(args, "id")
        message_id = (args.get("message_id") or "").strip() or None
        ok = await store.complete(cid, message_id=message_id)
        if not ok:
            return _content_block(
                _transition_failure_msg(store, cid, "complete"),
                is_error=True,
            )
        return _content_block(f"completed {cid}")

    @tool(
        "commitment_snooze",
        "Push a commitment to a later time. Use when the obligation "
        "is still relevant but cannot be acted on now (the operator "
        "asked to wait; you're blocked on something external; the "
        "promised time has shifted). Pass EITHER ``for_days`` "
        "(relative, fractional OK — e.g. 7 for a week) OR ``until_unix`` "
        "(absolute unix-second target). Exactly one. The snooze count "
        "increments on each call — crossing the operator-tunable "
        "threshold (default 3) triggers a ``commitment_snooze_pileup`` "
        "algedonic event so chronic deferral is visible. Optional "
        "``reason`` is stored on the record for audit context.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "for_days": {"type": "number"},
                "until_unix": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["id"],
        },
    )
    @_safe("commitment_snooze")
    async def commitment_snooze(args: dict[str, Any]) -> dict[str, Any]:
        cid = _need(args, "id")
        for_days = args.get("for_days")
        until_unix = args.get("until_unix")
        # Exactly-one enforcement matches the CLI's mutex group shape
        # (cli.py:170): one absolute target OR one relative shift, not
        # both / neither.
        have_for = isinstance(for_days, (int, float))
        have_until = isinstance(until_unix, (int, float))
        if have_for == have_until:
            return _content_block(
                "commitment_snooze failed: exactly one of 'for_days' "
                "or 'until_unix' required",
                is_error=True,
            )
        if have_for:
            until = time.time() + float(for_days) * 86400
        else:
            until = float(until_unix)
        reason = (args.get("reason") or "").strip() or None
        ok = await store.snooze(cid, until_unix=until, reason=reason)
        if not ok:
            return _content_block(
                _transition_failure_msg(store, cid, "snooze"),
                is_error=True,
            )
        return _content_block(f"snoozed {cid} until unix={until:.0f}")

    @tool(
        "commitment_dismiss",
        "Drop a commitment as no longer relevant. Terminal: a dismissed "
        "commitment cannot be re-opened. Use when the obligation has "
        "lapsed (the operator cancelled it, the underlying issue was "
        "resolved by someone else, the context expired). Distinct from "
        "``commitment_complete`` — completed means you did it; dismissed "
        "means it doesn't need doing anymore. Optional ``reason`` is "
        "stored on the record for audit context.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["id"],
        },
    )
    @_safe("commitment_dismiss")
    async def commitment_dismiss(args: dict[str, Any]) -> dict[str, Any]:
        cid = _need(args, "id")
        reason = (args.get("reason") or "").strip() or None
        ok = await store.dismiss(cid, reason=reason)
        if not ok:
            return _content_block(
                _transition_failure_msg(store, cid, "dismiss"),
                is_error=True,
            )
        return _content_block(f"dismissed {cid}")

    @tool(
        "commitment_list",
        "List active commitments. Useful when the ``## Upcoming "
        "commitments`` prompt block was truncated by max_entries (the "
        "footer says ``…and N more``) or when you want to filter by "
        "status / channel. Pass ``channel_id`` to scope to one channel "
        "(unbound records also returned by default); omit for cross-"
        "channel view. ``status`` filters by lifecycle state "
        "(pending/delivered/snoozed/completed/dismissed/expired); omit "
        "for all-active (pending+delivered+snoozed) — the agent's usual "
        "concern. Returns JSON.",
        {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
    )
    @_safe("commitment_list")
    async def commitment_list(args: dict[str, Any]) -> dict[str, Any]:
        channel_id = (args.get("channel_id") or "").strip() or None
        status = (args.get("status") or "").strip() or None
        rows = store.list(channel_id=channel_id, status=status)
        if status is None:
            # Default: active-only (matches the agent's "what should I
            # be paying attention to" use case; terminal records aren't
            # in the surfacing path).
            rows = [
                r for r in rows
                if r.status in ("pending", "delivered", "snoozed")
            ]
        payload = [
            {
                "id": r.id,
                "status": r.status,
                "channel_id": r.channel_id,
                "recipient_identity": r.recipient_identity,
                "text": r.text,
                "kind": r.kind,
                "sensitivity": r.sensitivity,
                "due_window_start_unix": r.due_window_start_unix,
                "due_window_hint": r.due_window_hint,
                "snooze_count": r.snooze_count,
                "attempts": r.attempts,
            }
            for r in rows
        ]
        return _content_block(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )

    return [
        commitment_complete,
        commitment_snooze,
        commitment_dismiss,
        commitment_list,
    ]


def _transition_failure_msg(
    store: CommitmentsStore, cid: str, verb: str,
) -> str:
    """Tighten the failure message based on whether the commitment is
    missing or already terminal. Mirrors the CLI's
    ``_report_transition_failure`` helper (cli.py:267) so operator and
    agent see the same disambiguation."""
    rec = store.current_state().get(cid)
    if rec is None:
        return f"commitment_{verb} failed: {cid!r} not found"
    return (
        f"commitment_{verb} failed: {cid} is already {rec.status} "
        f"(terminal — cannot transition)"
    )


def commitment_tool_names() -> list[str]:
    return [
        "mcp__mimir__commitment_complete",
        "mcp__mimir__commitment_snooze",
        "mcp__mimir__commitment_dismiss",
        "mcp__mimir__commitment_list",
    ]
