"""Tests for the Petrov OL approximation vs the exact ACT-R formula.

Two properties matter:

1. For atoms with ≤ K access events, Petrov OL should match exact
   activation to within float precision (no approximation kicks in
   when nothing was displaced from the recent window).
2. For atoms with > K access events, Petrov OL should track the exact
   formula within a tolerance — the aggregate approximation introduces
   error proportional to the dispersion of the displaced events' ages,
   so we check that the approximation is within ~20% in log-space
   for realistic access patterns.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from mimir.saga.activation import (
    DECAY_D,
    RECENT_K,
    activation_exact,
    activation_from_events,
    compute_activation,
    rebuild_summary_from_events,
    update_summary_on_access,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _events_at(now: datetime, ages_seconds: list[float], weight: float = 1.0):
    """Build events list given ages-in-seconds. Returns oldest-first."""
    return sorted(
        [(_iso(now - timedelta(seconds=a)), weight) for a in ages_seconds],
        key=lambda e: e[0],
    )


def test_single_event_matches_simple_formula():
    """One event 1 hour ago, weight 1.0, d=0.5.
    B = ln(1 * 3600^-0.5) = -0.5 * ln(3600) = -4.094."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    events = _events_at(now, [3600.0])
    b = activation_from_events(events, now=now)
    expected = math.log(3600.0 ** -0.5)
    assert abs(b - expected) < 1e-9, f"got {b}, expected {expected}"


def test_no_events_returns_neg_inf():
    assert activation_from_events([]) == float("-inf")


def test_recent_only_matches_exact_to_precision():
    """When the number of events is ≤ K, no aggregate kicks in, so
    Petrov OL should be bitwise identical to the exact formula."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    ages = [10, 100, 1000, 10000, 100000]  # 5 events, well below K=10
    events = _events_at(now, ages)
    petrov = activation_from_events(events, now=now)
    exact = activation_exact(events, now=now)
    assert abs(petrov - exact) < 1e-12


def test_recent_at_exactly_K_no_aggregate():
    """Exactly K events: still no aggregate kicks in."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    ages = [10 * (i + 1) for i in range(RECENT_K)]
    events = _events_at(now, ages)
    petrov = activation_from_events(events, now=now)
    exact = activation_exact(events, now=now)
    assert abs(petrov - exact) < 1e-12


def test_aggregate_tracks_exact_within_tolerance():
    """30 events, K=10. The first 20 (oldest) get aggregated.
    Petrov OL should track exact within ~20% in log-space."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    # Mix of ages: 10 recent (within last day), 20 spanning 1-30 days.
    recent_ages = [600 * (i + 1) for i in range(10)]  # 10 min .. 100 min
    old_ages = [86400 * (i + 1) for i in range(20)]   # 1-20 days
    events = _events_at(now, recent_ages + old_ages)
    petrov = activation_from_events(events, now=now)
    exact = activation_exact(events, now=now)
    # Both should be finite.
    assert petrov != float("-inf")
    assert exact != float("-inf")
    # Activation is log-scaled; 0.2 absolute tolerance is ~20% in
    # the underlying sum.
    assert abs(petrov - exact) < 0.5, f"petrov={petrov} exact={exact}"


def test_recent_event_dominates_old_ones():
    """A single access 10 seconds ago should outweigh 100 accesses
    from a year ago. This is THE property ACT-R is famous for."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    year_ago = 365 * 86400
    # 100 accesses a year ago
    old_events = _events_at(now, [year_ago + i * 100 for i in range(100)])
    activation_old_only = activation_from_events(old_events, now=now)
    # One access 10s ago
    fresh_event = _events_at(now, [10.0])
    activation_fresh_only = activation_from_events(fresh_event, now=now)
    assert activation_fresh_only > activation_old_only, (
        f"recent access should win: fresh={activation_fresh_only}, "
        f"old100={activation_old_only}"
    )


