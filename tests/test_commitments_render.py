"""Phase 3 — ``## Upcoming commitments`` prompt block rendering.

Pure-function tests over ``render_commitments_block``: sort order,
due-phrase shapes, sensitivity / recipient / scope / snooze
suffixing, terminal-record exclusion, overflow footer, empty-list
``None`` return.
"""

from __future__ import annotations

import time

from mimir.commitments import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentSensitivity,
    CommitmentStatus,
)
from mimir.commitments.render import render_commitments_block


def _rec(**kw) -> CommitmentRecord:
    """Test-only factory — fills the required ``id`` / ``channel_id`` /
    ``text`` and lets the caller override everything else."""
    defaults = {
        "id": kw.pop("id", "c-test000001"),
        "channel_id": kw.pop("channel_id", "ch-1"),
        "text": kw.pop("text", "do the thing"),
        "created_at_unix": kw.pop("created_at_unix", 1_700_000_000.0),
    }
    return CommitmentRecord(**defaults, **kw)


def test_empty_returns_none():
    assert render_commitments_block([]) is None


def test_all_terminal_returns_none():
    """Terminal records are excluded — block disappears entirely."""
    recs = [
        _rec(id="c-a", status=CommitmentStatus.COMPLETED.value),
        _rec(id="c-b", status=CommitmentStatus.DISMISSED.value),
        _rec(id="c-c", status=CommitmentStatus.EXPIRED.value),
    ]
    assert render_commitments_block(recs) is None


def test_active_record_renders():
    now = 1_715_000_000.0
    rec = _rec(
        id="c-abc123def0",
        text="Review PR #142",
        due_window_start_unix=now + 3 * 86400,
        recipient_identity="alice",
    )
    out = render_commitments_block([rec], now_unix=now)
    assert out is not None
    assert "c-abc123def0" in out
    assert "Review PR #142" in out
    assert "in 3d" in out
    assert "for @alice" in out


def test_overdue_phrase():
    now = 1_715_000_000.0
    rec = _rec(
        id="c-overdue",
        due_window_start_unix=now - 2 * 86400,
    )
    out = render_commitments_block([rec], now_unix=now)
    assert "overdue 2d" in out


def test_today_phrase():
    now = 1_715_000_000.0
    rec = _rec(
        id="c-today",
        due_window_start_unix=now + 3600,  # 1h from now
    )
    out = render_commitments_block([rec], now_unix=now)
    assert "today" in out


def test_hint_falls_back_when_no_unix_anchor():
    rec = _rec(
        id="c-hint",
        due_window_hint="next sprint",
    )
    out = render_commitments_block([rec], now_unix=1_715_000_000.0)
    assert "(next sprint)" in out


def test_no_anchor_phrase():
    rec = _rec(id="c-bare")
    out = render_commitments_block([rec], now_unix=1_715_000_000.0)
    assert "(no anchor)" in out


def test_unix_anchor_beats_hint():
    """When both are set, the numeric due phrase wins — it's more
    precise than the natural-language hint."""
    now = 1_715_000_000.0
    rec = _rec(
        id="c-both",
        due_window_start_unix=now + 86400,
        due_window_hint="tomorrow ish",
    )
    out = render_commitments_block([rec], now_unix=now)
    assert "in 1d" in out
    assert "tomorrow ish" not in out


def test_sort_anchored_before_hint_only():
    """Records with explicit unix anchors sort ahead of hint-only /
    no-anchor records, so the "I need to act soon" view comes first."""
    now = 1_715_000_000.0
    hint = _rec(
        id="c-hint",
        due_window_hint="someday",
        created_at_unix=1_000_000.0,  # ancient
    )
    anchored = _rec(
        id="c-soon",
        due_window_start_unix=now + 7 * 86400,
        created_at_unix=now,
    )
    out = render_commitments_block([hint, anchored], now_unix=now)
    lines = out.splitlines()
    # Header at lines[0], then bullets — anchored should come first
    soon_idx = next(i for i, l in enumerate(lines) if "c-soon" in l)
    hint_idx = next(i for i, l in enumerate(lines) if "c-hint" in l)
    assert soon_idx < hint_idx


