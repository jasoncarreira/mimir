"""Dedup pass — collapse near-duplicate raws before thematic consolidation.

Pass 1 of the two-pass consolidator. Clusters raws at a TIGHTER threshold
than the observation-synthesis pass (default 0.92 voyage / 0.92 openai
on calibrated mimir-corpus distributions — see /tmp/voyage_calibration/
percentile_map.py), picks one canonical per cluster by ACT-R activation,
folds the rest's history into the canonical, and tombstones them with
reason='merged'.

Why this is its own pass:

- Thematic consolidation synthesizes a NEW observation atom from a
  cluster of evidence. That's the right thing for "these N raws are
  related but distinct."
- Dedup MERGES — picks one of the N as canonical, drops the rest.
  Right when the N raws are saying essentially the same thing and
  keeping all N inflates evidence counts and burns retrieval slots.

Canonical-pick rule (in priority order):
    1. ACT-R activation B_i (higher = more retrieval-validated)
    2. is_pinned (pinned wins)
    3. confidence_tier on observations_metadata (high>medium>low; raws
       don't have this so falls through for raw-only clusters)
    4. evidence_count on observations_metadata (more downstream support)
    5. Oldest created_at (keep the original record)
    6. Lexicographic id (stable final tiebreaker)

What we preserve on the canonical:

- ALL access_events get rewritten to point at canonical → activation
  history is preserved (sum of (now - t_j)^(-d) is linear, so transfer
  is correct under Petrov OL)
- Topics + metadata.tags are unioned
- ``metadata.dedup_merged_ids`` is appended with the dropped atom IDs
  (audit trail; survives in the canonical's metadata JSON)
- Atom relations are rewritten (source/target redirected to canonical,
  with deduping on (source_id, target_id, relation_type))
- A NEW ``consolidated_into`` relation duplicate → canonical is added
  (mirrors observation-tier consolidate so retrieval's evidence_boost
  can lift the canonical when a duplicate would have been hit)

What we drop:

- The duplicate's atom row stays (tombstoned=1, reason='merged')
- atom_access_summary for the duplicate is preserved as-is (read paths
  filter by tombstoned=0; the row is dead-weight metadata)
- The duplicate's embedding row stays (lets us re-derive cluster
  membership for forensics; tombstone filter at retrieval drops it)

Idempotence:

Running dedup twice on the same DB is a no-op — tombstoned rows are
excluded from the candidate set, and observations/raws are processed
independently. The pass is safe to schedule alongside reflect /
consolidate.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .activation import (
    compute_activation,
    rebuild_summary_from_events,
)


# Default similarity threshold for dedup pass. Stricter than the
# thematic-consolidation default (0.80). Voyage's 1024d distribution is
# tighter at the tail; 0.92 maps to the ~99.98th percentile of pairs on
# the calibration corpus. Per-provider override comes from the same
# _PROVIDER_AUTO_THRESHOLDS table used by consolidation, with the dedup
# pass adding a fixed 0.12 above the thematic threshold for providers
# below 0.85; otherwise dedup ≈ thematic.
DEFAULT_DEDUP_THRESHOLD = 0.92

# Source-types we never dedup across. session_boundary atoms are
# structural; merging them would corrupt the per-session evidence trail.
NEVER_DEDUP_SOURCE_TYPES = ("session_boundary",)


@dataclass
class DedupResult:
    candidates_scanned: int = 0
    clusters_formed: int = 0
    canonicals_kept: list[str] = field(default_factory=list)
    duplicates_tombstoned: list[str] = field(default_factory=list)
    # cluster id (canonical id) → list of duplicate ids merged into it
    merges: dict[str, list[str]] = field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atom_activation(conn: sqlite3.Connection, atom_id: str) -> float:
    """Read the access summary and compute Petrov OL activation. Returns
    -inf if no summary row exists (atom has zero accesses).
    """
    row = conn.execute(
        "SELECT recent_ts_json, recent_weights_json, "
        "old_count, old_weight_sum, old_oldest_ts "
        "FROM atom_access_summary WHERE atom_id = ?",
        (atom_id,),
    ).fetchone()
    if row is None:
        return float("-inf")
    return compute_activation(
        recent_ts=json.loads(row[0] or "[]"),
        recent_weights=json.loads(row[1] or "[]"),
        old_count=row[2] or 0,
        old_weight_sum=row[3] or 0.0,
        old_oldest_ts=row[4],
    )


def _atom_score_key(
    conn: sqlite3.Connection,
    atom: dict,
) -> tuple:
    """Sort key for canonical selection. Higher = preferred.

    Returns a tuple of (activation, pinned, confidence_tier_rank,
    evidence_count, -created_at_unix, -id_lex). The negation on the
    last two reverses the natural sort so the older atom and the
    lexicographically smaller id win their respective tiebreakers
    when we take ``max(...)``.
    """
    aid = atom["id"]
    activation = _atom_activation(conn, aid)
    pinned = 1 if atom.get("is_pinned") else 0

    # observations_metadata is the source for confidence_tier &
    # evidence_count. Raws don't have a row; treat them as tier=low,
    # count=0 (consistent with how retrieval treats untiered atoms).
    tier_rank, ev_count = 0, 0
    row = conn.execute(
        "SELECT trend, evidence_count FROM observations_metadata WHERE atom_id = ?",
        (aid,),
    ).fetchone()
    if row is not None:
        ev_count = row[1] or 0
        # Re-use the trend label as a soft tier signal (strengthening
        # > stable > weakening > stale). Raws never have this; only
        # observations score above 0.
        tier_rank = {
            "strengthening": 3, "stable": 2,
            "weakening": 1, "stale": 0,
        }.get(row[0], 0)

    created_at = atom.get("created_at") or ""
    # Older is better → negate the lexicographic comparison by
    # flipping to most-negative-wins via tuple. We can't negate a
    # string directly; instead invert using a reverse comparable
    # surrogate (None sorts as "everything") — simplest is to compare
    # negated unix timestamp.
    try:
        created_unix = datetime.fromisoformat(created_at).timestamp()
    except (ValueError, TypeError):
        created_unix = 0.0

    return (
        activation,
        pinned,
        tier_rank,
        ev_count,
        -created_unix,   # older atom wins (smaller unix → larger -unix)
        # No id tiebreaker via -str; if all else ties, leave to
        # max()'s stability across calls (insertion order).
    )


def pick_canonical(
    conn: sqlite3.Connection,
    cluster_atoms: list[dict],
) -> dict:
    """Return the cluster member that should be the canonical.

    Pure function relative to the connection — does no writes. Caller
    is responsible for applying the merge based on the returned dict.
    """
    if len(cluster_atoms) == 1:
        return cluster_atoms[0]
    return max(cluster_atoms, key=lambda a: _atom_score_key(conn, a))


def _merge_topics(canonical_topics: list, dup_topics: list) -> list:
    """Union topics, preserving canonical's original order."""
    seen = set()
    out = []
    for t in (canonical_topics or []) + (dup_topics or []):
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _merge_metadata(
    canonical_meta: dict,
    dup_meta: dict,
    *,
    duplicate_id: str,
) -> dict:
    """Merge ``dup_meta`` into ``canonical_meta``. Returns a new dict;
    callers persist."""
    out = dict(canonical_meta or {})

    # Union tags (if present on either).
    can_tags = list(out.get("tags", []) or [])
    dup_tags = list((dup_meta or {}).get("tags", []) or [])
    seen = set(can_tags)
    for t in dup_tags:
        if t not in seen:
            seen.add(t); can_tags.append(t)
    if can_tags:
        out["tags"] = can_tags

    # Audit: append duplicate_id to dedup_merged_ids.
    merged_ids = list(out.get("dedup_merged_ids", []) or [])
    if duplicate_id not in merged_ids:
        merged_ids.append(duplicate_id)
    out["dedup_merged_ids"] = merged_ids

    return out


