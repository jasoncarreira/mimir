"""Viability metrics — collapse detection + write-side curation rate.

Background: the 2026-05-23 VSM evaluation (``state/wiki/topics/
mimir-vsm-eval.md``) flagged two Top-5 critical gaps that PR #291's
P0 set deliberately deferred because they're *observability* gaps,
not *safety* gaps:

  1. Collapse detection metrics — no rolling cosine similarity,
     atom-citation Gini, or topic-diversity computed anywhere,
     despite ``state/wiki/concepts/collapse-dynamics.md`` naming
     all three as required indicators.

  2. Write-side curation rate baseline — Strix's viability criterion
     requires active synthesis + pruning at a minimum rate; mimir's
     weekly reflection is the only curating pass and its output
     volume has never been benchmarked against a viability threshold.

This module instruments both. It does NOT enforce thresholds (no
automatic action on collapse risk); it surfaces the data as
algedonic events so the agent and operator can act consciously.

## Collapse metrics (per ``collapse-dynamics.md``)

Three indicators, computed over a trailing window of turns:

- **Output cosine similarity** — mean + max pairwise cosine similarity
  between the embeddings of recent assistant outputs. Detects
  autoregressive lock-in (the model's outputs converging on a
  fixed-point attractor).
- **Atom-citation Gini** — concentration of SAGA atom citations
  across recent turns. > 0.7 sustained = working-set variety
  decaying.
- **Topic-diversity ratio** — distinct (channel × trigger × top
  output token) tuples / total turns. Detects topic lock-in at
  the session-planning level.

## Curation metrics

Measured over a trailing window (default 4 weeks):

- Bytes of reflection / synthesis output per week
- Count of reflection turns per week
- ``saga_feedback`` events per week (atoms marked positive/negative)
- ``saga_forget`` events per week (atoms tombstoned)

Compared against a proposed minimum-viable threshold:

- ≥ 500 bytes of reflection output / week
- ≥ 5 ``saga_feedback`` events / week
- ≥ 1 ``saga_forget`` event / 4 weeks

Below threshold → ``curation_below_threshold`` algedonic event with
the specific metric flagged.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


# ── thresholds (proposed starting points; tune by editing here, or
#    add env-var overrides later if operators need per-deployment knobs) ──


# Collapse thresholds — conservative defaults; revisit once we have
# multi-deployment baselines.
COSINE_SIM_MEAN_THRESHOLD = 0.85
ATOM_CITATION_GINI_THRESHOLD = 0.70
TOPIC_DIVERSITY_MIN_RATIO = 0.20

# Curation minimum-viable thresholds — proposed starting points, not
# absolute. The whole point of measuring is to refine these against
# real data over time.
CURATION_MIN_REFLECTION_BYTES_PER_WEEK = 500
CURATION_MIN_FEEDBACK_EVENTS_PER_WEEK = 5
CURATION_MIN_FORGET_EVENTS_PER_4WEEKS = 1


# ── result types ────────────────────────────────────────────────────


@dataclass
class CollapseMetrics:
    """Snapshot of the three collapse indicators over a trailing window."""

    window_turns: int           # how many recent turns were examined
    window_days: int            # the time window (days), reported for context

    # Output cosine similarity
    cosine_sim_sample_size: int        # how many embedding pairs were compared
    cosine_sim_mean: float | None      # None if < 2 outputs to compare
    cosine_sim_max: float | None
    embedder_unavailable: bool         # True if fastembed not importable
                                       # (we degrade rather than crash)

    # Atom-citation concentration
    citations_total: int
    distinct_atoms_cited: int
    atom_citation_gini: float | None   # None if 0 citations

    # Topic diversity
    distinct_topics: int               # count of distinct (channel, trigger, top-token) tuples
    topic_diversity_ratio: float | None  # distinct_topics / window_turns


@dataclass
class CurationMetrics:
    """Snapshot of write-side curation rates over a trailing window."""

    window_days: int                            # default 28 (4 weeks)

    reflection_turn_count: int                  # reflection / saga_session_end turns
    reflection_bytes_total: int                 # sum of output length on those turns
    reflection_bytes_per_week: float            # normalized to weekly

    feedback_event_count: int                   # saga_feedback_sent events
    feedback_events_per_week: float

    forget_event_count: int                     # saga_forget_ok events
    # Note: forget rate is reported absolute over the window, since
    # the proposed threshold is "≥ 1 per 4 weeks".


@dataclass
class ViabilityReport:
    """Combined report — collapse + curation, with threshold warnings."""

    generated_at: datetime
    home: Path

    collapse: CollapseMetrics
    curation: CurationMetrics

    warnings: list[str] = field(default_factory=list)
    """Threshold-crossing warnings ready for algedonic emit."""

    def render(self) -> str:
        """Markdown-formatted report for the on-disk file + CLI."""
        lines: list[str] = []
        lines.append("# Mimir viability report")
        lines.append("")
        lines.append(f"_Generated {self.generated_at.isoformat(timespec='seconds')} from {self.home}_")
        lines.append("")

        # Collapse section.
        c = self.collapse
        lines.append(f"## Collapse indicators (trailing {c.window_days}d / {c.window_turns} turns)")
        lines.append("")
        if c.embedder_unavailable:
            lines.append("- ⚠️ fastembed not available — output cosine similarity not computed.")
        elif c.cosine_sim_mean is None:
            lines.append(f"- Output cosine similarity: (no data — only {c.cosine_sim_sample_size} comparable pairs)")
        else:
            flag = " ⚠️" if c.cosine_sim_mean >= COSINE_SIM_MEAN_THRESHOLD else ""
            lines.append(
                f"- Output cosine similarity: mean={c.cosine_sim_mean:.3f}, "
                f"max={c.cosine_sim_max:.3f} over {c.cosine_sim_sample_size} pairs"
                f" (threshold mean ≥ {COSINE_SIM_MEAN_THRESHOLD}){flag}"
            )

        if c.atom_citation_gini is None:
            lines.append(f"- Atom-citation Gini: (no citations in window — {c.citations_total} total)")
        else:
            flag = " ⚠️" if c.atom_citation_gini >= ATOM_CITATION_GINI_THRESHOLD else ""
            lines.append(
                f"- Atom-citation Gini: {c.atom_citation_gini:.3f} across "
                f"{c.citations_total} citations of {c.distinct_atoms_cited} distinct atoms"
                f" (threshold ≥ {ATOM_CITATION_GINI_THRESHOLD}){flag}"
            )

        if c.topic_diversity_ratio is None:
            lines.append(f"- Topic diversity: (no turns in window)")
        else:
            flag = " ⚠️" if c.topic_diversity_ratio < TOPIC_DIVERSITY_MIN_RATIO else ""
            lines.append(
                f"- Topic diversity: {c.distinct_topics} distinct topics / {c.window_turns} turns "
                f"= ratio {c.topic_diversity_ratio:.3f} "
                f"(threshold ≥ {TOPIC_DIVERSITY_MIN_RATIO}){flag}"
            )

        lines.append("")
        # Curation section.
        cur = self.curation
        lines.append(f"## Write-side curation (trailing {cur.window_days}d)")
        lines.append("")
        flag = " ⚠️" if cur.reflection_bytes_per_week < CURATION_MIN_REFLECTION_BYTES_PER_WEEK else ""
        lines.append(
            f"- Reflection output: {cur.reflection_bytes_total} bytes total, "
            f"{cur.reflection_bytes_per_week:.0f} bytes/week, "
            f"{cur.reflection_turn_count} reflection turns "
            f"(threshold ≥ {CURATION_MIN_REFLECTION_BYTES_PER_WEEK} bytes/week){flag}"
        )
        flag = " ⚠️" if cur.feedback_events_per_week < CURATION_MIN_FEEDBACK_EVENTS_PER_WEEK else ""
        lines.append(
            f"- SAGA feedback events: {cur.feedback_event_count} total, "
            f"{cur.feedback_events_per_week:.1f}/week "
            f"(threshold ≥ {CURATION_MIN_FEEDBACK_EVENTS_PER_WEEK}/week){flag}"
        )
        flag = " ⚠️" if cur.forget_event_count < CURATION_MIN_FORGET_EVENTS_PER_4WEEKS else ""
        lines.append(
            f"- SAGA forget events: {cur.forget_event_count} in window "
            f"(threshold ≥ {CURATION_MIN_FORGET_EVENTS_PER_4WEEKS} per 4-week window){flag}"
        )

        if self.warnings:
            lines.append("")
            lines.append("## Warnings")
            lines.append("")
            for w in self.warnings:
                lines.append(f"- {w}")
        lines.append("")
        return "\n".join(lines)


# ── helpers ─────────────────────────────────────────────────────────


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iter_jsonl(path: Path) -> Iterable[dict]:
    """Yield records from a JSONL file. Silently skips malformed lines —
    a corrupt entry shouldn't kill the whole report."""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _gini(counts: list[int]) -> float:
    """Gini coefficient on a list of counts. 0 = uniform, 1 = fully
    concentrated on one bucket. Standard formula:

        G = (Σ_i Σ_j |x_i - x_j|) / (2 n Σ x_i)

    For very small samples (n < 2) returns 0 (insufficient data to
    speak of concentration)."""
    n = len(counts)
    if n < 2:
        return 0.0
    total = sum(counts)
    if total == 0:
        return 0.0
    sorted_counts = sorted(counts)
    cumsum = 0
    for i, x in enumerate(sorted_counts, start=1):
        cumsum += i * x
    # Gini via the sorted-sum form: 2/(n Σx) * Σ(i · x_i) − (n+1)/n
    return (2 * cumsum) / (n * total) - (n + 1) / n