def test_sort_anchored_by_due_asc():
    now = 1_715_000_000.0
    a = _rec(id="c-a", due_window_start_unix=now + 10 * 86400)
    b = _rec(id="c-b", due_window_start_unix=now + 2 * 86400)
    c = _rec(id="c-c", due_window_start_unix=now + 30 * 86400)
    out = render_commitments_block([a, b, c], now_unix=now)
    ids_in_order = []
    for line in out.splitlines():
        for cid in ("c-a", "c-b", "c-c"):
            if cid in line:
                ids_in_order.append(cid)
                break
    assert ids_in_order == ["c-b", "c-a", "c-c"]


def test_care_sensitivity_prefix():
    rec = _rec(
        id="c-care",
        sensitivity=CommitmentSensitivity.CARE.value,
        text="check in after the outage",
    )
    out = render_commitments_block([rec])
    assert "[care]" in out


def test_personal_sensitivity_prefix():
    rec = _rec(
        id="c-personal",
        sensitivity=CommitmentSensitivity.PERSONAL.value,
    )
    out = render_commitments_block([rec])
    assert "[personal]" in out


def test_routine_sensitivity_no_prefix():
    rec = _rec(
        id="c-routine",
        sensitivity=CommitmentSensitivity.ROUTINE.value,
    )
    out = render_commitments_block([rec])
    assert "[routine]" not in out
    assert "[care]" not in out
    assert "[personal]" not in out


def test_unbound_scope_suffix():
    rec = _rec(id="c-unbound", channel_id=None)
    out = render_commitments_block([rec])
    assert "(unbound)" in out


def test_bound_no_scope_suffix():
    """Channel-bound commitments don't need the scope marker — the
    block is already channel-scoped via the agent's caller."""
    rec = _rec(id="c-bound", channel_id="ch-1")
    out = render_commitments_block([rec])
    assert "(unbound)" not in out


def test_snooze_count_suffix_at_two_or_more():
    rec = _rec(id="c-snoozy", snooze_count=2)
    out = render_commitments_block([rec])
    assert "snoozed×2" in out


def test_snooze_count_hidden_below_two():
    """1 snooze is normal; only ≥2 surfaces the pileup warning."""
    rec = _rec(id="c-once", snooze_count=1)
    out = render_commitments_block([rec])
    assert "snoozed" not in out


def test_overflow_footer():
    """``max_entries`` caps the bullet list; extras go in a footer."""
    recs = [_rec(id=f"c-{i:010x}") for i in range(12)]
    out = render_commitments_block(recs, max_entries=5)
    visible_count = sum(1 for line in out.splitlines() if line.startswith("- "))
    assert visible_count == 5
    assert "…and 7 more" in out


def test_pending_delivered_snoozed_all_visible():
    """All three active statuses surface; only terminal ones hide."""
    recs = [
        _rec(id="c-p", status=CommitmentStatus.PENDING.value),
        _rec(id="c-d", status=CommitmentStatus.DELIVERED.value),
        _rec(id="c-s", status=CommitmentStatus.SNOOZED.value),
        _rec(id="c-c", status=CommitmentStatus.COMPLETED.value),
    ]
    out = render_commitments_block(recs)
    assert "c-p" in out
    assert "c-d" in out
    assert "c-s" in out
    assert "c-c" not in out


def test_header_present():
    """The block opens with a one-sentence orientation line so the
    agent knows what the section is and which tools resolve it."""
    rec = _rec(id="c-x")
    out = render_commitments_block([rec])
    first_line = out.splitlines()[0]
    assert "Active commitments" in first_line
    assert "commitment_complete" in first_line
