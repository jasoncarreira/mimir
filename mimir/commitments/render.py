"""Prompt-block rendering for active commitments (Phase 3).

The agent needs structured visibility into its own pending obligations
between turns. The Phase 2b poller emits algedonic events for
time-anchored commitments coming due, but most extracted commitments
carry only a natural-language hint (``"Thursday"``, ``"once #108
merges"``) — without unix-second anchors the poller can't fire. This
module renders the full active set as a prompt block so those
hint-only commitments are still visible per turn.

Pure functions; no IO. The agent (``Agent._assemble_commitments_block``)
filters the store's records by channel + active-status and passes
them here. Synthetic-channel skipping happens in the agent, not here —
this module just renders whatever it's given.
"""

from __future__ import annotations

import math
import time

from .models import CommitmentRecord, CommitmentSensitivity, CommitmentStatus


# Active (non-terminal) statuses the prompt block surfaces. Terminal
# records (completed/dismissed/expired) are excluded by the caller's
# filter — they don't need agent attention.
_ACTIVE_STATUSES = frozenset({
    CommitmentStatus.PENDING.value,
    CommitmentStatus.DELIVERED.value,
    CommitmentStatus.SNOOZED.value,
})


def _due_phrase(rec: CommitmentRecord, *, now_unix: float) -> str:
    """Human-friendly "when" string for the rendered line.

    Priority:
    1. ``due_window_start_unix`` set → ``"in 3d"`` / ``"overdue 2d"`` /
       ``"today"`` against ``now_unix``.
    2. ``due_window_hint`` set (natural-language) → render verbatim
       in parens: ``"(Thursday)"``.
    3. Neither → ``"(no anchor)"`` so the agent knows it lacks a time.
    """
    start = rec.due_window_start_unix
    if start is not None:
        delta_secs = start - now_unix
        delta_days = delta_secs / 86400
        if delta_days < -1:
            return f"overdue {int(-delta_days)}d"
        if delta_days < 0:
            return "overdue <1d"
        if delta_days < 1:
            return "today"
        return f"in {int(math.ceil(delta_days))}d"
    if rec.due_window_hint:
        # Strip extreme whitespace + cap at 40 chars for safety
        hint = " ".join(rec.due_window_hint.split())
        if len(hint) > 40:
            hint = hint[:39] + "…"
        return f"({hint})"
    return "(no anchor)"


def _sort_key(rec: CommitmentRecord) -> tuple[float, float]:
    """Order: explicit time anchor first (sorted by start asc),
    then hint-only / no-anchor by ``created_at_unix`` asc.

    ``due_window_start_unix`` sentinel: ``inf`` so records without one
    sort after anchored ones."""
    start = rec.due_window_start_unix
    primary = start if start is not None else float("inf")
    return (primary, rec.created_at_unix or 0.0)


def render_commitments_block(
    records: list[CommitmentRecord],
    *,
    now_unix: float | None = None,
    max_entries: int = 8,
) -> str | None:
    """Render the ``## Upcoming commitments`` block body.

    ``records`` should already be filtered to the relevant channel
    (channel-bound + unbound; terminal records excluded) by the caller.
    Returns ``None`` when the filtered list is empty so the prompt
    builder can skip the section entirely.

    Format per line: ``- [c-abc123def0] (in 3d) text — recipient (kind)``
    with the kind suffix dropped for the common ``open_loop`` case and
    ``CARE`` / ``PERSONAL`` sensitivity surfaced as a bracketed prefix.

    ``max_entries`` caps the bullet count; overflow renders an
    ``…and N more`` footer so the section never grows unboundedly.
    """
    if now_unix is None:
        now_unix = time.time()

    active = [r for r in records if r.status in _ACTIVE_STATUSES]
    if not active:
        return None

    active.sort(key=_sort_key)
    overflow = max(0, len(active) - max_entries)
    visible = active[:max_entries]

    lines: list[str] = []
    for rec in visible:
        due = _due_phrase(rec, now_unix=now_unix)
        text = (rec.text or "").strip()
        # Sensitivity prefix only when non-routine — keeps the line
        # terse for the bulk case while flagging CARE/PERSONAL.
        sens_prefix = ""
        if rec.sensitivity == CommitmentSensitivity.CARE.value:
            sens_prefix = "[care] "
        elif rec.sensitivity == CommitmentSensitivity.PERSONAL.value:
            sens_prefix = "[personal] "
        # Recipient suffix when set — "remind Alice about X" gives the
        # agent who-to-mention without needing to look up the record.
        recipient_suffix = (
            f" — for @{rec.recipient_identity}"
            if rec.recipient_identity
            else ""
        )
        # Channel scope suffix only when the commitment is unbound,
        # to disambiguate from same-channel ones in the same list.
        scope_suffix = " (unbound)" if rec.channel_id is None else ""
        # Snooze count suffix when ≥2 — surface "I keep punting this"
        # before the pileup alarm fires (threshold default 3).
        snooze_suffix = (
            f" — snoozed×{rec.snooze_count}"
            if rec.snooze_count >= 2
            else ""
        )
        lines.append(
            f"- [{rec.id}] {sens_prefix}{due} {text}"
            f"{recipient_suffix}{scope_suffix}{snooze_suffix}"
        )

    if overflow:
        lines.append(f"…and {overflow} more")

    header = (
        "Active commitments visible to you. Resolve via the commitments "
        "skill's MCP tools (commitment_complete / commitment_snooze / "
        "commitment_dismiss) when you act on or drop one."
    )
    return header + "\n" + "\n".join(lines)
