"""
MSAM -- Multi-Stream Adaptive Memory

Cognitive memory system for AI agents. Stores knowledge as atoms across
semantic, episodic, and procedural streams with ACT-R-inspired activation
scoring, hybrid retrieval (atoms + knowledge graph triples), and adaptive
decay with contribution-based feedback. Cross-turn conversation state is
the agent's responsibility (e.g. message history in the LLM context),
not MSAM's.

Usage:
    from saga import store_atom, retrieve, hybrid_retrieve

    store_atom("User prefers dark mode")       # store an atom
    results = retrieve("preferences")          # semantic + keyword retrieval
    stats = get_stats()                        # database statistics
"""

__version__ = "2026.02.24"

from saga.core import (
    store_atom,
    retrieve,
    hybrid_retrieve,
    batch_cosine_similarity,
    get_stats,
    metamemory_query,
    dry_retrieve,
    retrieve_with_rewrite,
    retrieve_with_emotion,
    retrieve_diverse,
    score_context_quality,
    find_merge_candidates,
    merge_atoms,
    estimate_importance,
    detect_knowledge_gaps,
    predict_needed_atoms,
    store_session_boundary,
    get_associations,
    emotional_drift,
    episodic_replay,
    record_outcome,
    get_outcome_history,
)

from saga.triples import (
    extract_and_store as extract_triples,
    hybrid_retrieve_with_triples,
    retrieve_triples,
    graph_traverse,
    graph_path,
    detect_contradictions,
    get_triple_stats,
    query_world,
    update_world,
    world_history,
)

from saga.decay import run_decay_cycle
from saga.config import get_config, reload_config
from saga.annotate import smart_annotate, llm_annotate
from saga.contradictions import find_semantic_contradictions, check_before_store
from saga.prediction import PredictiveEngine
from saga.agents import register_agent, list_agents, share_atom, agent_stats
from saga.forgetting import identify_forgetting_candidates
from saga.calibration import calibrate, re_embed
from saga.prediction import track_temporal_pattern, track_co_retrievals

__all__ = [
    "store_atom",
    "retrieve",
    "hybrid_retrieve",
    "batch_cosine_similarity",
    "get_stats",
    "metamemory_query",
    "dry_retrieve",
    "retrieve_with_rewrite",
    "retrieve_with_emotion",
    "retrieve_diverse",
    "score_context_quality",
    "find_merge_candidates",
    "merge_atoms",
    "estimate_importance",
    "detect_knowledge_gaps",
    "predict_needed_atoms",
    "store_session_boundary",
    "get_associations",
    "emotional_drift",
    "episodic_replay",
    "extract_triples",
    "hybrid_retrieve_with_triples",
    "retrieve_triples",
    "graph_traverse",
    "graph_path",
    "detect_contradictions",
    "get_triple_stats",
    "run_decay_cycle",
    "get_config",
    "reload_config",
    "smart_annotate",
    "llm_annotate",
    "find_semantic_contradictions",
    "check_before_store",
    "PredictiveEngine",
    "register_agent",
    "list_agents",
    "share_atom",
    "agent_stats",
    "identify_forgetting_candidates",
    "calibrate",
    "re_embed",
    "record_outcome",
    "get_outcome_history",
    "query_world",
    "update_world",
    "world_history",
    "track_temporal_pattern",
    "track_co_retrievals",
]