def _top_token(text: str) -> str:
    """Single load-bearing token from a turn's output — used as a
    topic proxy. Strips common short words, takes the most frequent
    remaining lowercased alpha token. Length-3+ to avoid junk like
    ``the`` / ``and``."""
    if not text:
        return ""
    # Cheap stopword pass — not exhaustive, just gets the obvious ones.
    stopwords = {
        "the", "and", "for", "are", "was", "were", "this", "that",
        "with", "from", "have", "has", "had", "will", "would", "could",
        "should", "into", "your", "you", "but", "not", "any", "all",
        "out", "now", "see", "use", "via", "per", "let", "got", "yes",
        "okay", "fine", "good", "well", "just", "going", "want",
    }
    tokens = re.findall(r"[A-Za-z]{4,}", text.lower())
    if not tokens:
        return ""
    counts = Counter(t for t in tokens if t not in stopwords)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


# ── collapse metrics ───────────────────────────────────────────────


def compute_collapse_metrics(
    home: Path,
    *,
    window_days: int = 7,
    now: datetime | None = None,
    max_turns: int = 200,
) -> CollapseMetrics:
    """Compute the three collapse indicators over a trailing window.

    ``window_days`` — only include turns with ts within the window.
    ``max_turns`` — hard cap on outputs to embed. We compute cosine
    on consecutive pairs (O(n) pair comparisons), so the dominant cost
    is the fastembed step itself: 200 turns keeps embedding time
    bounded on the weekly cron without truncating realistic windows.
    """
    now = now or datetime.now(tz=timezone.utc)
    horizon = now - timedelta(days=window_days)

    # Walk turns.jsonl, collect what we need.
    outputs: list[str] = []
    atom_ids: list[str] = []
    topic_keys: list[tuple[str, str, str]] = []

    turns_log = home / "logs" / "turns.jsonl"
    for rec in _iter_jsonl(turns_log):
        ts = _parse_ts(rec.get("ts") or rec.get("timestamp"))
        if ts is None or ts < horizon:
            continue
        # Skip turns that errored — they don't represent real outputs.
        if rec.get("error"):
            continue
        output = rec.get("output") or ""
        if output:
            outputs.append(output)
        for aid in (rec.get("saga_atom_ids") or []):
            if aid:
                atom_ids.append(str(aid))
        channel = str(rec.get("channel_id") or "")
        trigger = str(rec.get("trigger") or "")
        topic_keys.append((channel, trigger, _top_token(output)))

    # Cap to bound fastembed cost (consecutive-pair cosine is O(n);
    # the embedding step is the actual cost driver).
    outputs_sample = outputs[-max_turns:]

    # Output cosine similarity — uses fastembed via the existing
    # Indexer's embedder. Fall through if fastembed isn't installed.
    cosine_sim_mean: float | None = None
    cosine_sim_max: float | None = None
    sample_size = 0
    embedder_unavailable = False
    if len(outputs_sample) >= 2:
        try:
            from .search import FastEmbedder
            embedder = FastEmbedder()
            vectors = list(embedder.embed(outputs_sample))
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            log.warning("collapse: embedder unavailable (%s)", exc)
            embedder_unavailable = True
            vectors = []
        if vectors and len(vectors) >= 2:
            sims: list[float] = []
            # Consecutive-pair similarity (autoregressive lock-in
            # framing — "is the next output too similar to the prior?").
            for i in range(len(vectors) - 1):
                sims.append(_cosine(vectors[i], vectors[i + 1]))
            if sims:
                cosine_sim_mean = sum(sims) / len(sims)
                cosine_sim_max = max(sims)
                sample_size = len(sims)

    # Atom-citation Gini.
    citation_counts = Counter(atom_ids)
    distinct_atoms = len(citation_counts)
    citations_total = sum(citation_counts.values())
    atom_citation_gini: float | None
    if citations_total == 0:
        atom_citation_gini = None
    else:
        atom_citation_gini = _gini(list(citation_counts.values()))

    # Topic diversity.
    window_turns = len(topic_keys)
    distinct_topics = len(set(topic_keys))
    topic_diversity_ratio: float | None
    if window_turns == 0:
        topic_diversity_ratio = None
    else:
        topic_diversity_ratio = distinct_topics / window_turns

    return CollapseMetrics(
        window_turns=window_turns,
        window_days=window_days,
        cosine_sim_sample_size=sample_size,
        cosine_sim_mean=cosine_sim_mean,
        cosine_sim_max=cosine_sim_max,
        embedder_unavailable=embedder_unavailable,
        citations_total=citations_total,
        distinct_atoms_cited=distinct_atoms,
        atom_citation_gini=atom_citation_gini,
        distinct_topics=distinct_topics,
        topic_diversity_ratio=topic_diversity_ratio,
    )


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


