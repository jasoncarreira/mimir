"""Observation-tier semantics: trend computation + supersession detection.

Trend labels (`stable | strengthening | weakening | stale`) answer "is
this belief still relevant?" — surfaced in retrieval results so the
agent can weight beliefs by their evidence trajectory. Stale labels
flag observations that haven't been validated by recent activity;
strengthening labels signal beliefs being reinforced.

Trend is computed from the observation atom's OWN access event history,
not the evidence raws'. Rationale: the agent's retrievals of the
observation are the direct signal of "this belief is being used." The
raws' activity is upstream noise — interesting for diagnosis, not the
trend's primary input.

Supersession detection runs at reflect time: when a new observation's
evidence set is a strict superset of an existing observation's, the
old observation gets a `supersedes` relation pointing at the new one,
and the recall ranker applies the supersession penalty at score time.

This module is pure-function over the events log + relations table —
no LLM calls, no embeddings. Cheap to recompute on demand or as part
of a periodic refresh.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


# Trend classification windows + thresholds. Tunable; calibrate
# against operator-perceived signal-vs-noise during bench iteration.
STALE_THRESHOLD_DAYS = 30
RECENT_WINDOW_DAYS = 7
HISTORICAL_WINDOW_DAYS = 30  # window prior to the recent window
STRENGTHENING_RATIO = 1.5  # recent_rate / historical_rate above this → strengthening
WEAKENING_RATIO = 0.5  # below this → weakening


@dataclass(frozen=True)
class TrendResult:
    atom_id: str
    trend: str  # 'stable' | 'strengthening' | 'weakening' | 'stale'
    last_access_days_ago: float
    recent_count: int
    historical_count: int
    ratio: float | None  # None when historical_count == 0
    rationale: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def classify_trend(
    *,
    observation_id: str,
    access_timestamps: Iterable[str],
    now: datetime | None = None,
) -> TrendResult:
    """Classify trend from a list of access event timestamps on the
    observation. ``access_timestamps`` is the observation's own
    access_events.ts values, in any order (function sorts internally).

    Decision tree:
    1. No events ever → stale (shouldn't happen for an observation
       since reflect fires a 'store' event at creation, but defensive).
    2. Last event > STALE_THRESHOLD_DAYS ago → stale.
    3. recent_rate / historical_rate above STRENGTHENING_RATIO →
       strengthening.
    4. Same ratio below WEAKENING_RATIO → weakening.
    5. Otherwise → stable.
    """
    if now is None:
        now = _utc_now()
    timestamps = sorted(_parse_iso(t) for t in access_timestamps)
    if not timestamps:
        return TrendResult(
            atom_id=observation_id,
            trend="stale",
            last_access_days_ago=float("inf"),
            recent_count=0,
            historical_count=0,
            ratio=None,
            rationale="no access events recorded",
        )

    last_access = timestamps[-1]
    days_since = (now - last_access).total_seconds() / 86400.0
    if days_since > STALE_THRESHOLD_DAYS:
        return TrendResult(
            atom_id=observation_id,
            trend="stale",
            last_access_days_ago=days_since,
            recent_count=0,
            historical_count=len(timestamps),
            ratio=None,
            rationale=(
                f"last access {days_since:.1f}d ago, "
                f"exceeds stale threshold {STALE_THRESHOLD_DAYS}d"
            ),
        )

    # Bucket events into recent + historical windows. Events older
    # than (recent + historical) are not used — they're too old to
    # signal momentum.
    recent_boundary = now - timedelta(days=RECENT_WINDOW_DAYS)
    historical_boundary = now - timedelta(
        days=RECENT_WINDOW_DAYS + HISTORICAL_WINDOW_DAYS,
    )
    recent = [t for t in timestamps if t >= recent_boundary]
    historical = [t for t in timestamps if historical_boundary <= t < recent_boundary]

    # Normalize counts to "events per day" so the windows can have
    # different lengths without skewing the comparison.
    recent_rate = len(recent) / max(RECENT_WINDOW_DAYS, 1)
    historical_rate = len(historical) / max(HISTORICAL_WINDOW_DAYS, 1)

    if historical_rate == 0 and recent_rate > 0:
        # First-time accesses in the recent window with no history.
        # Treat as strengthening (the observation just started getting
        # picked up) rather than dividing by zero.
        return TrendResult(
            atom_id=observation_id,
            trend="strengthening",
            last_access_days_ago=days_since,
            recent_count=len(recent),
            historical_count=0,
            ratio=None,
            rationale="recent activity, no prior history",
        )

    if historical_rate == 0:
        return TrendResult(
            atom_id=observation_id,
            trend="stable",
            last_access_days_ago=days_since,
            recent_count=0,
            historical_count=0,
            ratio=None,
            rationale="no activity in either window",
        )

    ratio = recent_rate / historical_rate
    if ratio >= STRENGTHENING_RATIO:
        trend = "strengthening"
        rationale = (
            f"recent rate {recent_rate:.3f}/d vs historical "
            f"{historical_rate:.3f}/d (ratio {ratio:.2f})"
        )
    elif ratio <= WEAKENING_RATIO:
        trend = "weakening"
        rationale = (
            f"recent rate {recent_rate:.3f}/d vs historical "
            f"{historical_rate:.3f}/d (ratio {ratio:.2f})"
        )
    else:
        trend = "stable"
        rationale = (
            f"recent rate {recent_rate:.3f}/d vs historical "
            f"{historical_rate:.3f}/d (ratio {ratio:.2f})"
        )
    return TrendResult(
        atom_id=observation_id,
        trend=trend,
        last_access_days_ago=days_since,
        recent_count=len(recent),
        historical_count=len(historical),
        ratio=ratio,
        rationale=rationale,
    )


def refresh_trend(
    conn: sqlite3.Connection,
    observation_id: str,
    *,
    now: datetime | None = None,
    manage_transaction: bool = True,
) -> TrendResult:
    """Compute the trend and persist to observations_metadata.

    Idempotent — running on the same observation twice produces the
    same result (modulo time progression). Used by reflect() on
    newly-created observations and by a periodic refresh job.

    chainlink #416: this function owns the TREND fields only
    (``trend``, ``last_evidence_at``, plus the ``consolidated_at``
    backfill on a missing row). ``evidence_count`` means the number of
    evidence atoms backing the observation (``evidenced_by``
    relations) — written by consolidate at insert time, rebuilt by
    dedup's end-of-pass sweep and by forget(). The pre-fix UPDATE
    branch here overwrote it with ``len(access events)``, so the
    consolidate→refresh_trend sequence destroyed the real count
    (every fresh observation collapsed to 1 — its own 'store' event)
    and ``pick_canonical``'s ev_count tiebreaker ranked on the
    corrupted value.
    """
    rows = conn.execute(
        "SELECT ts FROM access_events WHERE atom_id = ? ORDER BY ts",
        (observation_id,),
    ).fetchall()
    timestamps = [r[0] for r in rows]
    result = classify_trend(
        observation_id=observation_id,
        access_timestamps=timestamps,
        now=now,
    )
    # Upsert observations_metadata. Callers performing a larger atomic
    # mutation may own the surrounding transaction.
    existing = conn.execute(
        "SELECT atom_id FROM observations_metadata WHERE atom_id = ?",
        (observation_id,),
    ).fetchone()
    last_evidence_at = timestamps[-1] if timestamps else None
    try:
        if manage_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if existing is None:
            # Missing row (periodic refresh on an observation whose
            # metadata was never written): seed evidence_count from the
            # live evidenced_by relations — the relation-count semantics
            # (#416), never the access-event count.
            live_evidence = conn.execute(
                "SELECT COUNT(*) FROM atom_relations ar "
                "JOIN atoms t ON t.id = ar.target_id "
                "WHERE ar.source_id = ? "
                "  AND ar.relation_type = 'evidenced_by' "
                "  AND t.tombstoned = 0",
                (observation_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO observations_metadata "
                "(atom_id, evidence_count, trend, last_evidence_at, "
                "consolidated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    observation_id,
                    live_evidence,
                    result.trend,
                    last_evidence_at,
                    # consolidated_at filled by reflect when the observation
                    # is first created; if missing on a periodic refresh,
                    # use the earliest event time as a proxy.
                    timestamps[0] if timestamps else (now or _utc_now()).isoformat(),
                ),
            )
        else:
            # evidence_count deliberately untouched (#416) — consolidate
            # inserted the real relation count and this refresh must not
            # clobber it with an event count.
            conn.execute(
                "UPDATE observations_metadata SET trend = ?, "
                "last_evidence_at = ? WHERE atom_id = ?",
                (result.trend, last_evidence_at, observation_id),
            )
        if manage_transaction:
            conn.commit()
    except Exception:
        if manage_transaction:
            conn.rollback()
        raise
    return result


def find_equal_evidence_obs(
    conn: sqlite3.Connection,
    evidence_set: set[str],
) -> str | None:
    """Return an observation_id whose evidence set is exactly
    ``evidence_set``, or None.

    Used by reflect() and consolidate() to detect "we already drew
    this conclusion from exactly this evidence" — in that case the
    caller skips re-synthesis and logs a consolidation event on the
    existing observation instead. Saves the LLM synthesis cost and
    avoids storing semantically-duplicate observations.

    Strictly equal sets only. Superset/subset relationships are
    handled by find_superseded_observations + the regular create-and-
    link path (per SCORING.md: superset clusters DO create new
    observations and supersede the old).
    """
    if not evidence_set:
        return None
    placeholders = ",".join(["?"] * len(evidence_set))
    candidates = conn.execute(
        f"SELECT source_id FROM atom_relations "
        f"WHERE target_id IN ({placeholders}) "
        f"AND relation_type = 'evidenced_by' "
        f"GROUP BY source_id HAVING COUNT(DISTINCT target_id) = ?",
        list(evidence_set) + [len(evidence_set)],
    ).fetchall()
    for (obs_id,) in candidates:
        # Confirm the observation isn't tombstoned and has EXACTLY
        # this evidence set (no extras).
        is_live = conn.execute(
            "SELECT 1 FROM atoms WHERE id = ? AND tombstoned = 0",
            (obs_id,),
        ).fetchone()
        if not is_live:
            continue
        obs_evidence = {
            r[0]
            for r in conn.execute(
                "SELECT target_id FROM atom_relations "
                "WHERE source_id = ? AND relation_type = 'evidenced_by'",
                (obs_id,),
            )
        }
        if obs_evidence == evidence_set:
            return obs_id
    return None


def find_superseded_observations(
    conn: sqlite3.Connection,
    new_observation_id: str,
    new_evidence_set: set[str],
) -> list[str]:
    """Return prior observation ids whose evidence set is a STRICT
    subset of ``new_evidence_set``. Caller is reflect(), which adds
    ``supersedes`` relations from new → each returned id.

    Strict subset: every member of the old observation's evidence is
    in the new evidence, AND the new evidence has at least one
    additional member. Equal evidence sets do NOT supersede (the new
    observation is redundant; caller should suppress it).

    Tombstoned observations are excluded — once forgotten, they
    don't participate in supersession.
    """
    # Pull all current observation → their evidence sets in one query.
    rows = conn.execute(
        """
        SELECT a.id, ar.target_id
        FROM atoms a
        JOIN atom_relations ar
          ON ar.source_id = a.id
        WHERE a.memory_type = 'observation'
          AND a.tombstoned = 0
          AND a.id != ?
          AND ar.relation_type = 'evidenced_by'
    """,
        (new_observation_id,),
    ).fetchall()
    by_obs: dict[str, set[str]] = {}
    for obs_id, raw_id in rows:
        by_obs.setdefault(obs_id, set()).add(raw_id)
    superseded = []
    for obs_id, old_evidence in by_obs.items():
        if old_evidence and old_evidence < new_evidence_set:
            # Strict subset.
            superseded.append(obs_id)
    return superseded
