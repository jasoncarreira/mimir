"""Reference-free quality metrics + ASI for the commitments-extraction GEPA pilot.

Chainlink #404, Path A. These score an extractor candidate's output **without
gold labels** — every signal is computed from (source_text, extracted_texts)
alone, so no hand-annotated corpus is needed. The objective they encode is the
one v3→v4 was about (``state/spec/commitments-v4-evaluation.md``): commitment
texts should be *self-contained* — not over-compressed, not hallucinating
artifact ids, and **retaining the source's artifact ids** so a follow-up is
evaluable without backtracking — while holding extraction volume roughly
constant.

What these metrics deliberately do NOT measure: precision/recall, i.e. whether
the *right* set of commitments was extracted. That needs gold labels (Path B).
Treat a winning candidate here as "more self-contained," not "more correct" —
the adoption gate (a reviewed PR + human spot-check) is where correctness is
judged.

Scoring shape (per example):

    score = mean(per_text_quality) × (1 − count_penalty) × coverage_factor

  * per_text_quality — intrinsic defects only: over-compression, over-length,
    hallucinated ids. (Retention is NOT scored here — see below.)
  * count_penalty — deviation from the baseline's extraction volume.
  * coverage_factor — of the source's artifact ids, the fraction retained
    across all extracted texts, weighted by ``_COVERAGE_WEIGHT``.

Why coverage is a *factor*, not a per-text bonus (chainlink #404 review): a
per-text "+bonus on top of 1.0" is erased by the [0,1] clamp, so an id-dropping
paraphrase scored identically to an id-preserving one and GEPA got no pressure
to keep refs. An example-level coverage factor cannot be clamped away — dropping
the source's ids measurably lowers the score.

Anti-Goodhart guards: hallucinated ids → strong per-text penalty; length cap;
count anchored to the baseline. Reference-free confound (same one the v4 spec
flagged): coverage can't tell which id "belongs" to which commitment, and a
correctly-dropped commitment also drops its ids — the count anchor and the
human spot-check at the adoption gate mitigate this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Over-compression / schema bounds (from the v4 rubric + the schema's <=120).
MIN_SELF_CONTAINED_CHARS = 40
MAX_TEXT_CHARS = 120

# Per-text quality penalties (intrinsic defects). Hallucination dominates.
_PENALTY_OVER_COMPRESSED = 0.5
_PENALTY_OVER_LONG = 0.3
_PENALTY_HALLUCINATED = 0.8

# Example-level volume anchor: penalty = min(rel_dev * slope, cap).
_COUNT_PENALTY_SLOPE = 0.5
_COUNT_PENALTY_CAP = 0.5

# Id-coverage weight: coverage_factor = (1 - w) + w * coverage. With w=0.4,
# retaining all source ids → factor 1.0; dropping them all → 0.6. Big enough to
# be a real GEPA gradient, small enough that intrinsic quality still dominates.
_COVERAGE_WEIGHT = 0.4


# Artifact-id patterns — high-signal, low-false-positive. Normalized to a
# canonical token so "PR #199" and "#199" compare equal.
_ID_PATTERNS = (
    # PR / issue / chainlink numbers: "#199", "PR #199", "chainlink #147".
    re.compile(r"#\s?(\d+)"),
    # chainlink/commit short ids: "c-feeb930cfc".
    re.compile(r"\b(c-[0-9a-f]{6,})\b", re.IGNORECASE),
    # file paths / dotted source files: "skill_outcomes.py", "mimir/agent.py".
    re.compile(r"\b([\w./-]+\.(?:py|md|toml|yaml|yml|json|sh|js|ts|html))\b"),
)


def artifact_ids(text: str) -> set[str]:
    """Extract canonical artifact identifiers from ``text``.

    Captures PR/issue/chainlink numbers (``#199``), chainlink/commit short
    ids (``c-feeb930cfc``), and dotted source-file paths (``agent.py``).
    Numbers normalize to ``#<n>`` so ``PR #199`` and ``#199`` match.
    """
    ids: set[str] = set()
    for i, pat in enumerate(_ID_PATTERNS):
        for m in pat.finditer(text or ""):
            tok = m.group(1)
            ids.add(f"#{tok}" if i == 0 else tok.lower())
    return ids


@dataclass
class TextEval:
    """Per-commitment-text diagnostic — the unit ASI is built from."""

    text: str
    length: int
    over_compressed: bool
    over_long: bool
    retained_ids: set[str] = field(default_factory=set)
    hallucinated_ids: set[str] = field(default_factory=set)
    quality: float = 0.0


@dataclass
class ExampleEval:
    """Reference-free score + ASI for one example's extraction."""

    score: float
    count: int
    baseline_count: int
    count_penalty: float
    coverage: float
    source_ids: set[str]
    texts: list[TextEval]
    asi: str


def _eval_text(text: str, source_ids: set[str]) -> TextEval:
    """Intrinsic per-text quality: penalize over-compression, over-length, and
    hallucinated ids. Retention is scored at the example level (coverage), NOT
    here — a per-text retention bonus gets erased by the [0,1] clamp (#404 review).
    """
    length = len(text)
    over_compressed = length < MIN_SELF_CONTAINED_CHARS
    over_long = length > MAX_TEXT_CHARS
    ids = artifact_ids(text)
    retained = ids & source_ids
    hallucinated = ids - source_ids

    q = 1.0
    if over_compressed:
        q -= _PENALTY_OVER_COMPRESSED
    if over_long:
        q -= _PENALTY_OVER_LONG
    if hallucinated:
        q -= _PENALTY_HALLUCINATED
    q = max(0.0, min(1.0, q))

    return TextEval(
        text=text,
        length=length,
        over_compressed=over_compressed,
        over_long=over_long,
        retained_ids=retained,
        hallucinated_ids=hallucinated,
        quality=q,
    )


def _count_penalty(count: int, baseline_count: int) -> float:
    """Penalty for deviating from the baseline's extraction volume.

    Anchors volume so the optimizer can't win by extracting nothing (no bad
    texts) or everything (max id coverage). Relative deviation, capped.
    """
    if baseline_count <= 0:
        return min(count * _COUNT_PENALTY_SLOPE, _COUNT_PENALTY_CAP)
    rel_dev = abs(count - baseline_count) / baseline_count
    return min(rel_dev * _COUNT_PENALTY_SLOPE, _COUNT_PENALTY_CAP)


def _coverage(source_ids: set[str], retained_union: set[str]) -> float:
    """Fraction of the source's artifact ids retained across all texts.

    1.0 when the source has no ids (nothing to preserve → no pressure)."""
    if not source_ids:
        return 1.0
    return len(retained_union & source_ids) / len(source_ids)


def score_extraction(
    source_text: str,
    commitment_texts: list[str],
    *,
    baseline_count: int,
) -> ExampleEval:
    """Reference-free score in [0, 1] + ASI for one example.

    ``baseline_count`` is how many commitments the *baseline* prompt extracted
    from the same source — the volume anchor. Score = mean per-text quality ×
    (1 − count_penalty) × coverage_factor.
    """
    source_ids = artifact_ids(source_text)
    texts = [_eval_text(t, source_ids) for t in commitment_texts]
    n = len(texts)
    cpen = _count_penalty(n, baseline_count)
    retained_union: set[str] = set().union(*[t.retained_ids for t in texts]) if texts else set()
    coverage = _coverage(source_ids, retained_union)

    if n == 0:
        # No texts to grade. Perfect only if the baseline also found nothing;
        # otherwise under-extraction. Coverage does not apply (no commitments to
        # carry ids).
        score = (1.0 if baseline_count <= 0 else 0.0) * (1.0 - cpen)
    else:
        base_q = sum(t.quality for t in texts) / n
        cov_factor = (1.0 - _COVERAGE_WEIGHT) + _COVERAGE_WEIGHT * coverage
        score = base_q * (1.0 - cpen) * cov_factor

    return ExampleEval(
        score=max(0.0, min(1.0, score)),
        count=n,
        baseline_count=baseline_count,
        count_penalty=cpen,
        coverage=coverage,
        source_ids=source_ids,
        texts=texts,
        asi=_build_asi(source_ids, retained_union, texts, n, baseline_count, cpen, coverage),
    )


def _build_asi(
    source_ids: set[str],
    retained_union: set[str],
    texts: list[TextEval],
    count: int,
    baseline_count: int,
    count_penalty: float,
    coverage: float,
) -> str:
    """Actionable Side Information: the diagnostic text GEPA reflects on.

    Names *which* text failed *why* and *which* source ids were dropped, so the
    reflection LM can rewrite the prompt to fix the specific failure.
    """
    lines: list[str] = []
    src = ", ".join(sorted(source_ids)) if source_ids else "(none)"
    lines.append(f"source artifact ids: {src}")
    lines.append(f"extracted {count} commitment(s); baseline extracted {baseline_count}.")
    if count_penalty > 0:
        direction = "over-extracting" if count > baseline_count else "under-extracting"
        lines.append(
            f"VOLUME: count deviates from baseline ({direction}); "
            f"penalty {count_penalty:.2f}. Match the baseline's extraction scope."
        )
    # Example-level id preservation — the load-bearing self-containment signal.
    if source_ids:
        missing = sorted(source_ids - retained_union)
        if missing:
            lines.append(
                f"MISSING source ids (in no extracted text): {', '.join(missing)} "
                f"— coverage {len(retained_union & source_ids)}/{len(source_ids)}. "
                "Preserve these artifact refs in the commitment text(s) they belong to."
            )
        else:
            lines.append(f"id coverage: {len(source_ids)}/{len(source_ids)} — all source refs preserved.")
    if not texts:
        if baseline_count > 0:
            lines.append("MISS: extracted nothing while the baseline found commitments.")
        return "\n".join(lines)
    for i, t in enumerate(texts):
        issues: list[str] = []
        if t.over_compressed:
            issues.append(
                f"OVER-COMPRESSED ({t.length}<{MIN_SELF_CONTAINED_CHARS} chars; "
                "too terse to be self-contained — add the artifact ref / disposition)"
            )
        if t.over_long:
            issues.append(f"OVER-LONG ({t.length}>{MAX_TEXT_CHARS} chars; tighten)")
        if t.hallucinated_ids:
            issues.append(
                "HALLUCINATED ids not in source: "
                + ", ".join(sorted(t.hallucinated_ids))
                + " (never invent identifiers)"
            )
        if t.retained_ids:
            issues.append("retained: " + ", ".join(sorted(t.retained_ids)))
        verdict = "; ".join(issues) if issues else "no intrinsic defects"
        lines.append(f"  [{i}] q={t.quality:.2f} {verdict}\n      text: {t.text!r}")
    return "\n".join(lines)


def aggregate(evals: list[ExampleEval]) -> dict[str, float]:
    """Aggregate signals across a set of examples — for the decision record.

    Reports the GEPA objective (mean score) alongside the raw rubric rates from
    the v4 eval spec so a human reviewer can sanity-check that a higher score
    actually means better self-containment (not a gamed metric).
    """
    if not evals:
        return {}
    all_texts = [t for e in evals for t in e.texts]
    n_texts = len(all_texts)
    with_ids = [e for e in evals if e.source_ids]
    return {
        "mean_score": sum(e.score for e in evals) / len(evals),
        "avg_commitments_per_example": sum(e.count for e in evals) / len(evals),
        "avg_text_chars": (sum(t.length for t in all_texts) / n_texts) if n_texts else 0.0,
        "over_compressed_rate": (
            sum(1 for t in all_texts if t.over_compressed) / n_texts if n_texts else 0.0
        ),
        "hallucinated_text_rate": (
            sum(1 for t in all_texts if t.hallucinated_ids) / n_texts if n_texts else 0.0
        ),
        # The v4 spec's headline signal: of source artifact ids, fraction retained
        # (averaged over examples whose source actually carried ids).
        "id_coverage_mean": (sum(e.coverage for e in with_ids) / len(with_ids)) if with_ids else 1.0,
    }