# ── curation metrics ────────────────────────────────────────────────


def compute_curation_metrics(
    home: Path,
    *,
    window_days: int = 28,
    now: datetime | None = None,
) -> CurationMetrics:
    """Measure write-side curation rate over a trailing window.

    Counted from two sources:
    - ``turns.jsonl``: reflection / saga_session_end turns (the
      pruning + synthesis passes).
    - ``events.jsonl``: ``saga_feedback_sent`` events (atoms marked
      positive/negative) and ``saga_forget_ok`` events (atoms
      tombstoned).
    """
    now = now or datetime.now(tz=timezone.utc)
    horizon = now - timedelta(days=window_days)
    weeks = max(1.0, window_days / 7.0)

    reflection_turn_count = 0
    reflection_bytes_total = 0
    reflection_triggers = {"reflect", "saga_session_end"}

    for rec in _iter_jsonl(home / "logs" / "turns.jsonl"):
        ts = _parse_ts(rec.get("ts") or rec.get("timestamp"))
        if ts is None or ts < horizon:
            continue
        if rec.get("error"):
            continue
        if rec.get("trigger") in reflection_triggers:
            reflection_turn_count += 1
            reflection_bytes_total += len(rec.get("output") or "")

    feedback_event_count = 0
    forget_event_count = 0
    feedback_event_types = {"saga_feedback_sent"}

    for rec in _iter_jsonl(home / "logs" / "events.jsonl"):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None or ts < horizon:
            continue
        etype = rec.get("type")
        if etype in feedback_event_types:
            feedback_event_count += 1
        elif etype == "saga_forget_ok":
            forget_event_count += 1

    return CurationMetrics(
        window_days=window_days,
        reflection_turn_count=reflection_turn_count,
        reflection_bytes_total=reflection_bytes_total,
        reflection_bytes_per_week=reflection_bytes_total / weeks,
        feedback_event_count=feedback_event_count,
        feedback_events_per_week=feedback_event_count / weeks,
        forget_event_count=forget_event_count,
    )


