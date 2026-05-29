"""Periodic cross-session consolidation — TIER-2 internal helper.

Production callers should use ``SagaStore.consolidate`` (in
client.py), which is the canonical async entry point. It handles:
- concurrent LLM fan-out via a semaphore
- rich synthesis (triples + contradictions + P47 prior_block + P48
  vocab_block) via ``make_async_rich_synth_fn``
- correct routing of outputs (observations / triples / supersedes)
  through the per-cluster restructure pass

This module's standalone ``consolidate()`` is the **tier-2** path —
observation-only synthesis, sync orchestration, no triples / no rich
prompt. It's retained for the ``test_memory_tier2b.py`` regression
suite and for any future caller that genuinely wants only the
observation tier. **Not exported** from the package's public
``__init__`` for that reason: ``from mimir.saga import consolidate``
would silently get the simpler path and miss tier-3 features.

Complement to reflect():

- ``reflect()`` runs at session-end on the session's atoms. Catches
  within-session clusters of related raws and synthesizes one
  observation per cluster. Always emits a session_boundary.
- ``consolidate()`` runs on a schedule (weekly default) across all
  recent raws regardless of session. Catches cross-session
  accumulation — the case where the same fact gets re-mentioned in
  multiple separate sessions without ever clustering in any one of
  them.

Empirically motivated (per operator observation 2026-05): running
Hindsight against LongMemEval, atoms accumulate duplicates that
session-scoped reflection can't reach. Cross-session duplicates need
a non-session-scoped pass; that's this module's job.

Scope of the pass:

- Atoms with memory_type='raw' and tombstoned=0
- Accessed in the last ``lookback_days`` (default 30) — bounds the
  clustering work without missing recent activity
- NOT already covered by an observation (no incoming
  ``consolidated_into`` relation) — those are already represented

Clusters that produce observations also link the source raws via
``consolidated_into`` so subsequent consolidate passes skip them.

Transaction shape: same as reflect — LLM calls outside transactions,
one short transaction per emitted observation. Doesn't hold a write
lock across the (potentially many) LLM synthesis calls.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .mark_access import AccessEvent, mark_access
from .observations import (
    find_equal_evidence_obs, find_superseded_observations, refresh_trend,
)
from .store import store


# Default cadence. Weekly is appropriate for a single-agent workload —
# enough time to accumulate cross-session duplicates worth merging, not
# so frequent that synthesis cost dominates.
DEFAULT_LOOKBACK_DAYS = 30

# Minimum cluster size to justify an observation. Higher than
# reflect's threshold because we want cross-session conviction —
# three independent mentions across sessions is a stronger signal
# than three mentions in one session.
MIN_CLUSTER_SIZE_FOR_OBSERVATION = 3

# Cap on observations emitted per consolidate pass. Bounds LLM cost.
# Saga's bench TOMLs override the code default (50) to 20 across
# every variant including the canonical 81.6% run. We match the
# bench override here so the bench setup is directly comparable.
# At threshold 0.80 we form ~12 clusters/question per the sweep —
# the cap doesn't bind in practice, but if it ever does that's a
# real divergence from saga's bench shape and we want to see it.
# Production callers can override via consolidate(max_observations=50)
# to match saga's production default.
MAX_OBSERVATIONS_PER_RUN = 20

# Default similarity threshold for clustering (matches cluster.py).
DEFAULT_SIMILARITY_THRESHOLD = 0.80

# Skill-learning atoms (#266) are partitioned out of the general
# consolidation/dedup passes and processed per-skill instead. Local
# copy of mimir.skill_memory.SKILL_LEARNING_SOURCE_TYPE — saga is the
# lower layer and must not import up into mimir.*.
_SKILL_LEARNING_SOURCE_TYPE = "skill_learning"


# Injected callables — same shapes as in reflect.py
ObservationSynthFn = Callable[[list[dict]], tuple[str, list[str]]]
ClusterFn = Callable[[list[dict]], list[list[dict]]]


@dataclass
class ConsolidateResult:
    candidates_scanned: int = 0
    clusters_formed: int = 0
    observations_emitted: list[str] = field(default_factory=list)
    observations_superseded: list[tuple[str, str]] = field(
        default_factory=list,
    )
    skipped_already_covered: int = 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_raws(
    conn: sqlite3.Connection,
    *,
    lookback_days: int,
    agent_id: str,
    skill_scope: str | None = None,
) -> list[dict]:
    """Atoms eligible for cross-session consolidation.

    Selection criteria:
    - memory_type = 'raw'
    - tombstoned = 0
    - At least one access_event in the lookback window

    **Already-cited raws are NOT filtered out.** A raw that's already
    evidence for some observation can legitimately appear in a new
    cluster that brings in additional raws — the resulting larger
    cluster forms a superset of the old observation's evidence and
    triggers supersession (the new observation is created, the old
    is linked via ``supersedes`` and demoted at retrieval). Filtering
    out already-cited raws preemptively forecloses that whole class
    of consolidations.

    Equal-evidence redundancy (the cluster matches an existing
    observation's evidence exactly) is handled in the synthesis loop
    via ``find_equal_evidence_obs``.

    *skill_scope* partitions skill-learning atoms (#266) into their own
    consolidation passes, so a skill's gotchas never merge into a general
    cross-session observation and two unrelated skills never cluster
    together:
    - ``None`` (default, the general pass): EXCLUDE all ``skill_learning``
      atoms.
    - ``"<skill>"`` (per-skill pass): include ONLY that skill's
      ``skill_learning`` atoms.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=lookback_days)).isoformat()
    if skill_scope is None:
        scope_clause = "AND a.source_type != ?"
        scope_params: tuple = (_SKILL_LEARNING_SOURCE_TYPE,)
    else:
        scope_clause = (
            "AND a.source_type = ? "
            "AND json_extract(a.metadata, '$.skill') = ?"
        )
        scope_params = (_SKILL_LEARNING_SOURCE_TYPE, skill_scope)
    rows = conn.execute(f"""
        SELECT DISTINCT a.id, a.content, a.stream, a.memory_type,
               a.source_type, a.created_at, a.topics, a.metadata,
               a.agent_id, a.session_id
        FROM atoms a
        JOIN access_events e ON e.atom_id = a.id
        WHERE a.memory_type = 'raw'
          AND a.tombstoned = 0
          AND a.agent_id = ?
          AND e.ts >= ?
          {scope_clause}
    """, (agent_id, cutoff, *scope_params)).fetchall()
    cols = ("id", "content", "stream", "memory_type", "source_type",
            "created_at", "topics", "metadata", "agent_id", "session_id")
    return [dict(zip(cols, r)) for r in rows]


