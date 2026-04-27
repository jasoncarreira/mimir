"""
MSAM Config -- Central configuration loader.

Config search order:
  1. $MSAM_CONFIG (explicit path to msam.toml)
  2. $MSAM_DATA_DIR/msam.toml (explicit data directory)
  3. ~/.msam/msam.toml (user-level config)
  4. <package_dir>/msam.toml (legacy, in-place development)

Data directory (for DB, caches) uses:
  1. $MSAM_DATA_DIR (explicit)
  2. ~/.msam/ (default)

Singleton: loads once on first import.

Usage:
    from msam.config import get_config
    cfg = get_config()
    value = cfg('section', 'key', default=fallback)
    # or:
    value = cfg('section', 'key')  # raises KeyError if not found
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Python 3.11+ has tomllib in stdlib
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # fallback: pip install tomli
    except ImportError:
        tomllib = None

# ─── Hardcoded Defaults (current production values) ──────────────
# These mirror msam.toml exactly. Update both if you change a dial.

_DEFAULTS = {
    "embedding": {
        "provider": "nvidia-nim",
        "url": "https://integrate.api.nvidia.com/v1/embeddings",
        "model": "nvidia/nv-embedqa-e5-v5",
        "dimensions": 1024,
        "max_input_chars": 2000,
        "timeout_seconds": 10,
        "api_key": None,
        "api_key_env": "OPENAI_API_KEY",
        "batch_size": 50,
    },
    "storage": {
        "db_path": "msam.db",
        "metrics_db_path": "msam_metrics.db",
        "token_budget_ceiling": 40000,
        "auto_compact_threshold_pct": 85,
        "db_busy_timeout_ms": 5000,
        "refuse_threshold_pct": 95,
    },
    "retrieval": {
        "default_top_k": 12,
        "semantic_weight": 0.7,
        # P4-bench: supersedes demotion. When enabled, atoms that have been
        # marked as superseded by another atom in the candidate pool get a
        # multiplicative score penalty in hybrid_retrieve.
        "enable_supersedes_demotion": True,
        "supersedes_score_multiplier": 0.4,
        # Confidence filtering on the REST two-tier /v1/query path. When
        # enabled (the default) atoms are filtered per-atom by their own
        # _confidence_tier. In-process callers (benchmarks) bypass this —
        # filtering happens in api_query, not _two_tier_split.
        "enable_confidence_gating": True,
        # Default per-atom floor used when a request omits min_confidence_tier.
        # "low" drops only atoms classified "none" (sim < confidence_sim_low).
        "default_min_confidence_tier": "low",
        "similarity_threshold": 0.2,
        "sigmoid_midpoint": 0.35,
        "sigmoid_steepness": 15.0,
        "base_activation_cap": 3.0,
        "quality_threshold": 2.0,
        "context_quality_floor": 0.15,
        "mmr_lambda": 0.7,
        "keyword_top_k": 10,
        "spreading_activation_enabled": True,
        "max_spread_hops": 2,
        "spread_decay_factor": 0.3,
        "confidence_sim_high": 0.45,
        "confidence_sim_medium": 0.30,
        "confidence_sim_low": 0.15,
        "confidence_score_high": 40.0,
        "confidence_score_medium": 10.0,
        "temporal_recency_hours": 24,
        # Felt Consequence (outcome attribution)
        "outcome_weight": 0.15,
        "outcome_decay": 0.95,
        "min_outcomes_for_effect": 3,
    },
    "decay": {
        # Atom-level supersedes batch resolution. Off by default — see the
        # comment on [atoms] auto_resolve_supersedes_on_write. The function
        # resolve_contradictions_to_supersedes() remains callable manually
        # for callers who want it; nothing in the main loop fires it.
        "auto_resolve_supersedes": False,
        "supersedes_resolution_threshold": 0.85,
        "active_to_fading_threshold": 0.3,
        "fading_to_dormant_threshold": 0.1,
        "confidence_decay_rate": 0.01,
        "confidence_decay_grace_days": 7,
        "confidence_floor": 0.1,
        "stability_dampen_factor": 0.9,
        "stability_boost_factor": 1.1,
        "max_stability": 10.0,
        "intentional_forgetting_enabled": False,
        "intentional_forgetting_mode": "flag",  # "flag" | "auto"
        "forgetting_contribution_threshold": 0.15,
        "forgetting_min_retrievals": 5,
        "forgetting_contradiction_threshold": 0.85,
        "forgetting_confidence_floor": 0.1,
        "forgetting_grace_days": 14,
        "protection_days": 7,
        "compaction_full_min_age_days": 7,
        "compaction_full_max_access": 3,
        "compaction_standard_min_age_days": 14,
        "compaction_standard_max_access": 2,
        "compaction_trigger_ratio": 1.5,
        "profile_target_lightweight_chars": 90,
        "profile_target_standard_chars": 240,
    },
    "atoms": {
        "default_profile": "standard",
        "default_encoding_confidence": 0.7,
        "default_arousal": 0.5,
        "default_valence": 0.0,
        "profile_lightweight_max_words": 20,
        "profile_full_min_words": 80,
        # Atom-level supersedes resolution at write time. Off by default —
        # the LongMemEval P4-bench experiment (commit bb4b6c8 result)
        # showed this regressed temporal-reasoning -6.7pp because demoting
        # superseded raw atoms breaks queries about historical state
        # ("where did Alex work in May?"). Observation-level supersedes
        # (consolidation writing edges between observations) is still on
        # and applied during retrieval — that's a different story.
        "auto_resolve_supersedes_on_write": False,
        "supersedes_resolution_threshold": 0.85,
    },
    "merge": {
        "similarity_threshold": 0.85,
        "max_candidates": 20,
    },
    "vector_index": {
        "approx_threshold": 50000,
    },
    "consolidation": {
        "similarity_threshold": 0.80,
        "min_cluster_size": 3,
        "max_clusters_per_run": 50,
        "stability_reduction_factor": 0.5,
    },
    "negative_knowledge": {
        "default_ttl_hours": 168,
    },
    "annotation": {
        "llm_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "llm_model": "mistralai/mistral-large-3-675b-instruct-2512",
        "timeout_seconds": 15,
    },
    "emotional_context": {
        "urgency_recency_bonus": 1.0,
        "negative_valence_support_bonus": 0.5,
        "low_arousal_depth_bonus": 0.5,
        "high_arousal_recent_bonus": 0.3,
    },
    "relations": {
        "supersedes_demotion": 2.0,
        "supports_bonus": 0.5,
    },
    "entity_resolution": {
        "aliases": {
            "user_nick": "user",
            
            "agent_nick": "agent",
            
            
        },
    },
    "query_expansion": {
        "synonyms": {
            # Identity / role
            "profession": ["job", "career", "work", "occupation", "employment"],
            "job":        ["profession", "career", "work", "occupation"],
            "company":    ["employer", "office", "workplace", "firm"],

            # People
            "spouse":     ["partner", "wife", "husband", "married"],
            "partner":    ["spouse", "wife", "husband", "boyfriend", "girlfriend"],
            "family":     ["parents", "siblings", "relatives"],

            # Where
            "location":   ["city", "town", "address", "where"],
            "home":       ["hometown", "residence", "where lives", "based"],

            # When
            "birthday":   ["birth", "born", "age"],
            "schedule":   ["routine", "calendar", "plan", "timetable"],

            # Activity / preference
            "favorite":   ["preferred", "like", "love", "enjoy"],
            "prefer":     ["favorite", "like", "favourite"],
            "purchase":   ["buy", "bought", "ordered"],
            "buy":        ["purchase", "bought", "ordered"],
            "own":        ["have", "possess", "got"],

            # Domains
            "food":       ["meal", "dish", "cuisine", "eat", "eating"],
            "drink":      ["beverage", "drinking"],
            "movie":      ["film", "watched", "watching"],
            "book":       ["novel", "read", "reading"],
            "music":      ["songs", "playlist", "listening"],
            "show":       ["performance", "tour", "concert"],
            "anime":      ["manga", "japanese animation"],
            "pet":        ["dog", "cat", "animal"],
            "travel":     ["trip", "vacation", "visit", "journey"],
            "exercise":   ["workout", "gym", "fitness", "training"],

            # Communication verbs (probe phrasing vs. haystack statements)
            "told":       ["said", "mentioned", "discussed", "talked"],
            "discussed":  ["talked", "mentioned", "covered"],

            # Emotion / state
            "feelings":   ["emotions", "mood", "emotional state"],

            # Meta
            "memory":     ["remember", "recall", "memories", "msam"],
        },
    },
    "retrieval_v2": {
        "enabled": True,
        # Beam search control:
        # "auto" = dynamic gate based on atom count (default, recommended)
        # True   = always on (manual override)
        # False  = always off (manual override)
        "enable_beam_search": "auto",
        "beam_search_atom_threshold": 10000,  # dynamic gate: beam search activates above this
        "beam_width": 3,  # number of retrieval beams when active
        "enable_rewrite": True,
        "enable_query_expansion": True,
        "enable_triple_augment": True,
        "enable_entity_roles": True,
        "enable_quality_filter": True,
        "enable_temporal": True,
        "enable_rerank": False,  # LLM rerank off by default (latency)
        "enable_feedback": True,
        "max_expansion_terms": 5,
        "rerank_model": "mistralai/mistral-large-3-675b-instruct-2512",
        "entity_mappings": None,
    },
    "predictive_retrieval": {
        "user_active": True,
    },
    "prediction": {
        "temporal_weight": 0.4,
        "coretrieval_weight": 0.4,
        "momentum_weight": 0.2,
        "lookback_days": 30,
        "min_confidence": 0.3,
        # Predictive Context Assembly
        "enabled": True,
        "temporal_window_hours": 2,
        "min_pattern_count": 5,
        "co_retrieval_threshold": 3,
        "max_predicted_atoms": 8,
        "warmup_sessions": 50,
    },
    "context": {
        "default_token_budget": 500,
        "default_top_k": 10,
        "probe_token_budget": 200,
        "probe_top_k": 5,
        "startup_identity_query": "agent identity core traits personality",
        "startup_user_query": "user preferences relationship current situation",
        "startup_recent_query": "what happened today recent activity",
        "startup_emotional_query": "emotional state mood current feeling",
        "probe_queries": ["user current situation schedule", "agent identity personality traits"],
        "probe_atom_queries": ["What is the user's profession?", "Who is the agent?"],
        "emotional_state_file": "memory/context/emotional-state.md",
    },
    "agents": {
        "default_agent_id": "default",
        "enable_sharing": True,
    },
    "compression": {
        "enable_subatom": True,
        "enable_fact_dedup": True,
        "enable_synthesis": False,
        "subatom_token_budget": 120,
        "subatom_section_budget": 30,
        "sentence_similarity_threshold": 0.25,
        "dedup_similarity_threshold": 0.85,
        "synthesis_max_tokens": 30,
        "synthesis_model": "mistralai/mistral-large-3-675b-instruct-2512",
    },
    "comparison": {
        "startup_files": [],
        "query_files": [],
    },
    "triples": {
        "llm_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "llm_model": "mistralai/mistral-large-3-675b-instruct-2512",
    },
    "api": {
        "port": 3001,
        "host": "127.0.0.1",
        "allowed_origins": ["http://127.0.0.1:3000", "http://localhost:3000"],
        "api_key": None,
    },
    "metrics": {
        "enabled": True,
        "log_access_events": True,
        "log_emotional_state": True,
        "hybrid_probe_on_snapshot": True,
        "default_emotional_intensity": 0.5,
        "default_emotional_warmth": 0.5,
        "continuity_history_limit": 100,
        "retrieval_history_limit": 100,
    },
    "world_model": {
        "enabled": True,
        "auto_close_on_conflict": True,
        "temporal_extraction": True,
        "default_confidence": 1.0,
    },
    "sycophancy": {
        "tracking_enabled": True,
        "warning_threshold": 0.85,
        "window_size": 20,
    },
}


# ─── Singleton State ──────────────────────────────────────────────

_config = None
_config_loaded = False


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursively for nested dicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def get_data_dir() -> Path:
    """Return the MSAM data directory, creating it if needed.

    Resolution order:
      1. $MSAM_DATA_DIR (explicit override)
      2. ~/.msam/ (default user-level directory)
    """
    env = os.environ.get("MSAM_DATA_DIR")
    if env:
        data_dir = Path(env)
    else:
        data_dir = Path.home() / ".msam"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _find_toml() -> Optional[Path]:
    """Locate msam.toml using the search order.

    1. $MSAM_CONFIG (explicit path)
    2. $MSAM_DATA_DIR/msam.toml
    3. ~/.msam/msam.toml
    4. <package_dir>/msam.toml (legacy / development)
    """
    # 1. Explicit config path
    env_config = os.environ.get("MSAM_CONFIG")
    if env_config:
        p = Path(env_config)
        if p.exists():
            return p

    # 2. Explicit data dir
    env_data = os.environ.get("MSAM_DATA_DIR")
    if env_data:
        p = Path(env_data) / "msam.toml"
        if p.exists():
            return p

    # 3. User-level config
    p = Path.home() / ".msam" / "msam.toml"
    if p.exists():
        return p

    # 4. Legacy in-package config (development mode)
    p = Path(__file__).parent / "msam.toml"
    if p.exists():
        return p

    return None


def _load_config() -> dict:
    """Load configuration from msam.toml, falling back to defaults."""
    global _config, _config_loaded

    if _config_loaded:
        return _config

    config = dict(_DEFAULTS)

    toml_path = _find_toml()

    if toml_path is None:
        import logging
        logging.getLogger("msam.config").info(
            "No msam.toml found. Searched: $MSAM_CONFIG, $MSAM_DATA_DIR, "
            "~/.msam/msam.toml, <package>/msam.toml. Using defaults. "
            "Copy msam.example.toml to ~/.msam/msam.toml to configure."
        )

    if toml_path is not None and tomllib is not None:
        try:
            with open(toml_path, "rb") as f:
                toml_data = tomllib.load(f)
            config = _deep_merge(config, toml_data)
        except Exception as e:
            import logging
            logging.getLogger("msam.config").warning(
                f"Failed to load {toml_path}: {e}. Using defaults."
            )
    elif toml_path is not None and tomllib is None:
        import logging
        logging.getLogger("msam.config").warning(
            "msam.toml found but tomllib unavailable (Python < 3.11 and no tomli installed). "
            "Using defaults."
        )

    _config = config
    _config_loaded = True

    # Surface user-config typos: keys in known sections that don't match
    # any known key. Catches things like Mimir's `cluster_similarity_threshold`
    # (real key: `similarity_threshold`) silently falling through to defaults.
    if toml_path is not None and tomllib is not None:
        try:
            with open(toml_path, "rb") as f:
                user_data = tomllib.load(f)
            _warn_unknown_keys(user_data)
        except Exception:
            pass

    return _config


# ─── Unknown-key detection ─────────────────────────────────────────
#
# Some legitimate keys aren't in `_DEFAULTS` because the code calls
# `_cfg(section, key, default)` and only relies on the runtime default.
# The registry below lists those so we don't false-warn on them.
# When you add a new feature flag that's read with a runtime default,
# add it here (or to `_DEFAULTS`).
_KNOWN_EXTRA_KEYS: dict[str, set[str]] = {
    "retrieval": {
        "fusion", "rrf_k",
        "rrf_semantic_weight", "rrf_keyword_weight",
        "rrf_graph_weight", "rrf_temporal_weight",
        "two_tier_enabled", "observations_top_k",
        "observation_confidence_min_sim", "evidence_boost_cap_multiplier",
        "enable_observation_bonus", "observation_bonus_alpha",
        "trend_penalty_weakening", "trend_penalty_stale",
        "enable_graph_pathway", "graph_pathway_top_k",
        "enable_temporal_pathway", "temporal_pathway_top_k",
        "min_outcomes_for_effect",
    },
    "retrieval_v2": {
        # P11/P12/P13 cherry-picks. Read with runtime default in core.py.
        "enable_query_rewriting",
    },
    "consolidation": {
        "enabled", "enable_llm", "llm_url", "llm_model",
        "timeout_seconds", "api_key_env",
    },
    "embedding": {
        "max_chars", "dimensions",
    },
    "triples": {
        "enable_extraction", "llm_url", "llm_model", "api_key_env",
    },
    "annotation": {
        "use_llm", "llm_url", "llm_model", "api_key_env",
    },
    "decay": {
        "enabled",
    },
    "world_model": {
        "auto_close_on_conflict", "temporal_extraction", "default_confidence",
    },
    "agents": {
        "enable_sharing",
    },
    "atoms": set(),
    "api": {
        "host", "port", "allowed_origins", "api_key",
    },
}


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings (no deps)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dlt = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(ins, dlt, sub))
        prev = curr
    return prev[-1]


def _suggest_key(typo: str, known: set[str]) -> Optional[str]:
    """Return the closest known key by Levenshtein distance, or None.

    Threshold scales with the longer string's length so suffix omissions
    on long keys (e.g. `stability_reduction` for `stability_reduction_factor`,
    distance 7) still get suggested. Bounded between 4 and 8 to avoid
    nonsense matches on tiny strings or runaway matches on very long ones.
    """
    if not known:
        return None
    # Sort by (distance, key) so ties resolve deterministically — sets
    # are unordered and `min` over a set is non-deterministic across
    # Python invocations.
    best = sorted(known, key=lambda k: (_levenshtein(typo, k), k))[0]
    distance = _levenshtein(typo, best)
    longer = max(len(typo), len(best))
    max_distance = max(4, min(8, longer * 30 // 100))
    return best if distance <= max_distance else None


def _warn_unknown_keys(user_data: dict) -> None:
    """Warn for user-supplied keys that don't match any known key in
    sections that exist in `_DEFAULTS`. Sections not in `_DEFAULTS` are
    skipped (legitimate add-on features may live there).

    Suppress with environment variable `MSAM_QUIET_CONFIG=1`.
    """
    if os.environ.get("MSAM_QUIET_CONFIG"):
        return
    import logging
    log = logging.getLogger("msam.config")
    for section, kvs in user_data.items():
        if not isinstance(kvs, dict):
            continue
        if section not in _DEFAULTS:
            continue
        known = set(_DEFAULTS[section].keys()) | _KNOWN_EXTRA_KEYS.get(section, set())
        for key, value in kvs.items():
            # Nested tables (e.g. [retrieval_v2.entity_mappings]) appear
            # here as dicts. The top-level table key is what we check;
            # contents of nested tables are user-defined and not validated.
            if isinstance(value, dict):
                continue
            if key in known:
                continue
            suggestion = _suggest_key(key, known)
            if suggestion:
                log.warning(
                    f"Unknown config key [{section}] {key!r} "
                    f"— did you mean {suggestion!r}? "
                    f"(falling through to default)"
                )
            else:
                log.warning(
                    f"Unknown config key [{section}] {key!r} "
                    f"— falling through to default"
                )


_SENTINEL = object()


def get_config():
    """Return the config accessor function.

    The returned callable accepts (section, key, default=_SENTINEL):
        cfg = get_config()
        val = cfg('embedding', 'url')              # raises if missing
        val = cfg('embedding', 'url', 'fallback')  # returns fallback if missing

    For nested sections (entity_resolution.aliases, etc.) use:
        aliases = cfg('entity_resolution', 'aliases', {})

    The accessor reads the current live config each call, so reload_config()
    takes effect for every _cfg reference captured at import time.
    """
    _load_config()

    def accessor(section, key, default=_SENTINEL):
        current = _config if _config is not None else _DEFAULTS
        sec = current.get(section, {})
        if key in sec:
            return sec[key]
        if default is not _SENTINEL:
            return default
        raise KeyError(f"Config key not found: [{section}] {key}")

    return accessor


def reload_config():
    """Force reload config from disk (useful for testing)."""
    global _config, _config_loaded
    _config = None
    _config_loaded = False
    return get_config()


def get_raw_config() -> dict:
    """Return the full config dict (for debugging)."""
    return _load_config()


# ─── Convenience: direct attribute access ────────────────────────

if __name__ == "__main__":
    import json
    cfg = get_config()
    raw = get_raw_config()
    print(json.dumps(raw, indent=2, default=str))