def _rewrite_access_events(
    conn: sqlite3.Connection, duplicate_id: str, canonical_id: str,
) -> int:
    """Redirect every access_event for ``duplicate_id`` to ``canonical_id``.
    Returns rowcount. activation history sum is linear so the canonical's
    Petrov OL activation grows by exactly the duplicate's contribution.
    Caller MUST rebuild atom_access_summary for canonical afterwards."""
    cur = conn.execute(
        "UPDATE access_events SET atom_id = ? WHERE atom_id = ?",
        (canonical_id, duplicate_id),
    )
    return cur.rowcount or 0


def _rebuild_summary(conn: sqlite3.Connection, atom_id: str) -> None:
    """Rebuild atom_access_summary from the access_events table. Used
    after we've redirected events from duplicates onto the canonical."""
    rows = conn.execute(
        "SELECT ts, weight FROM access_events WHERE atom_id = ? ORDER BY ts",
        (atom_id,),
    ).fetchall()
    if not rows:
        conn.execute(
            "DELETE FROM atom_access_summary WHERE atom_id = ?", (atom_id,),
        )
        return
    summary = rebuild_summary_from_events([(r[0], r[1]) for r in rows])
    last_ts = rows[-1][0]
    conn.execute(
        "INSERT OR REPLACE INTO atom_access_summary "
        "(atom_id, recent_ts_json, recent_weights_json, "
        " old_count, old_weight_sum, old_oldest_ts, last_updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            atom_id,
            summary["recent_ts_json"],
            summary["recent_weights_json"],
            summary["old_count"],
            summary["old_weight_sum"],
            summary["old_oldest_ts"],
            last_ts,
        ),
    )


