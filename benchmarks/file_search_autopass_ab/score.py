"""Scoring for the file_search autopass A/B harness.

Reads both arms' per-probe result JSONL (written by runner.py) and
computes the seven metrics enumerated in
``state/spec/chainlink-138-sub-b-recon.md`` §"Metrics to capture":

  1. explicit ``mcp__mimir__file_search`` MCP tool-call count
  2. ``grep`` + ``Glob`` tool-call count
  3. ``Read`` tool-call count
  4. total tool-call count
  5. wall-clock per turn (ms)
  6. cost per turn (USD)
  7. outcome quality — binary hit/miss on path-citation in the reply

Mean ± stdev per arm; Welch's t-test p-value for tool-call deltas.

The module is import-clean (no I/O at import time); the runner calls
``score_run(...)`` after both arms complete. The same entrypoint can
be invoked from a follow-up CLI to re-score a prior run without
re-running the harness.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# Tool-name fingerprints. Mimir routes file_search through the MCP
# server (``mcp__mimir__file_search``); grep/Glob/Read are SDK built-ins
# with stable names from the Agent SDK.
_FILE_SEARCH_NAMES = {"mcp__mimir__file_search", "file_search"}
_GREP_GLOB_NAMES = {"Grep", "Glob"}
_READ_NAMES = {"Read"}


@dataclass
class ProbeResult:
    """One probe × one arm. Populated by ``runner.py``."""

    probe_index: int
    probe_text: str
    expected_target: str
    shape: str
    arm: str  # "on" | "off"
    # Tool-call counts (extracted from the turn record's ``events``).
    file_search_count: int = 0
    grep_glob_count: int = 0
    read_count: int = 0
    total_tool_calls: int = 0
    # Wall-clock + cost.
    duration_ms: int = 0
    total_cost_usd: float | None = None
    # Outcome: did the reply text cite the expected path (case-insensitive
    # substring match)? ``None`` when the reply was empty / missing.
    hit: bool | None = None
    # The agent's actual reply text. Persisted for post-hoc review.
    reply: str = ""
    # Whether the turn completed at all. ``False`` means the runner
    # couldn't capture the turn (queue stall, /event 5xx, etc.).
    captured: bool = True
    error: str | None = None


def load_results(path: Path) -> list[ProbeResult]:
    """Load one arm's per-probe JSONL into ``ProbeResult`` instances."""
    out: list[ProbeResult] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        out.append(ProbeResult(
            probe_index=d["probe_index"],
            probe_text=d["probe_text"],
            expected_target=d["expected_target"],
            shape=d["shape"],
            arm=d["arm"],
            file_search_count=d.get("file_search_count", 0),
            grep_glob_count=d.get("grep_glob_count", 0),
            read_count=d.get("read_count", 0),
            total_tool_calls=d.get("total_tool_calls", 0),
            duration_ms=d.get("duration_ms", 0),
            total_cost_usd=d.get("total_cost_usd"),
            hit=d.get("hit"),
            reply=d.get("reply", ""),
            captured=d.get("captured", True),
            error=d.get("error"),
        ))
    return out


