"""Render the markdown A/B report from ``results.json``.

Output mirrors chainlink #138 Sub B's results.md shape: per-arm
totals, per-category breakouts, per-probe table for divergent
probes, and an honest read + recommendation.

Usage:
    python gen_report.py --results results.json --out report.md

The default --out is ``state/spec/chainlink-141-slice2-ab-results.md``
relative to the repo root, which is the path the brief expects.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_DEFAULT_OUT = _REPO_ROOT / "state" / "spec" / "chainlink-141-slice2-ab-results.md"


def _pct(x: float) -> str:
    return f"{100.0 * x:.1f}%"


def _delta_pp(a: float, b: float) -> str:
    """Format a-b as a signed percentage-point delta."""
    d = 100.0 * (a - b)
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.1f}pp"


def _delta(a: float, b: float, digits: int = 3) -> str:
    d = a - b
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.{digits}f}"


def _render(doc: dict, out_path: Path) -> None:
    arms = doc["arms"]
    probe_count = doc["probe_count"]
    k = doc["k"]
    alpha_c = doc["alpha_arm_c"]

    arm_a, arm_b, arm_c = arms[0], arms[1], arms[2]

    lines: list[str] = []
    lines.append(
        f"<!-- desc: A/B results for chainlink #141 Slice 2: main vs "
        f"ColBERT vs ColBERT+recency on file_search. n={probe_count}, k={k}, "
        f"alpha_arm_c={alpha_c}. -->"
    )
    lines.append("")
    lines.append("# chainlink #141 Slice 2 — file_search ColBERT + recency A/B results")
    lines.append("")
    lines.append(f"**Probe count:** {probe_count}  ")
    lines.append(f"**Top-k measured:** {k}  ")
    lines.append(f"**Arm C alpha:** {alpha_c}  ")
    lines.append(f"**Home:** `{doc['home']}`")
    lines.append("")

    # ----- Recommendation header -----

    rate_a = arm_a["hit_rate_at_k"]
    rate_b = arm_b["hit_rate_at_k"]
    rate_c = arm_c["hit_rate_at_k"]
    mrr_a = arm_a["mrr_at_k"]
    mrr_b = arm_b["mrr_at_k"]
    mrr_c = arm_c["mrr_at_k"]

    # Decide a recommendation based on the data. Honest-read shape:
    # - If ColBERT-on (B) beats ColBERT-off (A) by ≥5pp on hit-rate, ship ColBERT.
    # - If alpha=0.3 (C) beats alpha=0 (B) by ≥3pp, ship with alpha=0.3.
    # - If C regresses vs B, don't ship recency (alpha=0).
    # - If both arms regress vs A, don't ship the colbert extra by default.
    delta_b_a = rate_b - rate_a
    delta_c_b = rate_c - rate_b

    if delta_b_a >= 0.05 and delta_c_b > 0:
        rec = "ship ColBERT + recency (alpha=0.3)"
    elif delta_b_a >= 0.05 and delta_c_b <= 0:
        rec = "ship ColBERT with alpha=0; recency does not help on this probe set"
    elif delta_b_a >= 0.0 and delta_c_b > 0.03:
        rec = "ship ColBERT + recency — ColBERT itself is neutral but recency lifts it"
    elif delta_b_a < -0.05:
        rec = "don't ship — ColBERT regresses vs the legacy weighted-sum path"
    elif abs(delta_b_a) < 0.05 and abs(delta_c_b) < 0.03:
        rec = "hold — deltas are within noise on n={n}; widen probe set before deciding".format(n=probe_count)
    else:
        rec = "ship ColBERT (alpha=0); revisit recency with a larger probe set"

    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"**{rec}**")
    lines.append("")
    lines.append(
        f"Δ hit-rate@{k} (B − A) = {_delta_pp(rate_b, rate_a)};  "
        f"Δ hit-rate@{k} (C − B) = {_delta_pp(rate_c, rate_b)};  "
        f"Δ MRR@{k} (C − A) = {_delta(mrr_c, mrr_a)}."
    )
    lines.append("")

    # ----- Per-arm summary -----

    lines.append("## Per-arm summary")
    lines.append("")
    lines.append(
        f"| Arm | Channel / config | Hit-rate@{k} | MRR@{k} | Total runtime |"
    )
    lines.append("|---|---|---|---|---|")
    for a in arms:
        cfg = a["config"]
        cfg_str = (
            f"channel={cfg['channel']}, alpha={cfg['recency_fuse_alpha']}"
        )
        lines.append(
            f"| {a['arm']} | {cfg_str} | "
            f"{_pct(a['hit_rate_at_k'])} | {a['mrr_at_k']:.3f} | "
            f"{a['total_runtime_s']:.1f}s |"
        )
    lines.append("")
    lines.append(
        f"Pairwise deltas: B vs A = **{_delta_pp(rate_b, rate_a)}** hit-rate, "
        f"{_delta(mrr_b, mrr_a)} MRR. "
        f"C vs B = **{_delta_pp(rate_c, rate_b)}** hit-rate, "
        f"{_delta(mrr_c, mrr_b)} MRR."
    )
    lines.append("")

    # ----- Per-category -----

    lines.append(f"## Hit-rate@{k} by category")
    lines.append("")
    # Collect every category across arms (should match).
    cats: list[str] = []
    for cat in arm_a["hit_rate_by_category"]:
        if cat not in cats:
            cats.append(cat)
    lines.append(
        "| Category | n | A (no ColBERT) | B (alpha=0) | C (alpha={a}) | Δ B−A | Δ C−B |".format(
            a=alpha_c,
        )
    )
    lines.append("|---|---|---|---|---|---|---|")
    for cat in cats:
        ra = arm_a["hit_rate_by_category"][cat]
        rb = arm_b["hit_rate_by_category"][cat]
        rc = arm_c["hit_rate_by_category"][cat]
        lines.append(
            f"| {cat} | {ra['total']} | {_pct(ra['rate'])} | "
            f"{_pct(rb['rate'])} | {_pct(rc['rate'])} | "
            f"{_delta_pp(rb['rate'], ra['rate'])} | "
            f"{_delta_pp(rc['rate'], rb['rate'])} |"
        )
    lines.append("")

    # ----- Per-probe table (only probes where arms diverge) -----

    lines.append("## Per-probe outcomes — probes where arms diverge")
    lines.append("")
    lines.append(
        "Only probes where rank or hit-status differs across at least "
        "one pair of arms are listed; identical-across-all-arms probes "
        "are folded into the per-arm totals."
    )
    lines.append("")
    lines.append(
        "| # | Cat | Query | A rank | B rank | C rank | Expected |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    rows = 0
    by_id_a = {o["probe_id"]: o for o in arm_a["outcomes"]}
    by_id_b = {o["probe_id"]: o for o in arm_b["outcomes"]}
    by_id_c = {o["probe_id"]: o for o in arm_c["outcomes"]}
    all_ids = sorted(by_id_a)
    for pid in all_ids:
        oa, ob, oc = by_id_a[pid], by_id_b[pid], by_id_c[pid]
        ranks = (oa["hit_rank"], ob["hit_rank"], oc["hit_rank"])
        if len(set(ranks)) == 1:
            continue
        rows += 1
        q = oa["query"].replace("|", "\\|")
        if len(q) > 60:
            q = q[:57] + "..."
        exp = oa["expected_paths"][0]
        if len(exp) > 40:
            exp = exp[:37] + "..."

        def _fmt(r):
            return f"#{r}" if r else "—"

        lines.append(
            f"| {pid} | {oa['category'][:3]} | `{q}` | {_fmt(ranks[0])} | "
            f"{_fmt(ranks[1])} | {_fmt(ranks[2])} | `{exp}` |"
        )
    if rows == 0:
        lines.append("| _(none — all arms agreed on every probe)_ |")
    lines.append("")
    lines.append(f"Total divergent probes: {rows}/{probe_count}.")
    lines.append("")

    # ----- Honest read -----

    lines.append("## Honest read")
    lines.append("")
    # Statistical: a binomial test on n=49 — a 1-probe flip = ~2pp.
    # Without computing exact p-values, just be explicit about the n.
    lines.append(
        f"**Sample size:** n={probe_count}. A single probe flip is "
        f"{100.0 / probe_count:.1f}pp; treating any |Δ| smaller than "
        f"~6pp as inside noise is reasonable for this dataset. The "
        "deltas below are pre-statistical — read them as effect sizes, "
        "not as confidence intervals."
    )
    lines.append("")
    lines.append(
        "**Does ColBERT win?** "
        f"Arm B (PR #184 as shipped) lands at {_pct(rate_b)} hit-rate@{k} "
        f"vs Arm A (legacy weighted-sum) at {_pct(rate_a)} — "
        f"Δ {_delta_pp(rate_b, rate_a)}. "
    )
    if delta_b_a >= 0.10:
        lines.append("That's a real, material lift on this probe set.")
    elif delta_b_a >= 0.05:
        lines.append("That's a meaningful lift on this probe set, but small enough that confirmation on a wider corpus would be valuable.")
    elif delta_b_a >= 0.0:
        lines.append("That's neutral — within noise on n={n}. ColBERT is not regressing, but it's not paying for its install cost on this probe set either.".format(n=probe_count))
    else:
        lines.append("That's a regression — ColBERT hurts retrieval on this probe set. Worth investigating before any rollout.")
    lines.append("")
    lines.append(
        "**Does recency help?** "
        f"Arm C (alpha={alpha_c}) lands at {_pct(rate_c)} hit-rate@{k} vs Arm B at "
        f"{_pct(rate_b)} — Δ {_delta_pp(rate_c, rate_b)}. "
    )
    if delta_c_b >= 0.05:
        lines.append(
            f"alpha={alpha_c} measurably lifts hit-rate; the recency "
            "nudge is paying its keep."
        )
    elif delta_c_b > 0.0:
        lines.append(
            f"alpha={alpha_c} shows a small positive effect — likely "
            "within noise but pointing in the right direction. A "
            "sweep on alpha or a larger probe set would confirm."
        )
    elif delta_c_b == 0.0:
        lines.append(
            f"alpha={alpha_c} is a no-op on hit-rate at this k — the "
            "multiplier reorders within the top-{0} but does not "
            "promote any new probe across the cutoff.".format(k)
        )
    else:
        lines.append(
            f"alpha={alpha_c} regresses hit-rate. Recency is fighting "
            "content signal more than it helps; ship alpha=0 or "
            "tune alpha downward."
        )
    lines.append("")
    lines.append(
        "**Per-category read:** check the `colbert-favorable` row in "
        "the by-category table — that's where ColBERT's predicted "
        "advantage on rare-token queries should be most visible. If "
        "the lift there is similar to (or smaller than) the lift on "
        "`path-citation` probes, ColBERT isn't doing what we predicted "
        "and the recommendation should de-emphasize the rare-token "
        "argument when justifying the install cost."
    )
    lines.append("")

    # ----- Caveats -----

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        f"- **Strict path-substring match.** A probe scores a hit only "
        f"if any `expected_paths` substring appears in any top-{k} "
        f"returned path. Some probes have multiple acceptable targets "
        f"(e.g. probe 27: `mimir/skills/heartbeat`, `skills/heartbeat`, "
        f"or `50-heartbeat-patterns.md`); a single returned path that "
        f"contains any of those counts."
    )
    lines.append("")
    lines.append(
        "- **Same SQLite candidate pool for all three arms.** Arm A "
        "and Arms B/C share `<home>/.mimir/index.db` for the BM25 + "
        "dense channel inputs; only the fusion path differs. Any "
        "index-quality bug surfaces in all three arms equally."
    )
    lines.append("")
    lines.append(
        "- **alpha=0.3 was chosen, not tuned.** The brief explicitly "
        "specced a single hyperparameter for this spawn. A sweep "
        "(0.0, 0.1, 0.2, 0.3, 0.5, 1.0) would tell us whether 0.3 is "
        "near the peak or whether a smaller / larger value wins — "
        "out of scope here."
    )
    lines.append("")
    lines.append(
        f"- **n={probe_count} is small.** Per-category sub-buckets "
        "are smaller still. Read 1-2pp category-level deltas as "
        "directional, not as statistically significant."
    )
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", type=Path,
                   default=_HERE / "results.json")
    p.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = p.parse_args()
    doc = json.loads(args.results.read_text())
    _render(doc, args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