def _rewrite_relations(
    conn: sqlite3.Connection, duplicate_id: str, canonical_id: str,
) -> int:
    """Redirect atom_relations rows where ``duplicate_id`` is either
    endpoint to ``canonical_id``. Dedupes on (source_id, target_id,
    relation_type) by deleting then re-inserting via INSERT OR IGNORE
    on the redirected rows. Returns rows affected.

    Skips self-loops created by the rewrite (e.g. an existing
    canonical→duplicate relation would become canonical→canonical;
    drop those).
    """
    affected = 0
    # First, collect existing relations involving the duplicate.
    rows = conn.execute(
        "SELECT source_id, target_id, relation_type, confidence, "
        "created_at, metadata FROM atom_relations "
        "WHERE source_id = ? OR target_id = ?",
        (duplicate_id, duplicate_id),
    ).fetchall()
    if not rows:
        return 0
    # Delete the old rows; re-insert with redirection.
    conn.execute(
        "DELETE FROM atom_relations WHERE source_id = ? OR target_id = ?",
        (duplicate_id, duplicate_id),
    )
    for src, tgt, rtype, conf, created, meta in rows:
        new_src = canonical_id if src == duplicate_id else src
        new_tgt = canonical_id if tgt == duplicate_id else tgt
        if new_src == new_tgt:
            continue   # drop self-loops
        cur = conn.execute(
            "INSERT OR IGNORE INTO atom_relations "
            "(source_id, target_id, relation_type, confidence, "
            " created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (new_src, new_tgt, rtype, conf, created, meta),
        )
        affected += cur.rowcount or 0
    return affected


def merge_duplicate_into_canonical(
    conn: sqlite3.Connection,
    *,
    canonical: dict,
    duplicate: dict,
    now_iso: str | None = None,
) -> None:
    """Apply the merge of ``duplicate`` into ``canonical``. The atoms
    table row for ``duplicate`` is tombstoned with reason='merged' and a
    ``consolidated_into`` relation is added. Caller manages the
    enclosing transaction.

    Pre-conditions enforced here:
    - duplicate is not already tombstoned
    - duplicate and canonical share an agent_id
    """
    now = now_iso or _utc_now_iso()
    can_id = canonical["id"]
    dup_id = duplicate["id"]
    if dup_id == can_id:
        return

    # Sanity: same agent. Cross-agent dedup is not in scope.
    if (canonical.get("agent_id") or "default") != (
        duplicate.get("agent_id") or "default"
    ):
        return

    # 1. Topics + metadata merge into canonical (persist).
    can_topics = json.loads(canonical.get("topics") or "[]")
    dup_topics = json.loads(duplicate.get("topics") or "[]")
    merged_topics = _merge_topics(can_topics, dup_topics)

    can_meta = json.loads(canonical.get("metadata") or "{}")
    dup_meta = json.loads(duplicate.get("metadata") or "{}")
    merged_meta = _merge_metadata(can_meta, dup_meta, duplicate_id=dup_id)
    conn.execute(
        "UPDATE atoms SET topics = ?, metadata = ? WHERE id = ?",
        (json.dumps(merged_topics), json.dumps(merged_meta), can_id),
    )

    # 2. Redirect access_events and rebuild summary.
    _rewrite_access_events(conn, dup_id, can_id)
    _rebuild_summary(conn, can_id)
    # Clear the duplicate's now-empty summary row.
    conn.execute(
        "DELETE FROM atom_access_summary WHERE atom_id = ?", (dup_id,),
    )

    # 3. Redirect atom_relations.
    _rewrite_relations(conn, dup_id, can_id)

    # 4. Add a fresh consolidated_into edge (duplicate → canonical) so
    # retrieval's evidenced_by lookups can find the canonical even if a
    # caller still has the duplicate id around. INSERT OR IGNORE
    # because _rewrite_relations may already have produced this from a
    # pre-existing edge.
    conn.execute(
        "INSERT OR IGNORE INTO atom_relations "
        "(source_id, target_id, relation_type, confidence, "
        " created_at, metadata) "
        "VALUES (?, ?, 'consolidated_into', 1.0, ?, ?)",
        (dup_id, can_id, now,
         json.dumps({"trigger": "dedup", "reason": "near_duplicate"})),
    )

    # 5. Tombstone the duplicate.
    conn.execute(
        "UPDATE atoms SET tombstoned = 1, tombstoned_at = ?, "
        "tombstoned_reason = 'merged' WHERE id = ? AND tombstoned = 0",
        (now, dup_id),
    )


