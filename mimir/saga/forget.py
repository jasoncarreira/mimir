"""forget — explicit tombstoning.

Two entry points:

- ``forget(atom_ids, reason)`` — explicit, by id. The agent or
  operator says "this atom is wrong / private / no longer relevant."
- ``forget_by_criteria(criteria, dry_run)`` — bulk by predicate.
  Periodic cleanup pass; preview by default.

Tombstoning is one-way: sets ``tombstoned=1``, records timestamp +
reason, no un-tombstone API. Tombstoned atoms are excluded from
recall/consolidate/reflect candidate pools by construction (the
``WHERE tombstoned=0`` clauses everywhere).

Side effects on observations citing forgotten raws:

- Observation's ``observations_metadata.evidence_count`` decremented
  (rebuilt from surviving relations).
- Observation's trend recomputed (may shift toward "weakening" if
  evidence loss is recent).
- The observation itself is NOT auto-tombstoned — it may still have
  enough surviving evidence to stand. If all of its evidence raws are
  forgotten, the trend will eventually classify it as ``stale`` and
  the scoring penalty kicks in.

What's NOT removed when an atom is tombstoned:

- The atom row itself (still in atoms table; ``tombstoned=1``)
- access_events for the atom (history is preserved)
- atom_relations involving the atom (historical record)
- embedding row (could be deleted to reclaim space; defer to a
  separate vacuum pass — not bug-adjacent)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .observations import refresh_trend


@dataclass
class ForgetResult:
    tombstoned_count: int = 0
    tombstoned_ids: list[str] = field(default_factory=list)
    observations_affected: list[str] = field(default_factory=list)
    dry_run: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def forget(
    conn: sqlite3.Connection,
    atom_ids: list[str],
    *,
    reason: str | None = None,
) -> ForgetResult:
    """Explicit tombstoning by id. Returns counts of atoms tombstoned +
    observations whose metadata had to be refreshed.

    Idempotent: tombstoning an already-tombstoned atom is a no-op.
    """
    if not atom_ids:
        return ForgetResult()

    now = _utc_now_iso()
    placeholders = ",".join(["?"] * len(atom_ids))

    # Identify which atoms are eligible (not already tombstoned).
    rows = conn.execute(
        f"SELECT id FROM atoms WHERE id IN ({placeholders}) AND tombstoned = 0",
        atom_ids,
    ).fetchall()
    eligible_ids = [r[0] for r in rows]
    if not eligible_ids:
        return ForgetResult()

    # Find observations that cite any of the eligible-to-forget atoms.
    # We'll refresh their metadata after tombstoning.
    affected_obs_rows = conn.execute(
        f"SELECT DISTINCT source_id FROM atom_relations "
        f"WHERE target_id IN ({','.join(['?'] * len(eligible_ids))}) "
        f"AND relation_type = 'evidenced_by'",
        eligible_ids,
    ).fetchall()
    affected_obs_ids = [r[0] for r in affected_obs_rows]

    # Tombstone the atoms in one transaction.
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "UPDATE atoms SET tombstoned = 1, tombstoned_at = ?, "
            "tombstoned_reason = ? WHERE id = ? AND tombstoned = 0",
            [(now, reason or "explicit_forget", aid) for aid in eligible_ids],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Refresh trend / evidence_count for affected observations. Each
    # refresh_trend call manages its own transaction.
    for obs_id in affected_obs_ids:
        refresh_trend(conn, obs_id)

    return ForgetResult(
        tombstoned_count=len(eligible_ids),
        tombstoned_ids=eligible_ids,
        observations_affected=affected_obs_ids,
    )


def forget_by_criteria(
    conn: sqlite3.Connection,
    *,
    min_age_days: int | None = None,
    activation_below: float | None = None,
    min_retrievals: int | None = None,
    stream: str | None = None,
    memory_type: str | None = None,
    source_type: str | None = None,
    agent_id: str = "default",
    dry_run: bool = True,
    max_atoms: int = 1000,
    reason: str | None = None,
    reference_date: datetime | None = None,
) -> ForgetResult:
    """Bulk forget by predicate. Dry-run by default (preview the set
    without writing).

    Criteria are AND'd. Returns the atom IDs that match (and are
    tombstoned, unless dry_run=True).

    ``max_atoms`` is a hard cap to prevent runaway forgetting from a
    misconfigured criterion.

    ``min_retrievals``: only forget atoms whose total retrieval count
    (``retrieval`` + ``feedback_positive`` access events) is *strictly
    less than* this value. Atoms never retrieved (no access_events row)
    count as 0 and are included when the filter is active.

    Activation-based filtering requires reading atom_access_summary;
    the test below uses a SQL view to avoid pulling all atoms into
    Python. Approximate — the stored activation may be stale between
    decay passes. Sharper would be to recompute on the fly, but the
    cost on N atoms is O(N) compute and we're cleaning up here, not
    serving live retrievals.
    """
    where = ["a.agent_id = ?", "a.tombstoned = 0"]
    params: list = [agent_id]
    joins: list[str] = []

    # ``now`` for age + activation math. Defaults to wall clock; bench
    # replays pass an explicit ``reference_date`` so historical-corpus
    # runs (longmemeval, etc.) age atoms against the corpus's epoch,
    # not the wall clock that ran the bench. Without this, a forget
    # pass during a 2023-era replay computes activation against 2026
    # and tombstones the entire corpus.
    now = reference_date or datetime.now(timezone.utc)

    if min_age_days is not None:
        cutoff = (now - timedelta(days=min_age_days)).isoformat()
        where.append("a.created_at <= ?")
        params.append(cutoff)

    if stream is not None:
        where.append("a.stream = ?")
        params.append(stream)

    if memory_type is not None:
        where.append("a.memory_type = ?")
        params.append(memory_type)

    if source_type is not None:
        where.append("a.source_type = ?")
        params.append(source_type)

    # Pinned atoms are protected from criteria-based forgetting.
    # Operator can still explicitly forget them via forget(ids).
    where.append("a.is_pinned = 0")

    if min_retrievals is not None:
        # LEFT JOIN to count retrieval + feedback_positive access events.
        # COALESCE handles atoms with no rows in access_events (count = 0).
        joins.append(
            "LEFT JOIN ("
            " SELECT atom_id, COUNT(*) AS _retrieval_cnt"
            " FROM access_events"
            " WHERE source IN ('retrieval', 'feedback_positive')"
            " GROUP BY atom_id"
            ") _rk ON _rk.atom_id = a.id"
        )
        where.append("COALESCE(_rk._retrieval_cnt, 0) < ?")
        params.append(min_retrievals)

    join_sql = " ".join(joins)
    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"SELECT a.id FROM atoms a {join_sql} WHERE {where_sql} "
        f"ORDER BY a.created_at ASC LIMIT ?",
        params + [max_atoms],
    ).fetchall()
    candidate_ids = [r[0] for r in rows]

    # Activation filter applies AFTER the SQL pre-filter (it needs
    # the access summary). Skipped if criterion not set.
    if activation_below is not None and candidate_ids:
        # Late import to avoid circular dep at module load.
        import json
        from .activation import compute_activation

        summary_rows = conn.execute(
            f"SELECT atom_id, recent_ts_json, recent_weights_json, "
            f"old_count, old_weight_sum, old_oldest_ts "
            f"FROM atom_access_summary "
            f"WHERE atom_id IN ({','.join(['?'] * len(candidate_ids))})",
            candidate_ids,
        ).fetchall()
        below: list[str] = []
        summaries = {r[0]: r for r in summary_rows}
        for atom_id in candidate_ids:
            row = summaries.get(atom_id)
            if row is None:
                # No access summary = no events = activation is -inf
                # → below any finite threshold.
                below.append(atom_id)
                continue
            act = compute_activation(
                recent_ts=json.loads(row[1] or "[]"),
                recent_weights=json.loads(row[2] or "[]"),
                old_count=row[3] or 0,
                old_weight_sum=row[4] or 0.0,
                old_oldest_ts=row[5],
                now=now,
            )
            if act < activation_below:
                below.append(atom_id)
        candidate_ids = below

    if dry_run:
        return ForgetResult(
            tombstoned_count=0,
            tombstoned_ids=candidate_ids,  # preview
            dry_run=True,
        )

    return forget(conn, candidate_ids, reason=reason or "bulk_criteria")
