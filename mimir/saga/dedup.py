"""Dedup pass — collapse near-duplicate raws before thematic consolidation.

Pass 1 of the two-pass consolidator. Clusters raws at a TIGHTER threshold
than the observation-synthesis pass (default 0.92 floor for all providers —
see ``DEFAULT_DEDUP_THRESHOLD`` below for the calibration rationale),
picks one canonical per cluster by ACT-R activation, folds the rest's
history into the canonical, and tombstones them with reason='merged'.

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
# thematic-consolidation default (0.80). Acts as a **floor for all
# providers** when ``SagaStore.consolidate`` resolves the effective
# threshold (``max(_PROVIDER_AUTO_THRESHOLDS[provider], 0.92)``).
# Calibration: on mimir's saga.db (693 atoms), 0.92 sits at the
# ~99.98th percentile of pair similarity for both Voyage 4-lite (1024d)
# and OpenAI text-embedding-3-large (3072d). Provider distributions
# differ at the head but converge at this tail. Per-corpus tuning may
# warrant lifting or lowering this; treat as a starting point.
DEFAULT_DEDUP_THRESHOLD = 0.92

# Source-types we never dedup across. session_boundary atoms are
# structural; merging them would corrupt the per-session evidence trail.
NEVER_DEDUP_SOURCE_TYPES = ("session_boundary",)

# Skill-learning atoms (#266) are partitioned out of the general dedup
# pass and deduped per-skill instead (see ``skill_scope`` below). Local
# copy of mimir.skill_memory.SKILL_LEARNING_SOURCE_TYPE — saga is the
# lower layer and must not import up into mimir.*.
_SKILL_LEARNING_SOURCE_TYPE = "skill_learning"


@dataclass
class DedupResult:
    candidates_scanned: int = 0
    clusters_formed: int = 0
    canonicals_kept: list[str] = field(default_factory=list)
    duplicates_tombstoned: list[str] = field(default_factory=list)
    # cluster id (canonical id) → list of duplicate ids merged into it
    merges: dict[str, list[str]] = field(default_factory=dict)
    # Observations whose ``evidenced_by`` set was modified during the pass
    # (an evidence atom got merged into a canonical). Their
    # ``observations_metadata.evidence_count`` is rebuilt from the live
    # relation table at end-of-pass — without that sweep the cached
    # count drifts above the actual count by one per collapsed edge.
    evidence_counts_rebuilt: list[str] = field(default_factory=list)


#: Baseline ``encoding_confidence`` value for a freshly-stored atom.
#: Mirrors the default in ``store.py``, ``schema.sql``, and
#: ``_config_io.py``'s ``default_encoding_confidence`` — keep these
#: synced when bumping. The retrieval-ranking factor (see
#: ``recall.ENCODING_CONFIDENCE_WEIGHT``) computes
#: ``(encoding_confidence - BASELINE_ENCODING_CONFIDENCE)`` so an atom
#: that's never been absorbed contributes zero, and only post-baseline
#: confidence (from dedup absorption or future feedback nudges) moves
#: the score.
BASELINE_ENCODING_CONFIDENCE = 0.7

#: Coefficient for the asymptotic dedup-absorption nudge applied to
#: ``encoding_confidence`` when a duplicate is folded into a canonical:
#:
#:     new = old + (1.0 - old) * ABSORPTION_COEFFICIENT
#:
#: With 0.3, the trajectory from baseline (0.7) over N absorptions is:
#: 0.700 → 0.790 → 0.853 → 0.897 → 0.928 → 0.949 → ... → 1.0.
#: Asymptotic so a single hot-context atom can't reach 1.0 by being
#: re-encoded a few times — sustained re-encoding across many sessions
#: still moves the score but with diminishing returns.
ABSORPTION_COEFFICIENT = 0.3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump_encoding_confidence(current: float) -> float:
    """Apply one dedup-absorption nudge to ``current``. Asymptotic
    approach to 1.0; never exceeds it; never decreases. Pure function,
    safe to test independently."""
    if not isinstance(current, (int, float)):
        return BASELINE_ENCODING_CONFIDENCE
    bumped = current + (1.0 - current) * ABSORPTION_COEFFICIENT
    return min(1.0, max(BASELINE_ENCODING_CONFIDENCE, bumped))


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
    evidence_count, recency_key) where ``recency_key`` is
    ``-created_at_unix`` so older atoms (smaller unix → larger ``-unix``)
    win the recency tiebreaker under ``max(...)``. Malformed/missing
    ``created_at`` falls back to ``float('-inf')`` so well-formed atoms
    always win the recency tiebreaker — see the inline note where
    ``recency_key`` is computed.

    If all five keys tie, ``max()`` returns the first occurrence (Python
    docs: stable on ties). Across runs the canonical may differ if the
    input list ordering differs; this is a very low-probability edge
    case (six other criteria tied) and the consequence is benign
    (different equally-valid canonicals across reruns).
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
    # Older is better → use -unix so older (smaller unix) → larger
    # -unix and wins ``max(...)``. Fallback when created_at is missing
    # or unparseable: malformed atoms should LOSE to any well-formed
    # one, so we emit -inf (smallest possible -unix). Every store()
    # writes a real ISO ts so this only fires on legacy/corrupted rows;
    # demoting them keeps a real, dateable atom as canonical.
    try:
        created_unix = datetime.fromisoformat(created_at).timestamp()
        recency_key = -created_unix
    except (ValueError, TypeError):
        recency_key = float("-inf")

    return (
        activation,
        pinned,
        tier_rank,
        ev_count,
        recency_key,   # older atom wins (smaller unix → larger -unix)
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
    conn: sqlite3.Connection,
    duplicate_id: str,
    canonical_id: str,
    *,
    touched_observations: set[str] | None = None,
) -> int:
    """Redirect atom_relations rows where ``duplicate_id`` is either
    endpoint to ``canonical_id``. Dedupes on (source_id, target_id,
    relation_type) by deleting then re-inserting via INSERT OR IGNORE
    on the redirected rows. Returns rows affected.

    Skips self-loops created by the rewrite (e.g. an existing
    canonical→duplicate relation would become canonical→canonical;
    drop those).

    If ``touched_observations`` is provided, every observation atom_id
    whose ``evidenced_by`` evidence set was modified (either by losing
    a duplicate-evidence row that collapsed into an existing edge, or
    by gaining one via redirection) is added to it. Caller uses this
    to rebuild ``observations_metadata.evidence_count`` after the pass —
    otherwise the cached count drifts above the live relation count.
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
    # Note any observation whose evidenced_by set is about to change —
    # we redirect rows below, which may collapse two evidenced_by edges
    # (obs→dup + obs→canonical) into one via INSERT OR IGNORE.
    if touched_observations is not None:
        for src, tgt, rtype, *_ in rows:
            if rtype == "evidenced_by" and tgt == duplicate_id:
                touched_observations.add(src)
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
    touched_observations: set[str] | None = None,
) -> None:
    """Apply the merge of ``duplicate`` into ``canonical``. The atoms
    table row for ``duplicate`` is tombstoned with reason='merged' and a
    ``consolidated_into`` relation is added. Caller manages the
    enclosing transaction.

    Pre-conditions enforced here:
    - duplicate is not already tombstoned
    - duplicate and canonical share an agent_id

    If ``touched_observations`` is provided, any observation whose
    ``evidenced_by`` edge set was redirected during this merge is added
    to it. Caller uses the accumulated set to rebuild
    ``observations_metadata.evidence_count`` once at the end of the
    dedup pass.
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

    # 1. Topics + metadata merge into canonical (persist). Also nudge
    #    ``encoding_confidence`` upward — the agent saved this fact more
    #    than once, which is independent evidence that it's a stable,
    #    well-supported encoding. Asymptotic to 1.0 so repeated dedup
    #    in a single hot-context session can't fully saturate the
    #    signal. See ``_bump_encoding_confidence`` for the math.
    can_topics = json.loads(canonical.get("topics") or "[]")
    dup_topics = json.loads(duplicate.get("topics") or "[]")
    merged_topics = _merge_topics(can_topics, dup_topics)

    can_meta = json.loads(canonical.get("metadata") or "{}")
    dup_meta = json.loads(duplicate.get("metadata") or "{}")
    merged_meta = _merge_metadata(can_meta, dup_meta, duplicate_id=dup_id)

    # Inherit the HIGHER ``encoding_confidence`` from the {canonical,
    # duplicate} pair before bumping. ``pick_canonical`` orders by
    # activation/pinned/etc. — NOT by encoding_confidence — so the
    # canonical is often NOT the more-encoded atom in the cluster.
    # Without this inheritance, absorbing a previously-merged
    # duplicate (k=3 → 0.897) into a fresh canonical (k=0 → 0.7)
    # would clobber the duplicate's accumulated confidence down to
    # 0.79. The MAX rule preserves the strongest evidence; the bump
    # then adds the current merge's vote on top.
    can_enc = canonical.get("encoding_confidence")
    if can_enc is None:
        # Caller didn't fetch the column on the canonical — read fresh.
        row = conn.execute(
            "SELECT encoding_confidence FROM atoms WHERE id = ?",
            (can_id,),
        ).fetchone()
        can_enc = (
            float(row[0]) if row and row[0] is not None
            else BASELINE_ENCODING_CONFIDENCE
        )
    dup_enc = duplicate.get("encoding_confidence")
    if dup_enc is None:
        row = conn.execute(
            "SELECT encoding_confidence FROM atoms WHERE id = ?",
            (dup_id,),
        ).fetchone()
        dup_enc = (
            float(row[0]) if row and row[0] is not None
            else BASELINE_ENCODING_CONFIDENCE
        )
    starting = max(float(can_enc), float(dup_enc))
    new_enc_conf = _bump_encoding_confidence(starting)
    # Cache the new value on the canonical dict so the caller's
    # in-memory view stays consistent (relevant when a single dedup
    # pass folds N duplicates into the same canonical — each
    # iteration sees the previous iteration's bumped value).
    canonical["encoding_confidence"] = new_enc_conf

    conn.execute(
        "UPDATE atoms SET topics = ?, metadata = ?, "
        "encoding_confidence = ? WHERE id = ?",
        (
            json.dumps(merged_topics),
            json.dumps(merged_meta),
            new_enc_conf,
            can_id,
        ),
    )

    # 2. Redirect access_events and rebuild summary.
    _rewrite_access_events(conn, dup_id, can_id)
    _rebuild_summary(conn, can_id)
    # Clear the duplicate's now-empty summary row.
    conn.execute(
        "DELETE FROM atom_access_summary WHERE atom_id = ?", (dup_id,),
    )

    # 3. Redirect atom_relations. Observations whose evidenced_by edges
    # touched the duplicate get noted for evidence_count rebuild.
    _rewrite_relations(
        conn, dup_id, can_id, touched_observations=touched_observations,
    )

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
    skill_scope: str | None = None,
) -> list[dict]:
    """Atoms eligible for dedup. Difference from consolidate's
    candidate query:

    - We INCLUDE raws regardless of whether they have access_events
      (dedup is more about "is this redundant" than "has this been
      retrieved"). The activation tiebreaker will return -inf for raws
      with no access history, but they still cluster.
    - We exclude session_boundary source_type — those are structural
      markers, not evidence.

    *skill_scope* partitions skill-learning atoms (#266) so a skill's
    near-duplicate gotchas collapse against each other but never against
    another skill's learnings or a general raw:
    - ``None`` (default, the general pass): EXCLUDE ``skill_learning``
      atoms (on top of the structural exclusions above).
    - ``"<skill>"`` (per-skill pass): include ONLY that skill's
      ``skill_learning`` atoms.
    """
    where = [
        "a.memory_type = 'raw'",
        "a.tombstoned = 0",
        "a.agent_id = ?",
    ]
    params: list = [agent_id]
    if skill_scope is None:
        where.append(
            "a.source_type NOT IN ({})".format(
                ",".join("?" * (len(NEVER_DEDUP_SOURCE_TYPES) + 1))
            )
        )
        params.extend(NEVER_DEDUP_SOURCE_TYPES)
        params.append(_SKILL_LEARNING_SOURCE_TYPE)
    else:
        where.append("a.source_type = ?")
        where.append("json_extract(a.metadata, '$.skill') = ?")
        params.append(_SKILL_LEARNING_SOURCE_TYPE)
        params.append(skill_scope)
    if lookback_days is not None:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=lookback_days)).isoformat()
        where.append("a.created_at >= ?")
        params.append(cutoff)

    rows = conn.execute(
        f"SELECT a.id, a.content, a.stream, a.memory_type, a.source_type, "
        f"  a.created_at, a.topics, a.metadata, a.is_pinned, a.agent_id, "
        f"  a.session_id, a.encoding_confidence "
        f"FROM atoms a "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY a.created_at",
        params,
    ).fetchall()
    cols = ("id", "content", "stream", "memory_type", "source_type",
            "created_at", "topics", "metadata", "is_pinned",
            "agent_id", "session_id", "encoding_confidence")
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
    max_clusters: int | None = None,
    skill_scope: str | None = None,
) -> DedupResult:
    """Run the dedup pass over recent raws for one agent.

    ``cluster_fn`` is a clusterer bound to the dedup threshold (caller
    supplies; client.py builds one via make_default_cluster_fn at the
    tighter threshold).

    ``min_cluster_size=2`` is the lowest meaningful value — a cluster
    of two is one duplicate to merge. Setting higher requires more
    evidence before collapsing.

    ``max_clusters`` caps the number of CLUSTERS processed, not the
    number of atoms tombstoned. A cluster of N near-duplicates counts
    as one against the cap and tombstones (N-1) atoms. Use this to
    bound runtime on cold-start passes; leave None for unbounded.

    ``skill_scope`` (#266) partitions skill-learning atoms: ``None``
    dedups the general corpus and excludes ``skill_learning`` atoms;
    a skill name dedups only that skill's learnings. See
    ``_candidate_raws_for_dedup``.

    Returns a DedupResult with counts + per-canonical merge lists.
    """
    result = DedupResult()
    raws = _candidate_raws_for_dedup(
        conn, lookback_days=lookback_days, agent_id=agent_id,
        skill_scope=skill_scope,
    )
    result.candidates_scanned = len(raws)
    if len(raws) < min_cluster_size:
        return result

    clusters = cluster_fn(raws)
    result.clusters_formed = len(clusters)

    # Accumulates across all merges in this pass. Each merge of a
    # duplicate that was someone's evidence adds the parent observation
    # id here; the post-pass sweep rebuilds evidence_count for each.
    touched_observations: set[str] = set()

    clusters_processed = 0
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue
        canonical = pick_canonical(conn, cluster)
        duplicates = [a for a in cluster if a["id"] != canonical["id"]]
        if not duplicates:
            continue
        if max_clusters is not None and clusters_processed >= max_clusters:
            break

        result.canonicals_kept.append(canonical["id"])
        result.merges[canonical["id"]] = [d["id"] for d in duplicates]
        result.duplicates_tombstoned.extend(d["id"] for d in duplicates)
        clusters_processed += 1

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
                    touched_observations=touched_observations,
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

    # ── Evidence-count rebuild sweep ──────────────────────────────────
    # Observations whose evidenced_by edges had a duplicate folded into
    # a canonical end up with a live edge count below their cached
    # ``observations_metadata.evidence_count``. Recompute the cached
    # value for each touched observation so ``find_superseded_observations``
    # and any display surface stays consistent. One short transaction
    # so the rebuild is atomic vs concurrent reads.
    if touched_observations and not dry_run:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for obs_id in touched_observations:
                live_count = conn.execute(
                    "SELECT COUNT(*) FROM atom_relations "
                    "WHERE source_id = ? AND relation_type = 'evidenced_by'",
                    (obs_id,),
                ).fetchone()[0]
                cur = conn.execute(
                    "UPDATE observations_metadata SET evidence_count = ? "
                    "WHERE atom_id = ? AND evidence_count != ?",
                    (live_count, obs_id, live_count),
                )
                if cur.rowcount > 0:
                    result.evidence_counts_rebuilt.append(obs_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return result