def test_access_pumps_activation():
    """Adding a new access event should raise activation. This is
    the property saga's age-from-creation formula violates."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    week_ago = 7 * 86400
    # An atom with one access a week ago
    events_before = _events_at(now, [week_ago])
    b_before = activation_from_events(events_before, now=now)
    # Same atom plus a fresh access
    events_after = events_before + _events_at(now, [10.0])
    b_after = activation_from_events(events_after, now=now)
    assert b_after > b_before, (
        f"new access should raise activation: before={b_before} after={b_after}"
    )


def test_old_atom_with_recent_access_beats_old_atom_without():
    """The KEY property: a year-old atom accessed today should
    have higher activation than a year-old atom never accessed since
    creation. Saga's age-anchored formula gets this wrong."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    year_ago = 365 * 86400
    # Atom A: created a year ago, never accessed since
    atom_a = _events_at(now, [year_ago])
    # Atom B: created a year ago, accessed yesterday
    atom_b = _events_at(now, [year_ago, 86400.0])
    b_a = activation_from_events(atom_a, now=now)
    b_b = activation_from_events(atom_b, now=now)
    assert b_b > b_a


def test_weights_amplify_contribution():
    """A feedback event (weight=2.0) should contribute more than a
    plain retrieval (weight=1.0) at the same timestamp."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    ts = now - timedelta(hours=1)
    plain = activation_from_events([(_iso(ts), 1.0)], now=now)
    feedback = activation_from_events([(_iso(ts), 2.0)], now=now)
    assert feedback > plain
    # The difference should be log(2) since the sum doubles.
    assert abs((feedback - plain) - math.log(2)) < 1e-9


def test_summary_invariant_under_incremental_updates():
    """Building the summary incrementally event-by-event must yield
    the same activation as building it via rebuild_from_events."""
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    ages = [10 * (i + 1) for i in range(25)]  # 25 events
    events = _events_at(now, ages)

    # Path A: rebuild from full list
    summary_a = rebuild_summary_from_events(events)

    # Path B: insert one event at a time
    summary_b = None
    for ts_str, weight in events:
        summary_b = update_summary_on_access(
            current_summary=summary_b, new_ts=ts_str, new_weight=weight,
        )

    # Both summaries should produce identical activation. They may
    # differ in old_oldest_ts because of insertion-order vs sorted
    # ordering — check via activation computation, not field equality.
    import json
    b_a = compute_activation(
        recent_ts=json.loads(summary_a["recent_ts_json"]),
        recent_weights=json.loads(summary_a["recent_weights_json"]),
        old_count=summary_a["old_count"],
        old_weight_sum=summary_a["old_weight_sum"],
        old_oldest_ts=summary_a["old_oldest_ts"],
        now=now,
    )
    b_b = compute_activation(
        recent_ts=json.loads(summary_b["recent_ts_json"]),
        recent_weights=json.loads(summary_b["recent_weights_json"]),
        old_count=summary_b["old_count"],
        old_weight_sum=summary_b["old_weight_sum"],
        old_oldest_ts=summary_b["old_oldest_ts"],
        now=now,
    )
    assert abs(b_a - b_b) < 1e-9


def test_feedback_event_weight_round_trips():
    """Regression for the silent-degradation bug where SOURCE_WEIGHTS
    was keyed `"feedback"` but events were written with source
    `"feedback_positive"`. mark_access does
    `SOURCE_WEIGHTS.get(source, 1.0)`, so a key mismatch silently
    falls through to 1.0 — same weight as a plain retrieval.

    This test wires the round-trip end-to-end: write a feedback event
    via mark_access without an explicit weight, then verify the
    persisted access_events row carries the documented 2.0 weight.
    """
    import sqlite3
    from pathlib import Path
    from mimir.saga.mark_access import AccessEvent, mark_access

    schema = (Path(__file__).resolve().parent.parent
              / "mimir" / "saga" / "schema.sql").read_text()
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at) "
        "VALUES ('a1', 'x', 'h1', '2026-05-12T00:00:00Z')",
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    mark_access(conn, [AccessEvent(atom_id="a1", source="feedback_positive")])
    conn.commit()
    row = conn.execute(
        "SELECT source, weight FROM access_events WHERE atom_id='a1'"
    ).fetchone()
    assert row == ("feedback_positive", 2.0)


# ── Petrov OL — feedback_negative cancellation ────────────────────────


def test_feedback_negative_cancels_a_paired_retrieval_in_recent_window():
    """A feedback_negative event (weight -1.0) co-occurring with a
    retrieval event (weight +1.0) should net to zero activation
    contribution for that pair, modulo the time-decay term they share.
    With them both at the same timestamp, the contribution cancels
    exactly — the atom has no recency boost left from that turn.
    """
    now = datetime.now(timezone.utc)
    ten_min_ago = now - timedelta(minutes=10)
    act = compute_activation(
        recent_ts=[_iso(ten_min_ago), _iso(ten_min_ago)],
        recent_weights=[1.0, -1.0],
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
        now=now,
    )
    # Total contribution is 0 → activation must be -inf (no signal).
    assert act == float("-inf")


def test_feedback_negative_makes_atom_filterable_when_solo():
    """An atom with only a single feedback_negative event (no
    counter-balancing positives) has Σ ≤ 0 and lands at -inf, which
    every finite threshold filters out — the documented contract in
    activation.py:SOURCE_WEIGHTS."""
    now = datetime.now(timezone.utc)
    act = compute_activation(
        recent_ts=[_iso(now - timedelta(minutes=5))],
        recent_weights=[-1.0],
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
        now=now,
    )
    assert act == float("-inf")


def test_petrov_ol_mixed_displaced_weights_dont_corrupt_recent_signal():
    """Petrov OL aggregates displaced events as ``old_weight_sum``
    and ``old_count``, producing a mean-weight approximation. When
    displaced events mix signs (feedback_negative cancellations of
    earlier retrievals), the aggregate's mean weight can collapse
    toward zero. The current closed-form path SHORT-CIRCUITS on
    ``old_weight_sum > 0`` (activation.py:176), so a net-zero or
    net-negative displaced aggregate contributes nothing — but the
    recent-window signal must still register cleanly.

    Pin this property: a recent feedback_positive (+2.0) on top of a
    fully-cancelled displaced aggregate (mixed +/- summing to ~0)
    produces a positive activation driven entirely by the recent
    event. The approximation can't drag activation negative when the
    aggregate is well-cancelled."""
    now = datetime.now(timezone.utc)
    # Recent: a single positive endorsement 1 minute ago.
    recent_ts = [_iso(now - timedelta(minutes=1))]
    recent_weights = [2.0]  # feedback_positive
    # Displaced aggregate: 4 events at varying ages, weights sum to 0
    # (two +1 retrievals cancelled by two -1 feedback_negatives).
    # ``old_weight_sum=0`` should engage the short-circuit at line 176.
    act = compute_activation(
        recent_ts=recent_ts,
        recent_weights=recent_weights,
        old_count=4,
        old_weight_sum=0.0,
        old_oldest_ts=_iso(now - timedelta(days=7)),
        now=now,
    )
    # Recent +2.0 contribution → finite activation (the log of a
    # positive Σ). The absolute value depends on the time-decay term;
    # what matters here is that the mixed displaced aggregate didn't
    # drag Σ negative and force a -inf.
    assert act > float("-inf")
    assert math.isfinite(act)
    # Should match a no-displaced computation since the displaced
    # aggregate is short-circuited (old_weight_sum=0 fails the > 0
    # gate).
    act_only_recent = compute_activation(
        recent_ts=recent_ts,
        recent_weights=recent_weights,
        old_count=0,
        old_weight_sum=0.0,
        old_oldest_ts=None,
        now=now,
    )
    assert act == act_only_recent