def _candidate_raws_for_dedup(
    conn: sqlite3.Connection,
    *,
    lookback_days: int | None,
    agent_id: str,
) -> list[dict]:
    """Atoms eligible for dedup. Difference from consolidate's
    candidate query:

    - We INCLUDE raws regardless of whether they have access_events
      (dedup is more about "is this redundant" than "has this been
      retrieved"). The activation tiebreaker will return -inf for raws
      with no access history, but they still cluster.
    - We exclude session_boundary source_type — those are structural
      markers, not evidence.
    """
    where = [
        "a.memory_type = 'raw'",
        "a.tombstoned = 0",
        "a.source_type NOT IN ({})".format(
            ",".join("?" * len(NEVER_DEDUP_SOURCE_TYPES))
        ),
        "a.agent_id = ?",
    ]
    params: list = list(NEVER_DEDUP_SOURCE_TYPES) + [agent_id]
    if lookback_days is not None:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=lookback_days)).isoformat()
        where.append("a.created_at >= ?")
        params.append(cutoff)

    rows = conn.execute(
        f"SELECT a.id, a.content, a.stream, a.memory_type, a.source_type, "
        f"  a.created_at, a.topics, a.metadata, a.is_pinned, a.agent_id, "
        f"  a.session_id "
        f"FROM atoms a "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY a.created_at",
        params,
    ).fetchall()
    cols = ("id", "content", "stream", "memory_type", "source_type",
            "created_at", "topics", "metadata", "is_pinned",
            "agent_id", "session_id")
    return [dict(zip(cols, r)) for r in rows]


# Injection type for the dedup-tier clusterer. Same shape as the
# thematic ClusterFn but bound to a stricter threshold.
DedupClusterFn = Callable[[list[dict]], list[list[dict]]]


def dedup_pass(
    conn: sqlite3.Connection,
    *,
    cluster_fn: DedupClusterFn,
    agent_id: str = "default",
    lookback_days: int | None = None,
    min_cluster_size: int = 2,
    dry_run: bool = False,
    max_merges: int | None = None,
) -> DedupResult:
    """Run the dedup pass over recent raws for one agent.

    ``cluster_fn`` is a clusterer bound to the dedup threshold (caller
    supplies; client.py builds one via make_default_cluster_fn at the
    tighter threshold).

    ``min_cluster_size=2`` is the lowest meaningful value — a cluster
    of two is one duplicate to merge. Setting higher requires more
    evidence before collapsing.

    Returns a DedupResult with counts + per-canonical merge lists.
    """
    result = DedupResult()
    raws = _candidate_raws_for_dedup(
        conn, lookback_days=lookback_days, agent_id=agent_id,
    )
    result.candidates_scanned = len(raws)
    if len(raws) < min_cluster_size:
        return result

    clusters = cluster_fn(raws)
    result.clusters_formed = len(clusters)

    merge_count = 0
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue
        canonical = pick_canonical(conn, cluster)
        duplicates = [a for a in cluster if a["id"] != canonical["id"]]
        if not duplicates:
            continue
        if max_merges is not None and merge_count >= max_merges:
            break

        result.canonicals_kept.append(canonical["id"])
        result.merges[canonical["id"]] = [d["id"] for d in duplicates]
        result.duplicates_tombstoned.extend(d["id"] for d in duplicates)
        merge_count += 1

        if dry_run:
            continue

        now = _utc_now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Re-read canonical from DB to get post-merge state if it
            # appeared in an earlier cluster. Defensive — the
            # clusterer should not put the same atom in two clusters,
            # but make the merge idempotent regardless.
            current_can = conn.execute(
                "SELECT id, topics, metadata, agent_id, tombstoned "
                "FROM atoms WHERE id = ?",
                (canonical["id"],),
            ).fetchone()
            if current_can is None or current_can[4] == 1:
                conn.rollback()
                continue
            canonical_dict = {
                "id": current_can[0], "topics": current_can[1],
                "metadata": current_can[2], "agent_id": current_can[3],
            }
            for dup in duplicates:
                merge_duplicate_into_canonical(
                    conn,
                    canonical=canonical_dict,
                    duplicate=dup,
                    now_iso=now,
                )
                # Re-read canonical so the next dup sees merged
                # topics+metadata (so dedup_merged_ids accumulates).
                row = conn.execute(
                    "SELECT id, topics, metadata, agent_id "
                    "FROM atoms WHERE id = ?",
                    (canonical["id"],),
                ).fetchone()
                if row:
                    canonical_dict = {
                        "id": row[0], "topics": row[1],
                        "metadata": row[2], "agent_id": row[3],
                    }
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return result
