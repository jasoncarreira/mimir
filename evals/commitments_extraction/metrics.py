"""Reference-free quality metrics + ASI for the commitments-extraction GEPA pilot.

Chainlink #404, Path A. These score an extractor candidate's output **without
gold labels** — every signal is computed from (source_text, extracted_texts)
alone, so no hand-annotated corpus is needed. The objective they encode is the
one v3→v4 was about (``state/spec/commitments-v4-evaluation.md``): commitment
texts should be *self-contained* — not over-compressed, not hallucinating
artifact ids, retaining the ids that are actually in the source — while holding
extraction volume roughly constant.

What these metrics deliberately do NOT measure: precision/recall, i.e. whether
the *right* set of commitments was extracted. That needs gold labels (Path B).
Treat a winning candidate here as "more self-contained," not "more correct" —
the adoption gate (a reviewed PR + human spot-check) is where correctness is
judged.

Anti-Goodhart guards (a text-only optimizer will try to game a scalar):
  * hallucinated ids → strong penalty (blocks "invent ids to look specific").
  * length cap (>120, the schema limit) → penalty (blocks "stuff everything in").
  * count anchored to the baseline's count on the same example → penalty for
    deviation (blocks "extract nothing → no bad texts → perfect" and
    "extract everything → maximize id coverage").
  * the retained-id bonus is small and capped, so it can't dominate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Over-compression / schema bounds (from the v4 rubric + the schema's <=120).
MIN_SELF_CONTAINED_CHARS = 40
MAX_TEXT_CHARS = 120

# Per-text quality penalties/bonus (tuned so hallucination dominates and the
# id bonus can't be farmed past a small cap).
_PENALTY_OVER_COMPRESSED = 0.5
_PENALTY_OVER_LONG = 0.3
_PENALTY_HALLUCINATED = 0.8
_BONUS_PER_RETAINED_ID = 0.1
_BONUS_RETAINED_CAP = 0.2
# Example-level volume anchor: penalty = min(rel_dev * slope, cap).
_COUNT_PENALTY_SLOPE = 0.5
_COUNT_PENALTY_CAP = 0.5


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
    texts: list[TextEval]
    asi: str


def _eval_text(text: str, source_ids: set[str]) -> TextEval:
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
    q += min(len(retained) * _BONUS_PER_RETAINED_ID, _BONUS_RETAINED_CAP)
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
        # Baseline found nothing. Any extraction is over-extraction; scale by
        # how many were produced (1 extra ≈ full slope, capped).
        return min(count * _COUNT_PENALTY_SLOPE, _COUNT_PENALTY_CAP)
    rel_dev = abs(count - baseline_count) / baseline_count
    return min(rel_dev * _COUNT_PENALTY_SLOPE, _COUNT_PENALTY_CAP)


def score_extraction(
    source_text: str,
    commitment_texts: list[str],
    *,
    baseline_count: int,
) -> ExampleEval:
    """Reference-free score in [0, 1] + ASI for one example.

    ``baseline_count`` is how many commitments the *baseline* prompt extracted
    from the same source — the volume anchor. Score combines mean per-text
    quality with the count-deviation penalty.
    """
    source_ids = artifact_ids(source_text)
    texts = [_eval_text(t, source_ids) for t in commitment_texts]
    n = len(texts)
    cpen = _count_penalty(n, baseline_count)

    if n == 0:
        # No texts to grade. Perfect only if the baseline also found nothing;
        # otherwise this is under-extraction and the count penalty bites.
        base_q = 1.0 if baseline_count <= 0 else 0.0
    else:
        base_q = sum(t.quality for t in texts) / n

    score = max(0.0, min(1.0, base_q * (1.0 - cpen)))
    return ExampleEval(
        score=score,
        count=n,
        baseline_count=baseline_count,
        count_penalty=cpen,
        texts=texts,
        asi=_build_asi(source_ids, texts, n, baseline_count, cpen),
    )


def _build_asi(
    source_ids: set[str],
    texts: list[TextEval],
    count: int,
    baseline_count: int,
    count_penalty: float,
) -> str:
    """Actionable Side Information: the diagnostic text GEPA reflects on.

    Not just a number — names *which* text failed *why*, with the source ids,
    so the reflection LM can rewrite the prompt to fix the specific failure.
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
        verdict = "; ".join(issues) if issues else "ok, self-contained"
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
    }