def write_results(path: Path, results: Iterable[ProbeResult]) -> int:
    """Persist results to JSONL. Returns count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({
                "probe_index": r.probe_index,
                "probe_text": r.probe_text,
                "expected_target": r.expected_target,
                "shape": r.shape,
                "arm": r.arm,
                "file_search_count": r.file_search_count,
                "grep_glob_count": r.grep_glob_count,
                "read_count": r.read_count,
                "total_tool_calls": r.total_tool_calls,
                "duration_ms": r.duration_ms,
                "total_cost_usd": r.total_cost_usd,
                "hit": r.hit,
                "reply": r.reply,
                "captured": r.captured,
                "error": r.error,
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def extract_metrics_from_turn(
    turn_record: dict[str, Any],
    *,
    expected_target: str,
) -> dict[str, Any]:
    """Pull the seven metrics out of a single ``TurnRecord`` dict.

    ``turn_record`` is one row of ``turns.jsonl`` (after json.loads).
    ``expected_target`` is the probe's target path; we case-insensitive
    substring-match it against the turn's ``output`` text to determine
    the ``hit`` boolean.
    """
    events = turn_record.get("events") or []
    file_search_count = 0
    grep_glob_count = 0
    read_count = 0
    total_tool_calls = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "tool_call":
            continue
        total_tool_calls += 1
        name = ev.get("name") or ""
        if name in _FILE_SEARCH_NAMES:
            file_search_count += 1
        elif name in _GREP_GLOB_NAMES:
            grep_glob_count += 1
        elif name in _READ_NAMES:
            read_count += 1

    reply_text = (turn_record.get("output") or "").strip()
    if not reply_text:
        hit: bool | None = None
    else:
        hit = expected_target.lower() in reply_text.lower()

    return {
        "file_search_count": file_search_count,
        "grep_glob_count": grep_glob_count,
        "read_count": read_count,
        "total_tool_calls": total_tool_calls,
        "duration_ms": int(turn_record.get("duration_ms") or 0),
        "total_cost_usd": turn_record.get("total_cost_usd"),
        "hit": hit,
        "reply": reply_text,
    }


# ----------------------------------------------------------------------
# Aggregate stats
# ----------------------------------------------------------------------


def _mean_stdev(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return float(values[0]), 0.0
    return statistics.fmean(values), statistics.stdev(values)


def _welch_p(a: list[float], b: list[float]) -> float | None:
    """Two-sided Welch's t-test p-value. Returns ``None`` when one arm
    has <2 values (no variance to compare).

    Imports scipy lazily; if scipy isn't installed we fall back to a
    pure-Python implementation that's good enough for the n≈30 case
    (uses Welch-Satterthwaite df and a survival-function approximation
    via ``math.erf`` for the t→normal limit, which is fine at df≥20).
    """
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        from scipy import stats as _stats  # type: ignore[import-not-found]
        result = _stats.ttest_ind(a, b, equal_var=False)
        return float(result.pvalue)
    except ImportError:
        # Fallback: normal-approximation Welch's. At n≥20 per arm the
        # t-distribution is within ~5% of the normal, which is enough
        # resolution for the "is the autopass effect significant?" call.
        mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
        var_a = statistics.variance(a)
        var_b = statistics.variance(b)
        se = math.sqrt(var_a / len(a) + var_b / len(b))
        if se == 0:
            return 1.0 if mean_a == mean_b else 0.0
        t = (mean_a - mean_b) / se
        # Two-sided p-value via the normal CDF (≈ t-CDF at high df).
        # erfc(|t|/√2) is the two-sided tail mass.
        return math.erfc(abs(t) / math.sqrt(2))


@dataclass
class MetricSummary:
    name: str
    on_mean: float
    on_stdev: float
    off_mean: float
    off_stdev: float
    delta_mean: float  # on - off
    p_value: float | None  # Welch's t-test on the per-probe values


def _summarise_one(
    *,
    name: str,
    on: list[float],
    off: list[float],
) -> MetricSummary:
    on_mean, on_stdev = _mean_stdev(on)
    off_mean, off_stdev = _mean_stdev(off)
    return MetricSummary(
        name=name,
        on_mean=on_mean,
        on_stdev=on_stdev,
        off_mean=off_mean,
        off_stdev=off_stdev,
        delta_mean=on_mean - off_mean,
        p_value=_welch_p(on, off),
    )


@dataclass
class RunSummary:
    metrics: list[MetricSummary]
    on_hit_rate: float
    off_hit_rate: float
    n_probes: int
    n_on: int
    n_off: int
    per_probe: list[dict[str, Any]] = field(default_factory=list)


def _hit_rate(results: list[ProbeResult]) -> float:
    if not results:
        return 0.0
    captured = [r for r in results if r.captured and r.hit is not None]
    if not captured:
        return 0.0
    return sum(1 for r in captured if r.hit) / len(captured)


def summarise(
    on_results: list[ProbeResult],
    off_results: list[ProbeResult],
) -> RunSummary:
    """Compute the seven-metric summary + per-probe outcome table."""
    on_by_idx = {r.probe_index: r for r in on_results}
    off_by_idx = {r.probe_index: r for r in off_results}
    common = sorted(set(on_by_idx) & set(off_by_idx))

    def _pull(arm_results: list[ProbeResult], attr: str) -> list[float]:
        vals: list[float] = []
        for r in arm_results:
            if not r.captured:
                continue
            v = getattr(r, attr)
            if v is None:
                continue
            vals.append(float(v))
        return vals

    metrics = [
        _summarise_one(
            name="file_search tool calls",
            on=_pull(on_results, "file_search_count"),
            off=_pull(off_results, "file_search_count"),
        ),
        _summarise_one(
            name="grep + Glob tool calls",
            on=_pull(on_results, "grep_glob_count"),
            off=_pull(off_results, "grep_glob_count"),
        ),
        _summarise_one(
            name="Read tool calls",
            on=_pull(on_results, "read_count"),
            off=_pull(off_results, "read_count"),
        ),
        _summarise_one(
            name="total tool calls",
            on=_pull(on_results, "total_tool_calls"),
            off=_pull(off_results, "total_tool_calls"),
        ),
        _summarise_one(
            name="wall-clock per turn (ms)",
            on=_pull(on_results, "duration_ms"),
            off=_pull(off_results, "duration_ms"),
        ),
        _summarise_one(
            name="cost per turn (USD)",
            on=_pull(on_results, "total_cost_usd"),
            off=_pull(off_results, "total_cost_usd"),
        ),
    ]

    per_probe = []
    for idx in common:
        on_r = on_by_idx[idx]
        off_r = off_by_idx[idx]
        per_probe.append({
            "probe_index": idx,
            "shape": on_r.shape,
            "expected_target": on_r.expected_target,
            "probe_text": on_r.probe_text,
            "on_hit": on_r.hit,
            "off_hit": off_r.hit,
            "on_tools": on_r.total_tool_calls,
            "off_tools": off_r.total_tool_calls,
        })

    return RunSummary(
        metrics=metrics,
        on_hit_rate=_hit_rate(on_results),
        off_hit_rate=_hit_rate(off_results),
        n_probes=len(common),
        n_on=len(on_results),
        n_off=len(off_results),
        per_probe=per_probe,
    )


# ----------------------------------------------------------------------
# Markdown rendering
# ----------------------------------------------------------------------


def _fmt_p(p: float | None) -> str:
    if p is None:
        return "n/a"
    if p < 0.001:
        return "<0.001"
    return f"{p:.3f}"


def render_markdown(summary: RunSummary, *, run_tag: str) -> str:
    """Render the seven-metric + per-probe + recommendation report.

    Recommendation logic (per recon doc §"Reporting shape"):
      - "ship Sub A as-is, skip Sub C": autopass-on reduces total
        tool-calls (mean delta ≤ -0.5, p<0.10) AND hit-rate did not
        regress (Δ ≥ 0).
      - "ship Sub A + proceed to Sub C": autopass-on improves hit-rate
        meaningfully (Δ ≥ +0.10) — there's headroom for a stronger
        retrieval backend to push it further.
      - "don't ship": autopass-on raises cost/latency without a
        tool-call or hit-rate win.
    """
    by_name = {m.name: m for m in summary.metrics}
    tool_total = by_name["total tool calls"]
    hit_delta = summary.on_hit_rate - summary.off_hit_rate
    cost = by_name["cost per turn (USD)"]

    # Recommendation logic.
    tool_call_reduced = (
        tool_total.delta_mean <= -0.5
        and (tool_total.p_value is not None and tool_total.p_value < 0.10)
    )
    hit_rate_up = hit_delta >= 0.10
    hit_rate_down = hit_delta <= -0.05
    cost_regression = cost.delta_mean > 0 and not (tool_call_reduced or hit_rate_up)

    cost_direction = (
        "raised" if cost.delta_mean > 0 else "lowered"
    )
    cost_abs = abs(cost.delta_mean)

    if hit_rate_up:
        rec = "ship Sub A + proceed to Sub C"
        rec_rationale = (
            f"Autopass-on improved hit-rate by {hit_delta:+.2%}. Late-interaction "
            "retrieval (ColBERT) has headroom to push the lift further on the "
            "fingerprinted-error and concept-lookup probes that benefit most "
            "from semantic matching."
        )
    elif tool_call_reduced and not hit_rate_down:
        rec = "ship Sub A as-is, skip Sub C"
        rec_rationale = (
            f"Autopass-on cut mean tool calls by {-tool_total.delta_mean:.2f} "
            f"(p={_fmt_p(tool_total.p_value)}) without regressing hit-rate. The "
            "existing hybrid backend produces enough lift that the ColBERT "
            "structural swap isn't a slam-dunk."
        )
    elif hit_rate_down:
        rec = "don't ship"
        rec_rationale = (
            "Autopass-on regressed hit-rate by "
            f"{-hit_delta:.2%} (Δ {hit_delta:+.2%}) and did not significantly "
            f"reduce tool calls (Δ {tool_total.delta_mean:+.2f}, "
            f"p={_fmt_p(tool_total.p_value)}). Cost was {cost_direction} by "
            f"{cost_abs:.4f} USD/turn. The quality regression is the load-bearing "
            "signal — close parent chainlink with the bounded learning, or "
            "revisit the autopass block design before re-running."
        )
    elif cost_regression:
        rec = "don't ship"
        rec_rationale = (
            f"Autopass-on raised cost by {cost.delta_mean:+.4f} USD/turn "
            "without reducing tool calls (Δ "
            f"{tool_total.delta_mean:+.2f}, p={_fmt_p(tool_total.p_value)}) "
            f"or improving hit-rate (Δ {hit_delta:+.2%}). Close parent "
            "chainlink with the bounded learning."
        )
    else:
        rec = "ship Sub A as-is, skip Sub C"
        rec_rationale = (
            "Autopass-on did not meaningfully shift the metrics in either "
            "direction. The block is cheap and harmless; keep it shipped, but "
            "the ColBERT swap doesn't have a measurable target to hit."
        )

    lines: list[str] = []
    lines.append(f"<!-- desc: A/B harness results for chainlink #140 (Sub B of #138). Run tag: {run_tag}. -->")
    lines.append(f"# chainlink #138 Sub B — file_search autopass A/B results")
    lines.append("")
    lines.append(f"**Run tag:** `{run_tag}`  ")
    lines.append(f"**Probe count:** {summary.n_probes} (on={summary.n_on}, off={summary.n_off})")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"**{rec}**")
    lines.append("")
    lines.append(rec_rationale)
    lines.append("")
    lines.append("## Per-metric comparison")
    lines.append("")
    lines.append("| Metric | autopass-on (μ ± σ) | autopass-off (μ ± σ) | Δ (on − off) | p (Welch) |")
    lines.append("|---|---|---|---|---|")
    for m in summary.metrics:
        lines.append(
            f"| {m.name} | {m.on_mean:.3f} ± {m.on_stdev:.3f} "
            f"| {m.off_mean:.3f} ± {m.off_stdev:.3f} "
            f"| {m.delta_mean:+.3f} | {_fmt_p(m.p_value)} |"
        )
    lines.append(
        f"| outcome quality (hit-rate) | {summary.on_hit_rate:.2%} "
        f"| {summary.off_hit_rate:.2%} | {hit_delta:+.2%} | n/a |"
    )
    lines.append("")
    lines.append("## Per-probe outcomes")
    lines.append("")
    lines.append("| # | Shape | Expected target | on hit | off hit | on tools | off tools |")
    lines.append("|---|---|---|---|---|---|---|")

    def _hit_glyph(h: bool | None) -> str:
        if h is True:
            return "yes"
        if h is False:
            return "no"
        return "—"

    for row in summary.per_probe:
        lines.append(
            f"| {row['probe_index']} | {row['shape']} | "
            f"`{row['expected_target']}` "
            f"| {_hit_glyph(row['on_hit'])} | {_hit_glyph(row['off_hit'])} "
            f"| {row['on_tools']} | {row['off_tools']} |"
        )
    lines.append("")
    lines.append("## Interpreting the recommendation")
    lines.append("")
    lines.append("- **\"ship Sub A as-is, skip Sub C\"** — autopass produces a")
    lines.append("  tool-call reduction or hit-rate non-regression, but the existing")
    lines.append("  backend does enough that the ColBERT swap (chainlink #141) isn't")
    lines.append("  worth the structural cost.")
    lines.append("- **\"ship Sub A + proceed to Sub C\"** — autopass helps AND the")
    lines.append("  retrieval misses look like ColBERT's late-interaction architecture")
    lines.append("  could plausibly fix them. Fire chainlink #141.")
    lines.append("- **\"don't ship\"** — autopass adds latency/cost without a")
    lines.append("  measurable quality or tool-call-count win. Close parent chainlink")
    lines.append("  with the bounded learning.")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI re-scoring entrypoint
# ----------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="benchmarks.file_search_autopass_ab.score",
        description=(
            "Re-score a prior file_search autopass A/B run from its "
            "per-arm result JSONLs. Use this when you want to tweak the "
            "scoring rubric without re-running the harness."
        ),
    )
    p.add_argument("--on", required=True, type=Path,
                   help="path to arm_on.jsonl")
    p.add_argument("--off", required=True, type=Path,
                   help="path to arm_off.jsonl")
    p.add_argument("--run-tag", required=True,
                   help="identifier rendered into the markdown header")
    p.add_argument("--output", required=True, type=Path,
                   help="markdown output path")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    on = load_results(args.on)
    off = load_results(args.off)
    summary = summarise(on, off)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(summary, run_tag=args.run_tag), encoding="utf-8")
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
