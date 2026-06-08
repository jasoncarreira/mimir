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
            return load_turns_corpus(home, split=split, limit=args.limit)
        src = f"REAL in-home saga_session_end turns ({home}/logs/turns.jsonl, limit {args.limit})"
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
    cand_evals = _eval_split(extract_fn, best[COMPONENT_SYSTEM], holdout, baseline_counts)
    base_agg = metrics.aggregate(base_evals)
    cand_agg = metrics.aggregate(cand_evals)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "candidate_system.txt").write_text(best[COMPONENT_SYSTEM], encoding="utf-8")
    report = {
        "max_metric_calls": args.max_metric_calls,
        "baseline_holdout": base_agg,
        "candidate_holdout": cand_agg,
        "delta_mean_score": cand_agg.get("mean_score", 0.0) - base_agg.get("mean_score", 0.0),
    }
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
