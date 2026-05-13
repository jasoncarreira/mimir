"""ACT-R base-level activation with Petrov (2006) Optimized Learning.

The single load-bearing module of the rewrite. Replaces saga's
``stability`` scalar + ``retrievability`` column + ``decay_cycle`` state
machine with on-demand activation from an access-events log.

Theoretical basis:
    B_i = ln(Σ_j (now - t_j)^(-d))                            [exact ACT-R]

where t_j is the time since the j-th access of the atom and d is the
decay parameter (default 0.5; the value Anderson uses in most ACT-R
models).

Computing the exact sum is O(n_accesses) per retrieval. With ~600 access
events/atom over a year, that's prohibitive. Petrov 2006's "Optimized
Learning" approximation gets to O(K) where K is a small constant (~10):

    B_i ≈ ln(
        Σ_{j ∈ recent_K} weight_j · (now - t_j)^(-d)
      + decayed_aggregate(n_old, weight_sum_old, oldest_ts_old, now, d)
    )

The aggregate approximation treats old events as uniformly distributed
between ``oldest_ts_old`` and the boundary of the recent window. The
integral of (now - τ)^(-d) over that interval, scaled by mean event
weight, captures most of the tail contribution without per-event scan.

References
----------
- [Anderson & Schooler 1991]: power-law fit to environmental recurrence
- [Petrov 2006]: Computationally Efficient Approximation of the
  Base-Level Learning Equation in ACT-R
- [ACT-R 7 Reference Manual, §B]: the :ol parameter
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Iterable

# ─── Tunables ────────────────────────────────────────────────────────

#: ACT-R decay parameter. Anderson's typical value across many models.
#: Higher = faster forgetting. Range [0.0, 1.0).
DECAY_D = 0.5

#: How many recent events to track exactly per atom. Trades read-path
#: cost (O(K) per activation) against approximation quality. K=10 is
#: Petrov's sweet spot — captures the transient post-access boost
#: cleanly without making per-retrieval cost noticeable.
RECENT_K = 10

#: Floor on the (now - t_j) term to avoid the singularity at t_j = now.
#: One second is the smallest meaningful resolution; using zero would
#: blow the formula up the instant an access lands.
EPSILON_SECONDS = 1.0

#: Activation thresholds for retrieval gating, per stream. Atoms
#: whose computed activation B_i is below their stream's threshold
#: are not returned. Roughly maps to ACT-R's :rt parameter, but
#: indexed by stream because the three streams have different
#: empirical activation distributions:
#:
#: - episodic atoms (events / "what happened") get accessed rarely
#:   per-atom but accumulate quickly, so their typical activation
#:   sits lower than semantic — accept a lower threshold to keep
#:   "yesterday's meeting" surfaceable
#: - semantic atoms (facts / beliefs) get re-accessed steadily and
#:   sit at moderate activation — middle threshold
#: - procedural atoms (how-to) are sticky and accessed when needed
#:   — demand higher activation to surface so we don't churn
#:   noise from rarely-relevant routines
#:
#: These are starting values; empirical calibration against
#: LongMemEval-S should refine them per provider. Live override via
#: ``recall(threshold=...)`` or ``recall(thresholds={'episodic': ...})``.
DEFAULT_STREAM_THRESHOLDS = {
    "semantic": -1.5,
    "episodic": -2.5,
    "procedural": -1.0,
}

#: Fallback when an atom's stream isn't in the dict (defensive).
GLOBAL_FALLBACK_THRESHOLD = -1.5

#: Source-type weights for access events. Feedback is a stronger
#: signal than passive retrieval ("the agent explicitly endorsed
#: this"); consolidation is weaker ("this atom appeared as evidence
#: but wasn't directly used"). Tune empirically.
SOURCE_WEIGHTS = {
    "retrieval": 1.0,
    "feedback_positive": 2.0,  # MUST match the source string written by
                               # `feedback()` in __init__.py — keyed
                               # differently here silently degrades the
                               # endorsement signal to 1.0.
    "feedback_negative": 0.0,  # Zero-weight flag: the event records that
                               # the agent disowned the atom but doesn't
                               # decay activation. Used as a forget-review
                               # signal (forget_by_criteria can query for
                               # atoms with a recent negative event).
    "store": 1.0,           # the create event counts as one access
    "consolidation": 0.5,
    "pinned_init": 5.0,     # pinned atoms get a heavy initial weight
}


# ─── Time helpers ────────────────────────────────────────────────────


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_between(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds()


# ─── Petrov OL ───────────────────────────────────────────────────────


def compute_activation(
    *,
    recent_ts: Iterable[str],
    recent_weights: Iterable[float],
    old_count: int,
    old_weight_sum: float,
    old_oldest_ts: str | None,
    now: datetime | None = None,
    decay: float = DECAY_D,
    epsilon: float = EPSILON_SECONDS,
) -> float:
    """Petrov OL activation.

    ``recent_ts`` and ``recent_weights`` are parallel arrays of the
    last K access events for this atom (newest first or oldest first
    — sum is commutative). ``old_*`` describes the aggregate of access
    events that have aged out of the recent window.

    Returns the activation B_i = ln(Σ ...). Caller compares against
    threshold to gate retrieval.

    Returns -inf when the atom has zero access events (never retrieved,
    never stored — shouldn't happen for a stored atom since store
    creates one access event).
    """
    if now is None:
        now = _now_utc()

    total = 0.0

    # Exact contribution from the recent K events.
    for ts_str, weight in zip(recent_ts, recent_weights):
        t = _parse_iso(ts_str)
        age_s = max(_seconds_between(now, t), epsilon)
        total += weight * (age_s ** (-decay))

    # Aggregate approximation for displaced events.
    if old_count > 0 and old_oldest_ts is not None and old_weight_sum > 0:
        oldest = _parse_iso(old_oldest_ts)
        # Newest displaced event is just past the recent window; use the
        # oldest recent timestamp as the upper bound on the integral,
        # falling back to now if there are no recent events (shouldn't
        # happen — store inserts one).
        if recent_ts:
            # The first item should be oldest if newest-first OR
            # newest if oldest-first; we sort defensively.
            recent_dt = sorted(_parse_iso(t) for t in recent_ts)
            upper = recent_dt[0]  # oldest "recent" = boundary
        else:
            upper = now
        # Treat the displaced events as uniformly distributed in
        # [oldest, upper]. Mean per-event weight:
        mean_weight = old_weight_sum / old_count
        # Integral of (now - τ)^(-d) dτ from τ=oldest to τ=upper.
        # Closed form: -(now - τ)^(1-d) / (1-d) | evaluated at endpoints.
        # = ((now - oldest)^(1-d) - (now - upper)^(1-d)) / (1-d)
        upper_age = max(_seconds_between(now, upper), epsilon)
        oldest_age = max(_seconds_between(now, oldest), epsilon)
        if abs(1.0 - decay) < 1e-9:
            # d=1 case: integral becomes ln. Edge case; we don't ship
            # with d=1 by default but guard anyway.
            integral_per_unit_dτ = math.log(oldest_age / upper_age)
        else:
            integral_per_unit_dτ = (
                (oldest_age ** (1.0 - decay)) - (upper_age ** (1.0 - decay))
            ) / (1.0 - decay)
        # Divide by window width to get "per second" contribution,
        # then multiply by mean weight × count for total.
        window_seconds = max(oldest_age - upper_age, epsilon)
        avg_contribution_per_event = integral_per_unit_dτ / window_seconds
        total += old_count * mean_weight * avg_contribution_per_event

    if total <= 0.0:
        return float("-inf")
    return math.log(total)


# ─── Summary maintenance (denormalization for read speed) ────────────


def update_summary_on_access(
    *,
    current_summary: dict | None,
    new_ts: str,
    new_weight: float,
    recent_k: int = RECENT_K,
) -> dict:
    """Apply one new access event to an atom's access summary.

    Returns the updated summary dict suitable for storing back into
    ``atom_access_summary``. Pure function — caller persists.

    Maintains the invariant that ``recent_ts_json`` holds the K most
    recent timestamps (newest first), with anything beyond K folded
    into ``old_count`` / ``old_weight_sum`` / ``old_oldest_ts``.
    """
    if current_summary is None:
        current_summary = {
            "recent_ts_json": "[]",
            "recent_weights_json": "[]",
            "old_count": 0,
            "old_weight_sum": 0.0,
            "old_oldest_ts": None,
        }

    recent_ts = json.loads(current_summary.get("recent_ts_json") or "[]")
    recent_weights = json.loads(current_summary.get("recent_weights_json") or "[]")

    # Insert new event at the head (newest first).
    recent_ts.insert(0, new_ts)
    recent_weights.insert(0, new_weight)

    # Spill the oldest if we exceeded K.
    old_count = int(current_summary.get("old_count") or 0)
    old_weight_sum = float(current_summary.get("old_weight_sum") or 0.0)
    old_oldest_ts = current_summary.get("old_oldest_ts")

    while len(recent_ts) > recent_k:
        spilled_ts = recent_ts.pop()
        spilled_weight = recent_weights.pop()
        old_count += 1
        old_weight_sum += spilled_weight
        if old_oldest_ts is None or spilled_ts < old_oldest_ts:
            old_oldest_ts = spilled_ts

    return {
        "recent_ts_json": json.dumps(recent_ts),
        "recent_weights_json": json.dumps(recent_weights),
        "old_count": old_count,
        "old_weight_sum": old_weight_sum,
        "old_oldest_ts": old_oldest_ts,
        "last_updated_ts": new_ts,
    }


def rebuild_summary_from_events(
    events: list[tuple[str, float]],
    recent_k: int = RECENT_K,
) -> dict:
    """Build a summary from a list of (ts, weight) events. Used by the
    migration importer (saga.db → mimir.memory.db) and by repair tools
    that detect a corrupt summary.

    ``events`` should be ordered oldest-first (insertion order).
    """
    # Sort defensively in case caller didn't.
    events = sorted(events, key=lambda e: e[0])
    summary = None
    for ts, weight in events:
        summary = update_summary_on_access(
            current_summary=summary, new_ts=ts, new_weight=weight,
            recent_k=recent_k,
        )
    return summary or {
        "recent_ts_json": "[]",
        "recent_weights_json": "[]",
        "old_count": 0,
        "old_weight_sum": 0.0,
        "old_oldest_ts": None,
    }


# ─── Convenience for the test harness ────────────────────────────────


def activation_from_events(
    events: list[tuple[str, float]],
    *,
    now: datetime | None = None,
    decay: float = DECAY_D,
    recent_k: int = RECENT_K,
) -> float:
    """End-to-end: take a list of (ts, weight) events, build the
    summary, compute activation. Used by tests to verify the OL
    approximation against the exact sum (which the test re-computes
    directly from the events without summarization).
    """
    summary = rebuild_summary_from_events(events, recent_k=recent_k)
    return compute_activation(
        recent_ts=json.loads(summary["recent_ts_json"]),
        recent_weights=json.loads(summary["recent_weights_json"]),
        old_count=summary["old_count"],
        old_weight_sum=summary["old_weight_sum"],
        old_oldest_ts=summary["old_oldest_ts"],
        now=now, decay=decay,
    )


def activation_exact(
    events: list[tuple[str, float]],
    *,
    now: datetime | None = None,
    decay: float = DECAY_D,
    epsilon: float = EPSILON_SECONDS,
) -> float:
    """Ground-truth ACT-R activation: full sum over every event, no
    approximation. Slow but exact — used by tests to validate that
    the Petrov OL approximation tracks within tolerance.
    """
    if not events:
        return float("-inf")
    if now is None:
        now = _now_utc()
    total = 0.0
    for ts_str, weight in events:
        t = _parse_iso(ts_str)
        age_s = max(_seconds_between(now, t), epsilon)
        total += weight * (age_s ** (-decay))
    if total <= 0.0:
        return float("-inf")
    return math.log(total)
