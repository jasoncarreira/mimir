"""Central tunable registry.

Single source of truth for every parameter the memory subsystem
exposes. Each module that currently has inline constants will be
refactored to import from here during the mimir/memory/ integration
pass. For the sketch phase the inline constants still exist; this
module documents them and provides the override mechanism.

Load order:
  1. Defaults (this module)
  2. TOML overrides (when wired into mimir, via saga.toml — same
     mechanism mimir uses today for the [embedding] / [retrieval]
     blocks)
  3. Per-call overrides (e.g. ``recall(threshold=...)``)

The TOML loader is sketched as ``load_overrides()`` but not wired —
plumb it into mimir's existing config-loading path during integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ───────────────────────────────────────────────────────────────────
# Activation (ACT-R base-level via Petrov OL)
# ───────────────────────────────────────────────────────────────────

@dataclass
class ActivationConfig:
    """Parameters for compute_activation."""
    #: ACT-R decay parameter. Anderson's standard value across models.
    #: Higher = faster forgetting. Range [0.0, 1.0).
    decay_d: float = 0.5
    #: How many recent events to track exactly per atom (Petrov OL).
    #: Trades read-path cost (O(K) per activation) against approximation
    #: quality. K=10 captures the transient post-access boost cleanly.
    recent_k: int = 10
    #: Floor on (now - t_j) to avoid the singularity at t_j → now.
    epsilon_seconds: float = 1.0


# ───────────────────────────────────────────────────────────────────
# Retrieval thresholds + scoring weights
# ───────────────────────────────────────────────────────────────────

@dataclass
class ThresholdConfig:
    """Per-stream activation thresholds for retrieval gating.

    Atoms whose activation B_i falls below their stream's threshold
    are dropped from candidates. Calibrate against LongMemEval-S
    during bench iteration."""
    semantic: float = -1.5
    episodic: float = -2.5         # episodic decays fast; accept lower B
    procedural: float = -1.0       # procedural is sticky; demand higher B
    fallback: float = -1.5         # for streams not in the dict above

    def get(self, stream: str) -> float:
        return {
            "semantic": self.semantic,
            "episodic": self.episodic,
            "procedural": self.procedural,
        }.get(stream, self.fallback)


@dataclass
class ScoringWeights:
    """Linear weights for the combined score formula. See SCORING.md."""
    w_sim: float = 0.7           # primary signal — semantic match
    w_kw: float = 0.2            # keyword/BM25 contribution
    w_topic: float = 0.1         # topic-filter agreement
    w_act: float = 0.3           # activation tiebreaker (sigmoid'd above threshold)


@dataclass
class TrendModifiers:
    """Score adjustments per observation trend label.

    Stale gets the harshest penalty: the agent should aggressively
    downrank beliefs that haven't been validated by recent activity."""
    strengthening: float = +0.10
    stable: float = 0.0
    weakening: float = -0.10
    stale: float = -0.25

    def get(self, trend: str | None) -> float:
        if trend is None:
            return 0.0
        return getattr(self, trend, 0.0)


@dataclass
class BoostsAndPenalties:
    """Fixed adjustments applied in the scoring pass."""
    evidence_boost: float = 0.20       # observation surfaces → boost its evidence raws
    session_boost: float = 0.15        # atom accessed in current session
    pinned_boost: float = 0.25         # atom marked is_pinned
    supersession_penalty: float = 0.15  # superseder is in the candidate set
    contradiction_penalty: float = 0.10


# ───────────────────────────────────────────────────────────────────
# Source weights for access events
# ───────────────────────────────────────────────────────────────────

@dataclass
class SourceWeights:
    """Per-source-tag weight for access events. Higher weight = stronger
    contribution to activation. See SCORING.md table."""
    retrieval: float = 1.0
    feedback_positive: float = 2.0     # explicit endorsement double-weighted
    store: float = 1.0
    consolidation: float = 0.5         # derivative use, half-weight
    pinned_init: float = 5.0           # one-time heavy event at pin time

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


# ───────────────────────────────────────────────────────────────────
# Trend classification windows + thresholds
# ───────────────────────────────────────────────────────────────────

@dataclass
class TrendConfig:
    """Windows + ratios for classify_trend()."""
    stale_threshold_days: int = 30
    recent_window_days: int = 7
    historical_window_days: int = 30
    strengthening_ratio: float = 1.5   # recent/historical above → strengthening
    weakening_ratio: float = 0.5       # below → weakening


# ───────────────────────────────────────────────────────────────────
# Consolidation (reflect + consolidate share these)
# ───────────────────────────────────────────────────────────────────

@dataclass
class ConsolidationConfig:
    """Thresholds for reflect's within-session synthesis and
    consolidate's cross-session pass."""
    #: Cosine threshold for greedy agglomerative clustering. Calibrate
    #: per embedding provider — voyage's 1024d produces tighter
    #: distributions than openai-1536d, so a higher threshold may be
    #: appropriate.
    similarity_threshold: float = 0.6
    #: Cluster must hit this minimum size to justify synthesis.
    min_cluster_size: int = 3
    #: reflect-specific gate: session must have at least this many
    #: distinct atoms before any observation synthesis runs.
    min_session_events_for_observations: int = 5
    #: reflect-specific cap on observations emitted per session.
    max_observations_per_session: int = 3
    #: consolidate-specific window for the candidate pool.
    consolidate_lookback_days: int = 30
    #: consolidate-specific cap per run.
    max_observations_per_consolidate_run: int = 20


# ───────────────────────────────────────────────────────────────────
# Top-level
# ───────────────────────────────────────────────────────────────────

@dataclass
class MemoryConfig:
    """Aggregate config object passed around. mimir's bootstrap
    constructs one from saga.toml and threads it through."""
    activation: ActivationConfig = field(default_factory=ActivationConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    scoring_weights: ScoringWeights = field(default_factory=ScoringWeights)
    trend_modifiers: TrendModifiers = field(default_factory=TrendModifiers)
    boosts: BoostsAndPenalties = field(default_factory=BoostsAndPenalties)
    source_weights: SourceWeights = field(default_factory=SourceWeights)
    trend: TrendConfig = field(default_factory=TrendConfig)
    consolidation: ConsolidationConfig = field(default_factory=ConsolidationConfig)

    @classmethod
    def from_toml_dict(cls, data: dict[str, Any]) -> "MemoryConfig":
        """Build a MemoryConfig from a parsed saga.toml-style dict.

        Missing sections fall back to defaults. Unknown keys within a
        section emit a warning (matching saga's _warn_unknown_keys
        pattern) but don't crash — operator typos surface in logs
        without blocking startup.
        """
        cfg = cls()
        # Each section maps to a top-level TOML table. Per-key override
        # only — missing keys keep the default.
        for section_name in (
            "activation", "thresholds", "scoring_weights",
            "trend_modifiers", "boosts", "source_weights",
            "trend", "consolidation",
        ):
            section = data.get(section_name, {})
            if not isinstance(section, dict):
                continue
            target = getattr(cfg, section_name)
            for key, value in section.items():
                if hasattr(target, key):
                    setattr(target, key, value)
                # else: silently skip; could log "unknown key" here.
        return cfg


#: Module-level default. Modules import this for their inline-constant
#: behavior; mimir's bootstrap replaces it via
#: ``config.DEFAULT = MemoryConfig.from_toml_dict(...)``.
DEFAULT = MemoryConfig()