# ── combined report + warnings ──────────────────────────────────────


def build_report(
    home: Path,
    *,
    collapse_window_days: int = 7,
    curation_window_days: int = 28,
    now: datetime | None = None,
) -> ViabilityReport:
    """Compute both metric families + the threshold warnings list."""
    now = now or datetime.now(tz=timezone.utc)
    collapse = compute_collapse_metrics(
        home, window_days=collapse_window_days, now=now,
    )
    curation = compute_curation_metrics(
        home, window_days=curation_window_days, now=now,
    )

    warnings: list[str] = []
    if (collapse.cosine_sim_mean is not None
            and collapse.cosine_sim_mean >= COSINE_SIM_MEAN_THRESHOLD):
        warnings.append(
            f"collapse_risk_output_self_similarity: cosine mean "
            f"{collapse.cosine_sim_mean:.3f} ≥ {COSINE_SIM_MEAN_THRESHOLD} "
            f"over {collapse.cosine_sim_sample_size} consecutive pairs"
        )
    if (collapse.atom_citation_gini is not None
            and collapse.atom_citation_gini >= ATOM_CITATION_GINI_THRESHOLD):
        warnings.append(
            f"collapse_risk_atom_concentration: Gini "
            f"{collapse.atom_citation_gini:.3f} ≥ {ATOM_CITATION_GINI_THRESHOLD} "
            f"across {collapse.citations_total} citations"
        )
    if (collapse.topic_diversity_ratio is not None
            and collapse.topic_diversity_ratio < TOPIC_DIVERSITY_MIN_RATIO):
        warnings.append(
            f"collapse_risk_topic_lock: diversity ratio "
            f"{collapse.topic_diversity_ratio:.3f} < {TOPIC_DIVERSITY_MIN_RATIO} "
            f"({collapse.distinct_topics}/{collapse.window_turns} turns)"
        )
    if curation.reflection_bytes_per_week < CURATION_MIN_REFLECTION_BYTES_PER_WEEK:
        warnings.append(
            f"curation_below_threshold_reflection: "
            f"{curation.reflection_bytes_per_week:.0f} bytes/week < "
            f"{CURATION_MIN_REFLECTION_BYTES_PER_WEEK}"
        )
    if curation.feedback_events_per_week < CURATION_MIN_FEEDBACK_EVENTS_PER_WEEK:
        warnings.append(
            f"curation_below_threshold_feedback: "
            f"{curation.feedback_events_per_week:.1f} events/week < "
            f"{CURATION_MIN_FEEDBACK_EVENTS_PER_WEEK}"
        )
    if curation.forget_event_count < CURATION_MIN_FORGET_EVENTS_PER_4WEEKS:
        warnings.append(
            f"curation_below_threshold_forget: "
            f"{curation.forget_event_count} in {curation.window_days}d window < "
            f"{CURATION_MIN_FORGET_EVENTS_PER_4WEEKS}"
        )

    return ViabilityReport(
        generated_at=now,
        home=home,
        collapse=collapse,
        curation=curation,
        warnings=warnings,
    )


