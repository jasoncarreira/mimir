"""mimir.memory — agent memory subsystem.

Public API. Internal helpers (``_session_atoms``, ``_make_atom_id``,
etc.) stay module-private; mimir's call sites use only the names
exported here.

Operation surface (matches the architecture-doc table):

| Op | Where | Trigger |
|---|---|---|
| ``store`` | store.py | Atom creation |
| ``recall`` | recall.py | Per-turn retrieval |
| ``mark_access`` | mark_access.py | Explicit access logging |
| ``feedback`` | (alias) | Agent endorsement → feedback_positive event |
| ``reflect`` | reflect.py | Session-end synthesis (boundary + within-session observations) |
| ``recent_session_boundaries`` | reflect.py | Cross-session continuity rendering |
| ``consolidate`` | consolidate.py | Periodic cross-session consolidation pass |
| ``forget`` | forget.py | Explicit tombstoning |
| ``forget_by_criteria`` | forget.py | Bulk criteria-based cleanup |
| ``refresh_trend`` | observations.py | Recompute trend label on an observation |

Imports are lazy where the inline-constants vs config.py refactor is
incomplete (cluster.make_default_cluster_fn currently reads its own
threshold rather than going through config). Tracked in NEXT.md.
"""

from __future__ import annotations

# Top-level operations
from .store import store, StoreResult
from .recall import recall, RecallResult, RecallCandidate
from .mark_access import mark_access, AccessEvent
from .reflect import (
    reflect, recent_session_boundaries, ReflectResult,
)
from .consolidate import ConsolidateResult
# Note: ``consolidate.consolidate()`` is intentionally NOT re-exported.
# Production callers should use ``MemoryClient.consolidate()`` for the
# tier-3 (triples + contradictions + P47/P48) path. The standalone
# ``consolidate()`` is the tier-2 helper kept for the
# test_memory_tier2b regression suite; tests that need it import via
# ``from mimir.memory.consolidate import consolidate`` directly.
from .forget import forget, forget_by_criteria, ForgetResult

# Observation utilities
from .observations import (
    classify_trend, refresh_trend, TrendResult,
    find_equal_evidence_obs, find_superseded_observations,
)

# Clustering
from .cluster import (
    cluster_by_similarity, make_default_cluster_fn,
)

# Activation (mostly internal but useful for tests / introspection)
from .activation import (
    compute_activation, activation_from_events, activation_exact,
    SOURCE_WEIGHTS, DEFAULT_STREAM_THRESHOLDS,
)

# Search adapters
from .fts import fts_search, fts5_query
from .vector_index import VectorIndex, FAISS_AVAILABLE

# LLM-backed synthesis (lazy: import-time pulls in saga._llm)
from .synthesize import (
    make_observation_synth_fn, make_async_observation_synth_fn,
    make_async_rich_synth_fn,
    make_boundary_synth_fn, make_async_boundary_synth_fn,
)

# Triples + temporal world model (P42, P37)
from .triples import (
    make_triple_id, parse_triples, store_triples,
    triple_augment_search, retrieve_by_entity,
    get_current_value, get_history, WorldFact,
    detect_contradictions, resolve_contradictions_to_supersedes,
)

# Credit-pass + contextual query rewrite
from .contributions import (
    mark_contributions as mark_contributions_fn,
    ContributionResult,
    DEFAULT_CONTRIBUTION_THRESHOLD,
)
from .query_rewrite import rewrite_query

# Config
from .config import (
    MemoryConfig, ActivationConfig, ThresholdConfig,
    ScoringWeights, TrendModifiers, BoostsAndPenalties,
    SourceWeights, TrendConfig, ConsolidationConfig,
    DEFAULT as DEFAULT_CONFIG,
)


def feedback(
    conn,
    atom_ids,
    *,
    signal: str = "positive",
    session_id: str | None = None,
) -> int:
    """Convenience wrapper over mark_access for the agent's
    ``mark_contributions`` semantic.

    Maps the saga-era 'positive'/'negative'/'neutral' signal to the
    new access_events source vocabulary:

    - positive → ``feedback_positive`` event (weight 2.0)
    - negative → NO event; flag the atom for explicit forget() review.
      Caller can read the response to see the candidate set.
    - neutral → no signal worth recording

    Returns the number of events written (0 for non-positive signals).

    The negative-feedback handling is deliberately weak: ACT-R has no
    notion of negative activation contribution; reducing activation by
    appending negative-weight events would break the OL aggregate's
    monotonicity. Negative feedback instead surfaces atoms to the
    operator for explicit forget(); see SCORING.md.
    """
    if signal != "positive" or not atom_ids:
        return 0
    events = [
        AccessEvent(atom_id=aid, source="feedback_positive",
                    session_id=session_id)
        for aid in atom_ids
    ]
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = mark_access(conn, events)
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


__all__ = [
    # Operations
    "store", "StoreResult",
    "recall", "RecallResult", "RecallCandidate",
    "mark_access", "AccessEvent",
    "feedback",
    "reflect", "recent_session_boundaries", "ReflectResult",
    "ConsolidateResult",
    "forget", "forget_by_criteria", "ForgetResult",
    # Observation utilities
    "classify_trend", "refresh_trend", "TrendResult",
    "find_equal_evidence_obs", "find_superseded_observations",
    # Clustering
    "cluster_by_similarity", "make_default_cluster_fn",
    # Activation (introspection / test surface)
    "compute_activation", "activation_from_events", "activation_exact",
    "SOURCE_WEIGHTS", "DEFAULT_STREAM_THRESHOLDS",
    # Search adapters
    "fts_search", "fts5_query", "VectorIndex", "FAISS_AVAILABLE",
    # Synthesis
    "make_observation_synth_fn", "make_async_observation_synth_fn",
    "make_boundary_synth_fn", "make_async_boundary_synth_fn",
    # Config
    "MemoryConfig", "ActivationConfig", "ThresholdConfig",
    "ScoringWeights", "TrendModifiers", "BoostsAndPenalties",
    "SourceWeights", "TrendConfig", "ConsolidationConfig",
    "DEFAULT_CONFIG",
]
