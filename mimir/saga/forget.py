"""forget — explicit tombstoning.

Two entry points:

- ``forget(atom_ids, reason)`` — explicit, by id. The agent or
  operator says "this atom is wrong / private / no longer relevant."
- ``forget_by_criteria(criteria, dry_run)`` — bulk by predicate.
  Periodic cleanup pass; preview by default.

Tombstoning sets ``tombstoned=1`` and records timestamp + reason.
Operator restore is available for explicit/bulk forget tombstones. Tombstoned atoms are excluded from
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
- Derived triple rows (retained for audit, but ``tombstoned=1``)
- Derived world-state rows (retained as history; current rows are
  end-dated and have ``is_current=0``)
- access_events for the atom (history is preserved)
- atom_relations involving the atom (historical record)
- embedding row (could be deleted to reclaim space; defer to a
  separate vacuum pass — not bug-adjacent)
"""

from __future__ import annotations

import sqlite3
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .observations import refresh_trend


@dataclass
class ForgetResult:
    tombstoned_count: int = 0
    tombstoned_ids: list[str] = field(default_factory=list)
    observations_affected: list[str] = field(default_factory=list)
    dry_run: bool = False
    candidates: list[dict] = field(default_factory=list)
    preview_token: str | None = None


def _preview_token(ids: list[str], criteria: dict) -> str:
    payload = json.dumps([sorted(ids), criteria], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def unforget(conn: sqlite3.Connection, *, ids: list[str] | None = None,
             reason: str | None = None) -> list[str]:
    """Restore explicitly forgotten rows, preserving an audit trail in the DB."""
    if (ids is None) == (reason is None) or (ids is not None and not ids):
        raise ValueError("Specify exactly one of nonempty ids or reason")
    if ids is not None:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id FROM atoms WHERE id IN ({placeholders}) AND tombstoned = 1 "
            "AND tombstoned_reason IN ('bulk_criteria', 'explicit_forget')", ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM atoms WHERE tombstoned = 1 AND tombstoned_reason = ? "
            "AND tombstoned_reason IN ('bulk_criteria', 'explicit_forget')", (reason,),
        ).fetchall()
    restored = [row[0] for row in rows]
    previous_reasons = dict(conn.execute(
        f"SELECT id, tombstoned_reason FROM atoms WHERE id IN ({','.join('?' for _ in restored)})", restored,
    ).fetchall()) if restored else {}
    if not restored:
        return []
    placeholders = ",".join("?" for _ in restored)
    now = _utc_now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE atoms SET tombstoned = 0, tombstoned_at = NULL, "
            f"tombstoned_reason = NULL WHERE id IN ({placeholders})", restored,
        )
        conn.execute(
            f"UPDATE triples SET tombstoned = 0 WHERE source_atom_id IN ({placeholders})", restored,
        )
        conn.execute(
            "UPDATE world_state SET is_current = 1, valid_until = NULL, updated_at = ? "
            "WHERE source_triple_id IN (SELECT id FROM triples WHERE source_atom_id IN ("
            f"{placeholders})) AND NOT EXISTS (SELECT 1 FROM world_state newer "
            "WHERE newer.subject = world_state.subject AND newer.predicate = world_state.predicate "
            "AND newer.is_current = 1)", (now, *restored),
        )
        for obs_id, in conn.execute(
            "SELECT DISTINCT source_id FROM atom_relations WHERE relation_type = 'evidenced_by' "
            f"AND target_id IN ({placeholders})", restored,
        ).fetchall():
            refresh_trend(conn, obs_id, manage_transaction=False)
            conn.execute(
                "UPDATE observations_metadata SET evidence_count = (SELECT COUNT(*) FROM atom_relations ar "
                "JOIN atoms a ON a.id = ar.target_id WHERE ar.source_id = ? "
                "AND ar.relation_type = 'evidenced_by' AND a.tombstoned = 0) WHERE atom_id = ?",
                (obs_id, obs_id),
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS forget_restore_audit ("
            "atom_id TEXT NOT NULL, restored_at TEXT NOT NULL, previous_reason TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO forget_restore_audit VALUES (?, ?, ?)",
            [(aid, now, previous_reasons[aid]) for aid in restored],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return restored


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

    # Tombstones and all dependent observation metadata updates are one SQL
    # mutation. Authorization is preflighted by SagaStore before entering this
    # function; committing a partial dependency refresh would violate the same
    # all-or-nothing boundary even if no unauthorized row were touched.
    began = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        began = True
        conn.executemany(
            "UPDATE atoms SET tombstoned = 1, tombstoned_at = ?, "
            "tombstoned_reason = ? WHERE id = ? AND tombstoned = 0",
            [(now, reason or "explicit_forget", aid) for aid in eligible_ids],
        )
        eligible_placeholders = ",".join(["?"] * len(eligible_ids))
        conn.execute(
            "UPDATE world_state SET is_current = 0, valid_until = ?, updated_at = ? "
            "WHERE is_current = 1 AND source_triple_id IN ("
            "SELECT id FROM triples "
            f"WHERE source_atom_id IN ({eligible_placeholders}))",
            (now, now, *eligible_ids),
        )
        conn.execute(
            "UPDATE triples SET tombstoned = 1 "
            f"WHERE source_atom_id IN ({eligible_placeholders}) "
            "AND tombstoned = 0",
            eligible_ids,
        )
        for obs_id in affected_obs_ids:
            refresh_trend(conn, obs_id, manage_transaction=False)
            live_count = conn.execute(
                "SELECT COUNT(*) FROM atom_relations ar "
                "JOIN atoms t ON t.id = ar.target_id "
                "WHERE ar.source_id = ? "
                "  AND ar.relation_type = 'evidenced_by' "
                "  AND t.tombstoned = 0",
                (obs_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE observations_metadata SET evidence_count = ? WHERE atom_id = ?",
                (live_count, obs_id),
            )
        conn.commit()
    except Exception:
        if began:
            conn.rollback()
        raise

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
    owner_principal: str | None = None,
    origin_domains: tuple[str, ...] | None = None,
    preview_token: str | None = None,
    allow_bulk: bool = False,
) -> ForgetResult:
    """Bulk forget by predicate. Dry-run by default (preview the set
    without writing).

    Criteria are AND'd. ``owner_principal`` is the destructive authorization
    boundary when provided; ``origin_domains`` cannot widen or narrow that
    owner-authorized set and is insufficient on its own. Calls that omit both
    remain available for administrative bulk cleanup. Returns the atom IDs that
    match (and are tombstoned, unless dry_run=True).

    ``max_atoms`` is a hard cap to prevent runaway forgetting from a
    misconfigured criterion. Destructive calls require at least one selection
    criterion; agent/owner/origin scope and ``max_atoms`` do not count.

    ``min_retrievals``: only forget atoms whose total retrieval count
    (``retrieval`` + ``feedback_positive`` access events) is *strictly
    less than* this value. Atoms never retrieved (no access_events row)
    count as 0 and are included when the filter is active.

    ``owner_principal``: if provided, only forget atoms owned by this
    principal. This enables authorization filtering for non-admin users and
    takes precedence over ``origin_domains``.

    Activation-based filtering requires reading atom_access_summary;
    the test below uses a SQL view to avoid pulling all atoms into
    Python. Approximate — the stored activation may be stale between
    decay passes. Sharper would be to recompute on the fly, but the
    cost on N atoms is O(N) compute and we're cleaning up here, not
    serving live retrievals.
    """
    if activation_below is not None and (not math.isfinite(activation_below) or activation_below > 0):
        raise ValueError("activation_below must be finite and <= 0 (log activation; -inf means never accessed)")
    where = ["a.agent_id = ?", "a.tombstoned = 0"]
    params: list = [agent_id]

    if owner_principal is not None:
        where.append("a.owner_principal = ?")
        params.append(owner_principal)
    elif origin_domains is not None:
        return ForgetResult(dry_run=dry_run)

    if not dry_run and all(value is None for value in (
        min_age_days, activation_below, min_retrievals, stream, memory_type, source_type,
    )):
        raise ValueError(
            "Destructive forget_by_criteria requires at least one selection criterion: "
            "min_age_days, activation_below, min_retrievals, stream, memory_type, or "
            "source_type. Agent/owner/origin scope and max_atoms do not count; "
            "set a selection criterion or use dry_run=True to preview."
        )
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

    criteria = dict(min_age_days=min_age_days, activation_below=activation_below,
                    min_retrievals=min_retrievals, stream=stream, memory_type=memory_type,
                    source_type=source_type, agent_id=agent_id, owner_principal=owner_principal,
                    origin_domains=origin_domains, max_atoms=max_atoms)
    token = _preview_token(candidate_ids, criteria)
    if dry_run:
        candidates = []
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            details = {r[0]: r for r in conn.execute(
                f"SELECT id, created_at, stream, memory_type, content FROM atoms WHERE id IN ({placeholders})",
                candidate_ids,
            )}
            candidates = [dict(id=aid, created_at=details[aid][1], stream=details[aid][2],
                               memory_type=details[aid][3], snippet=(details[aid][4] or "")[:160])
                          for aid in candidate_ids]
        return ForgetResult(
            tombstoned_count=0,
            tombstoned_ids=candidate_ids,  # preview
            dry_run=True,
            candidates=candidates,
            preview_token=token,
        )

    if not preview_token or preview_token != token:
        raise ValueError("Preview token missing or selection changed; run dry_run=True again and review the candidates")
    live = conn.execute("SELECT COUNT(*) FROM atoms WHERE agent_id = ? AND tombstoned = 0", (agent_id,)).fetchone()[0]
    if candidate_ids and not allow_bulk and (len(candidate_ids) >= max_atoms or len(candidate_ids) > live * .10):
        raise ValueError(f"Bulk forget refused: {len(candidate_ids)} of {live} live atoms ({len(candidate_ids) / live:.1%}); pass allow_bulk=True after reviewing the preview")
    return forget(conn, candidate_ids, reason=reason or "bulk_criteria")