def distinct_skill_scopes(
    conn: sqlite3.Connection, *, agent_id: str | None = None,
) -> list[str]:
    """Distinct skill names with at least one live skill-learning atom.

    Drives the per-skill consolidation loop (#266): general
    consolidation excludes ``skill_learning`` atoms, so each skill's
    learnings dedup in their own scoped pass. A skill whose atoms are
    all tombstoned drops out (no pass needed). ``NULL``/empty skill
    tags are skipped — those can't be scoped. *agent_id* optionally
    restricts to one agent's atoms (the per-skill dedup runs per-agent,
    matching the general pass).
    """
    where = [
        "source_type = ?",
        "tombstoned = 0",
        "skill IS NOT NULL",
        "skill != ''",
    ]
    params: list = [_SKILL_LEARNING_SOURCE_TYPE]
    if agent_id is not None:
        where.append("agent_id = ?")
        params.append(agent_id)
    rows = conn.execute(
        f"""
        SELECT DISTINCT json_extract(metadata, '$.skill') AS skill
        FROM atoms
        WHERE {' AND '.join(where)}
        ORDER BY skill
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]


def consolidate(
    conn: sqlite3.Connection,
    *,
    embed_fn,
    cluster_fn: ClusterFn,
    observation_synth_fn: ObservationSynthFn,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    agent_id: str = "default",
    max_observations: int = MAX_OBSERVATIONS_PER_RUN,
    min_cluster_size: int = MIN_CLUSTER_SIZE_FOR_OBSERVATION,
) -> ConsolidateResult:
    """Run a global cross-session consolidation pass.

    Designed for cron invocation (weekly default). Idempotent in the
    sense that re-running on the same DB without new activity is a
    no-op — atoms already covered by observations are skipped, and
    clusters that didn't reach the threshold last time are unchanged.

    LLM cost: bounded by ``max_observations``. A pass over 1000 raws
    might produce 10-20 cluster candidates above threshold; we emit
    at most ``max_observations``.
    """
    result = ConsolidateResult()
    raws = _candidate_raws(
        conn, lookback_days=lookback_days, agent_id=agent_id,
    )
    result.candidates_scanned = len(raws)
    if len(raws) < min_cluster_size:
        return result

    # Cluster — read-only operation, no transaction needed.
    clusters = cluster_fn(raws)
    result.clusters_formed = len(clusters)

    for cluster in clusters:
        if len(result.observations_emitted) >= max_observations:
            break
        if len(cluster) < min_cluster_size:
            continue

        evidence_ids = [a["id"] for a in cluster]

        # Pre-check: an observation with exactly this evidence set
        # already exists? Skip synthesis (no LLM cost) and continue.
        # No access_event fired: consolidation is system-internal; the
        # ``consolidated_into`` / ``evidenced_by`` relations remain the
        # audit trail. access_events is reserved for external access.
        existing_equal = find_equal_evidence_obs(conn, set(evidence_ids))
        if existing_equal:
            continue

        # LLM call OUTSIDE transaction.
        content, topics = observation_synth_fn(cluster)
        if not content or not content.strip():
            continue

        # store() opens its own transaction.
        store_result = store(
            conn, content,
            embed_fn=embed_fn,
            memory_type="observation",
            stream="semantic",
            topics=topics,
            agent_id=agent_id,
            # session_id intentionally None — this observation is
            # cross-session by construction. The atom is created
            # outside any session's scope.
            session_id=None,
        )
        if not store_result.stored:
            # Content-hash dedupe hit on the observation. Relations
            # were already in place from the prior cluster pass; no
            # access_event fired (consolidation stays out of activation).
            continue

        observation_id = store_result.atom_id
        now = _utc_now_iso()

        # One transaction for the observation's relations + access
        # events + metadata.
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO atom_relations "
                "(source_id, target_id, relation_type, confidence, created_at) "
                "VALUES (?, ?, 'evidenced_by', 1.0, ?)",
                [(observation_id, raw_id, now) for raw_id in evidence_ids],
            )
            conn.executemany(
                "INSERT INTO atom_relations "
                "(source_id, target_id, relation_type, confidence, created_at) "
                "VALUES (?, ?, 'consolidated_into', 1.0, ?)",
                [(raw_id, observation_id, now) for raw_id in evidence_ids],
            )
            # No mark_access on evidence raws: consolidation is
            # system-internal. The evidence_boost on retrieval is the
            # only ranking signal consolidation produces; activation
            # stays a pure external-access record.

            superseded = find_superseded_observations(
                conn, observation_id, set(evidence_ids),
            )
            for old_obs_id in superseded:
                conn.execute(
                    "INSERT OR IGNORE INTO atom_relations "
                    "(source_id, target_id, relation_type, confidence, "
                    "created_at, metadata) "
                    "VALUES (?, ?, 'supersedes', 1.0, ?, ?)",
                    (observation_id, old_obs_id, now,
                     json.dumps({"trigger": "consolidate"})),
                )

            conn.execute(
                "INSERT INTO observations_metadata "
                "(atom_id, evidence_count, trend, last_evidence_at, "
                "consolidated_at) VALUES (?, ?, ?, ?, ?)",
                (observation_id, len(evidence_ids),
                 "strengthening", now, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Trend recompute in its own short txn.
        refresh_trend(conn, observation_id)

        result.observations_emitted.append(observation_id)
        for old_id in superseded:
            result.observations_superseded.append((observation_id, old_id))

    return result
