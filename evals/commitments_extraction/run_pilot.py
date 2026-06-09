"""Runner for the commitments-extraction GEPA pilot (chainlink #404, Path A).

Two modes:
  --baseline   Score the CURRENT (baseline) system prompt on the holdout and
               print the aggregate + per-example ASI. Run this FIRST to confirm
               the evaluator produces sane, actionable feedback before spending
               model budget on optimization (the GEPA skill's gate). Cheap-ish:
               one extraction call per example.
  (default)    Compute baseline counts, run gepa.optimize on the train split,
               evaluate the best candidate vs baseline on the HELD-OUT split,
               and write a decision record + the candidate prompt to --out.
               Never edits mimir/commitments/extractor.py — adoption is a
               separate, reviewed PR.

Run on a deployment with a configured model + the gepa extra (e.g. mimirbot):
    uv run python -m evals.commitments_extraction.run_pilot --baseline
    uv run python -m evals.commitments_extraction.run_pilot --max-metric-calls 60
    uv run python -m evals.commitments_extraction.run_pilot \
        --focus-artifact-rich --focus-low-id-coverage --max-metric-calls 200

Every metric call runs the extractor against the configured model, so
--max-metric-calls is the cost knob. Start small.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from . import metrics
from .adapter import (
    COMPONENT_SYSTEM,
    CommitmentsAdapter,
    load_corpus,
    load_turns_corpus,
    make_extract_fn,
)


def _counts(extract_fn, system_prompt, examples) -> dict[str, int]:
    async def _run():
        results = await asyncio.gather(
            *[extract_fn(system_prompt, ex.source_text) for ex in examples]
        )
        return {ex.id: len(texts) for ex, texts in zip(examples, results)}

    return asyncio.run(_run())


def _eval_split(extract_fn, system_prompt, examples, baseline_counts):
    async def _run():
        return await asyncio.gather(
            *[extract_fn(system_prompt, ex.source_text) for ex in examples]
        )

    texts_per = asyncio.run(_run())
    return [
        metrics.score_extraction(
            ex.source_text, texts, baseline_count=baseline_counts.get(ex.id, len(texts))
        )
        for ex, texts in zip(examples, texts_per)
    ]



def _seed_retained(seed: dict[str, str], best: dict[str, str]) -> bool:
    """True when GEPA kept the original prompt rather than proposing adoptable text."""
    return best.get(COMPONENT_SYSTEM, "") == seed.get(COMPONENT_SYSTEM, "")


def _prioritize_low_id_coverage(examples, baseline_evals):
    """Order training examples so #407's artifact-id misses surface first.

    GEPA's reflective data is built from evaluated examples. Sorting does not
    change the holdout gate, but it makes the targeted pass spend early
    reflection on examples with source artifact ids whose baseline extraction
    dropped those ids. Examples without source ids stay last because they cannot
    teach artifact-id retention.
    """
    eval_by_id = {ex.id: ev for ex, ev in zip(examples, baseline_evals)}

    def _key(ex):
        ev = eval_by_id.get(ex.id)
        if ev is None:
            return (1, 1.0, 1.0, ex.id)
        has_source_ids = bool(ev.source_ids)
        return (0 if has_source_ids else 1, ev.coverage, ev.score, ex.id)

    return sorted(examples, key=_key)


def _report(max_metric_calls, base_agg, cand_agg=None, *, seed_retained: bool = False):
    """Build the JSON decision record without implying a seed-retained run won."""
    report = {
        "max_metric_calls": max_metric_calls,
        "seed_retained": seed_retained,
        "baseline_holdout": base_agg,
    }
    if seed_retained:
        report.update(
            {
                "candidate_holdout": None,
                "delta_mean_score": None,
                "recommendation": "no-go: GEPA retained the seed; no candidate to adopt",
            }
        )
    else:
        cand_agg = cand_agg or {}
        report.update(
            {
                "candidate_holdout": cand_agg,
                "delta_mean_score": cand_agg.get("mean_score", 0.0)
                - base_agg.get("mean_score", 0.0),
                "recommendation": "review candidate: adopt only if the holdout gain is real self-containment",
            }
        )
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="GEPA pilot: commitments-extraction self-containment (chainlink #404)"
    )
    ap.add_argument(
        "--baseline", action="store_true",
        help="Score the baseline prompt only and print ASI. No optimization, no budget spend on search.",
    )
    ap.add_argument(
        "--max-metric-calls", type=int, default=60,
        help="gepa optimization budget cap (default 60). Each call = one extractor model run.",
    )
    ap.add_argument("--model", default=None, help="Override the extractor (task) model name.")
    ap.add_argument(
        "--home", type=Path, default=None,
        help="Agent home to read REAL saga_session_end turns from "
             "(<home>/logs/turns.jsonl). Defaults to $MIMIR_HOME. Real session "
             "text is read at run time and NEVER committed.",
    )
    ap.add_argument(
        "--synthetic", action="store_true",
        help="Force the committed synthetic fixture instead of in-home real turns.",
    )
    ap.add_argument(
        "--limit", type=int, default=40,
        help="Max real turns to sample (most recent). Default 40.",
    )
    ap.add_argument(
        "--focus-artifact-rich",
        action="store_true",
        help="For real-turn corpora, sample turns with artifact ids first instead of most-recent only.",
    )
    ap.add_argument(
        "--focus-low-id-coverage",
        action="store_true",
        help="After baseline scoring, order train examples by low artifact-id coverage for #407.",
    )
    ap.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "pilot_output",
        help="Directory for the candidate prompt + decision-record report.",
    )
    args = ap.parse_args(argv)

    from mimir.commitments.extractor import EXTRACTION_SYSTEM

    extract_fn = make_extract_fn(model=args.model)

    # Corpus source: REAL in-home saga_session_end turns by default (privacy:
    # never committed), synthetic committed fixture only as a fallback/offline.
    home = args.home or (Path(os.environ["MIMIR_HOME"]) if os.environ.get("MIMIR_HOME") else None)
    use_real = (not args.synthetic) and home is not None and (home / "logs" / "turns.jsonl").is_file()
    if use_real:
        def _load(split):
            return load_turns_corpus(
                home, split=split, limit=args.limit, focus_artifact_rich=args.focus_artifact_rich
            )
        focus = ", artifact-rich focused" if args.focus_artifact_rich else ""
        src = (
            f"REAL in-home saga_session_end turns "
            f"({home}/logs/turns.jsonl, limit {args.limit}{focus})"
        )
    else:
        def _load(split):
            return load_corpus(split=split)
        why = "--synthetic set" if args.synthetic else "no --home/$MIMIR_HOME turns.jsonl found"
        src = f"SYNTHETIC committed fixture ({why})"

    train = _load("train")
    holdout = _load("holdout")
    all_ex = train + holdout

    print(f"[pilot] corpus source: {src}")
    print(f"[pilot] corpus: {len(train)} train, {len(holdout)} holdout")
    print("[pilot] computing baseline extraction counts (volume anchor)...")
    baseline_counts = _counts(extract_fn, EXTRACTION_SYSTEM, all_ex)

    if args.baseline:
        evals = _eval_split(extract_fn, EXTRACTION_SYSTEM, holdout, baseline_counts)
        print("\n=== BASELINE on holdout ===")
        print(json.dumps(metrics.aggregate(evals), indent=2))
        for ex, ev in zip(holdout, evals):
            print(f"\n--- {ex.id} (score {ev.score:.2f}) ---\n{ev.asi}")
        print(
            "\n[pilot] Verify above: does each ASI correctly name real over-compression / "
            "hallucination / missing-id issues? If not, fix the evaluator before optimizing."
        )
        return 0

    # ── Optimization pass ────────────────────────────────────────────
    import gepa

    from mimir.gepa_support import reflection_lm_from_config

    if args.focus_low_id_coverage:
        print("[pilot] scoring baseline train split to prioritize low-id-coverage examples...")
        train_evals = _eval_split(extract_fn, EXTRACTION_SYSTEM, train, baseline_counts)
        train = _prioritize_low_id_coverage(train, train_evals)

    adapter = CommitmentsAdapter(all_ex, baseline_counts, extract_fn)
    seed = {COMPONENT_SYSTEM: EXTRACTION_SYSTEM}
    print(f"[pilot] gepa.optimize (max_metric_calls={args.max_metric_calls})...")
    result = gepa.optimize(
        seed_candidate=seed,
        trainset=train,
        valset=holdout,
        adapter=adapter,
        reflection_lm=reflection_lm_from_config(),
        max_metric_calls=args.max_metric_calls,
    )
    best = result.best_candidate

    base_evals = _eval_split(extract_fn, EXTRACTION_SYSTEM, holdout, baseline_counts)
    base_agg = metrics.aggregate(base_evals)

    args.out.mkdir(parents=True, exist_ok=True)
    if _seed_retained(seed, best):
        report = _report(args.max_metric_calls, base_agg, seed_retained=True)
        (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

        print("\n=== DECISION RECORD (holdout) ===")
        print(json.dumps(report, indent=2))
        print(
            "\n[pilot] GEPA retained the seed prompt. No candidate prompt was written; "
            "this is an explicit no-go, not a baseline-vs-candidate win."
        )
        return 0

    cand_evals = _eval_split(extract_fn, best[COMPONENT_SYSTEM], holdout, baseline_counts)
    cand_agg = metrics.aggregate(cand_evals)
    (args.out / "candidate_system.txt").write_text(best[COMPONENT_SYSTEM], encoding="utf-8")
    report = _report(args.max_metric_calls, base_agg, cand_agg, seed_retained=False)
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== DECISION RECORD (holdout) ===")
    print(json.dumps(report, indent=2))
    print(f"\n[pilot] candidate prompt written to {args.out / 'candidate_system.txt'}")
    print(
        "[pilot] NOT applied. Adoption = a reviewed PR swapping EXTRACTION_SYSTEM, "
        "with a human spot-check that the score gain is real self-containment, not a gamed metric."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