# ── persistence + emission ──────────────────────────────────────────


def write_report(report: ViabilityReport) -> Path:
    """Write the rendered report to ``<home>/state/reports/
    viability-YYYY-MM-DD.md`` and return the path."""
    out_dir = report.home / "state" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"viability-{report.generated_at.strftime('%Y-%m-%d')}.md"
    out_path.write_text(report.render(), encoding="utf-8")
    return out_path


async def emit_warnings(report: ViabilityReport) -> None:
    """Emit one algedonic event per warning. Fire-and-forget — never
    raises so a logging failure can't crash the scheduler."""
    from .event_logger import log_event
    for warning in report.warnings:
        # Each warning starts with a stable kind prefix; use it as
        # the event type so feedback.py's _EVENT_RULES can route.
        kind, _, detail = warning.partition(":")
        kind = kind.strip()
        if not kind:
            continue
        try:
            await log_event(kind, detail=detail.strip())
        except Exception:  # noqa: BLE001
            log.exception("viability emit_warnings: log_event failed for %s", kind)


# ── CLI + scheduler entrypoints ─────────────────────────────────────


def run_viability_report_cmd(
    home: Path,
    *,
    collapse_window_days: int = 7,
    curation_window_days: int = 28,
    write_to_disk: bool = True,
) -> int:
    """``mimir viability-report`` — operator-facing manual run. Builds
    the report, prints it to stdout, optionally writes to disk.
    Returns the count of threshold warnings (0 = clean)."""
    report = build_report(
        home,
        collapse_window_days=collapse_window_days,
        curation_window_days=curation_window_days,
    )
    print(report.render())
    if write_to_disk:
        out_path = write_report(report)
        print(f"\nWritten to {out_path}", end="")
    return len(report.warnings)


async def run_scheduled_viability_report(home: Path) -> None:
    """Scheduled-job callable. Builds the report, writes to disk, emits
    one event per warning, and emits a paired ``viability_report_ok``
    when the run completes (regardless of warning count) so operators
    can confirm the job is firing."""
    from .event_logger import log_event
    try:
        report = build_report(home)
        out_path = write_report(report)
        await emit_warnings(report)
        await log_event(
            "viability_report_ok",
            report_path=str(out_path),
            warnings=len(report.warnings),
        )
    except Exception as exc:  # noqa: BLE001 — defensive scheduler boundary
        log.exception("viability report failed")
        await log_event(
            "viability_report_error",
            error=f"{type(exc).__name__}: {exc}",
        )
