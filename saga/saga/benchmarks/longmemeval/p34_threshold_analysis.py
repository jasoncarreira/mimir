"""P34: empirical recalibration of consolidation.similarity_threshold.

For a sample of LongMemEval haystacks, compute pairwise cosine sim
between every pair of atoms and histogram by threshold band. Surfaces:

- How many "real duplicate" pairs exist at each threshold
- Sample pairs at each band so we can manually eyeball whether they
  ARE topical duplicates worth consolidating
- Whether the Mimir-derived recommendation of 0.75 transfers to
  LongMemEval's conversation-style content (vs Mimir's news-feed style)

Usage:
    set -a; source /Users/jcarreira/projects/odin/msam/.env; set +a
    PYTHONPATH=. python -m saga.benchmarks.longmemeval.p34_threshold_analysis
        [--sample-size N]   # sample N questions; default 10 (smaller than P33 — pairwise O(n²))
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from .config import DATASET_PATH, BENCH_SAGA_CONFIG
from .env_loader import load_env


def _format_atom(date_iso: str, role: str, content: str) -> str:
    date_tag = date_iso[:10] if date_iso else ""
    return f"[{date_tag} {role}] {content.strip()}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-size", type=int, default=10,
                    help="number of questions to analyze (default 10)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-sample-pairs", type=int, default=5,
                    help="number of pair examples to surface per band")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    load_env(repo_root / ".env")
    os.environ["SAGA_CONFIG"] = str(BENCH_SAGA_CONFIG)
    from saga.config import reload_config
    reload_config()
    from saga.embeddings import get_provider
    import saga.embeddings as _emb
    _emb._provider_instance = None

    print(f"Loading dataset from {DATASET_PATH}", file=sys.stderr)
    with open(DATASET_PATH) as f:
        data = json.load(f)

    random.seed(args.seed)
    sample = random.sample(data, min(args.sample_size, len(data)))
    print(f"Sampling {len(sample)} of {len(data)} questions", file=sys.stderr)

    provider = get_provider()

    # Aggregate pair-similarity histogram across all sampled haystacks.
    # Per-question we'll compute the upper triangle (no self-pairs, no duplicates).
    all_pair_sims: list[float] = []
    sample_pairs_by_band: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    BANDS = [(0.85, 1.0), (0.80, 0.85), (0.75, 0.80), (0.70, 0.75), (0.65, 0.70)]
    per_q_stats: list[dict] = []

    for q_idx, q in enumerate(sample):
        qid = q.get("question_id", "?")
        sessions = q.get("haystack_sessions", [])
        dates = q.get("haystack_dates", [])
        if not sessions:
            continue

        atom_texts: list[str] = []
        # Only semantic atoms (assistant turns) get clustered in production —
        # match that to keep numbers comparable.
        for s_idx, s in enumerate(sessions):
            sdate = dates[s_idx] if s_idx < len(dates) else ""
            for turn in s:
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                if turn.get("role") != "assistant":
                    continue
                atom_texts.append(_format_atom(sdate, turn.get("role", "assistant"), content))

        if len(atom_texts) < 2:
            continue

        try:
            if hasattr(provider, "batch_embed"):
                embs = provider.batch_embed(atom_texts, input_type="passage")
            else:
                embs = [provider.embed(t, input_type="passage") for t in atom_texts]
        except Exception as e:
            print(f"  skip {qid}: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
            continue

        amat = np.asarray(embs, dtype=np.float32)
        norms = np.linalg.norm(amat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        amat = amat / norms

        # Pairwise cosine — full matrix, then upper triangle
        sim_matrix = amat @ amat.T
        N = sim_matrix.shape[0]
        ti, tj = np.triu_indices(N, k=1)
        upper = sim_matrix[ti, tj]
        all_pair_sims.extend(upper.tolist())

        per_q_stats.append({
            "qid": qid,
            "n_atoms": N,
            "n_pairs": len(upper),
            "max": float(upper.max()),
            "p99": float(np.percentile(upper, 99)),
            "p95": float(np.percentile(upper, 95)),
        })

        # Surface sample pairs per band
        for lo, hi in BANDS:
            in_band = np.where((upper >= lo) & (upper < hi))[0]
            random.shuffle(list(in_band))
            for k in in_band[:args.n_sample_pairs]:
                if len(sample_pairs_by_band[f"{lo:.2f}-{hi:.2f}"]) >= args.n_sample_pairs:
                    break
                i, j = ti[k], tj[k]
                sample_pairs_by_band[f"{lo:.2f}-{hi:.2f}"].append(
                    (float(upper[k]), atom_texts[i], atom_texts[j])
                )

        if (q_idx + 1) % 5 == 0:
            print(f"  processed {q_idx + 1}/{len(sample)} questions  "
                  f"(total pairs={len(all_pair_sims)})", file=sys.stderr)
        time.sleep(0.5)

    arr = np.asarray(all_pair_sims)
    if not len(arr):
        print("ERROR: no pair sims collected (all questions skipped?)", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"=== Aggregate pair-similarity distribution ===")
    print(f"Total pairs: {len(arr)}  ({len(per_q_stats)} questions × ~{len(arr)/max(len(per_q_stats),1):.0f} pairs each)")
    print(f"Stats: min={arr.min():.3f}  p25={np.percentile(arr, 25):.3f}  "
          f"median={np.median(arr):.3f}  p75={np.percentile(arr, 75):.3f}  "
          f"p95={np.percentile(arr, 95):.3f}  p99={np.percentile(arr, 99):.3f}  "
          f"p999={np.percentile(arr, 99.9):.3f}  max={arr.max():.3f}")
    print()

    print("=== Threshold sweep (pairs ≥ threshold across all sampled haystacks) ===")
    print(f"{'thr':>5} {'n_pairs':>10} {'pct_of_total':>14} {'avg_per_q':>10}")
    n_q = max(len(per_q_stats), 1)
    for thr in [0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50]:
        n = (arr >= thr).sum()
        pct = 100 * n / len(arr)
        per_q = n / n_q
        print(f"{thr:>5.2f} {n:>10d} {pct:>13.4f}% {per_q:>10.2f}")
    print()

    print("=== Sample pairs per band (manual inspection: are these duplicates?) ===")
    for band in [f"{lo:.2f}-{hi:.2f}" for lo, hi in BANDS]:
        pairs = sample_pairs_by_band.get(band, [])
        if not pairs:
            print(f"\n[{band}] (no pairs)")
            continue
        print(f"\n[{band}]  ({len(pairs)} examples)")
        for sim, a, b in pairs[:args.n_sample_pairs]:
            print(f"  sim={sim:.3f}")
            print(f"    A: {a[:120]}")
            print(f"    B: {b[:120]}")

    print()
    print("=== Per-question max-pair-sim distribution ===")
    maxes = [s["max"] for s in per_q_stats]
    p99s = [s["p99"] for s in per_q_stats]
    if maxes:
        print(f"Max pair sim per question:  min={min(maxes):.3f}  median={np.median(maxes):.3f}  max={max(maxes):.3f}")
        print(f"P99 pair sim per question: min={min(p99s):.3f}  median={np.median(p99s):.3f}  max={max(p99s):.3f}")


if __name__ == "__main__":
    main()
