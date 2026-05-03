"""P33: empirical recalibration of confidence_sim_{high,medium,low} thresholds.

Computes cosine sim of each LongMemEval probe embedding against every
atom in its haystack. Buckets sims by `has_answer` flag (gold vs noise).
Finds crossover thresholds where:

- HIGH: precision ≥90% (only ≤10% of atoms above this are non-gold)
- MEDIUM: recall ≥80% (at least 80% of gold atoms clear this)
- LOW: noise floor (~5th percentile of gold sim — everything below this
  has essentially no gold atoms)

Output:
- A histogram of gold vs noise sim distributions, written to stdout.
- Recommended thresholds with their precision/recall trade-offs.
- Per-subtype breakdown so we can see if the thresholds need to vary.

Usage:
    set -a; source <repo>/.env; set +a
    PYTHONPATH=. python -m saga.benchmarks.longmemeval.p33_threshold_analysis
        [--sample-size N]   # sample N questions; default 100
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from .config import DATASET_PATH, BENCH_SAGA_CONFIG
from .env_loader import load_env


def _format_atom(date_iso: str, role: str, content: str) -> str:
    """Match the atom format used by ingest.iter_turns so embeddings see
    the same `[YYYY-MM-DD role] content` shape the bench produces."""
    date_tag = date_iso[:10] if date_iso else ""
    return f"[{date_tag} {role}] {content.strip()}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-size", type=int, default=100,
                    help="number of questions to analyze (default 100)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    # Load env + config so embed_query/embed_text use the bench's
    # OpenAI text-embedding-3-small endpoint.
    repo_root = Path(__file__).resolve().parents[3]
    load_env(repo_root / ".env")
    os.environ["SAGA_CONFIG"] = str(BENCH_SAGA_CONFIG)
    # Reload config so saga.embeddings picks up the bench config
    # (its module-top _cfg = get_config() captures whatever was active
    # at first import, which may have happened before this script ran).
    from saga.config import reload_config
    reload_config()

    from saga.embeddings import get_provider, embed_query
    # Force embeddings module to re-resolve its provider after reload.
    import saga.embeddings as _emb
    _emb._provider_instance = None

    print(f"Loading dataset from {DATASET_PATH}", file=sys.stderr)
    with open(DATASET_PATH) as f:
        data = json.load(f)

    random.seed(args.seed)
    sample = random.sample(data, min(args.sample_size, len(data)))
    print(f"Sampling {len(sample)} of {len(data)} questions", file=sys.stderr)

    provider = get_provider()
    gold_sims: list[float] = []
    noise_sims: list[float] = []
    by_subtype: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"gold": [], "noise": []}
    )

    for q_idx, q in enumerate(sample):
        qid = q.get("question_id", "?")
        qtype = q.get("question_type", "?")

        # Question embedding
        qvec = np.asarray(embed_query(q["question"]), dtype=np.float32)
        qvec = qvec / (np.linalg.norm(qvec) + 1e-12)

        # Build atoms with the ingest-style formatting and gold flag
        sessions = q.get("haystack_sessions", [])
        dates = q.get("haystack_dates", [])
        if not sessions:
            continue

        atom_texts: list[str] = []
        atom_gold: list[bool] = []
        for s_idx, s in enumerate(sessions):
            sdate = dates[s_idx] if s_idx < len(dates) else ""
            for turn in s:
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                atom_texts.append(_format_atom(sdate, turn.get("role", "user"), content))
                atom_gold.append(bool(turn.get("has_answer")))

        if not atom_texts:
            continue

        # Batch-embed atoms; on rate-limit failure skip this question
        # rather than crashing the whole run.
        try:
            if hasattr(provider, "batch_embed"):
                embs = provider.batch_embed(atom_texts, input_type="passage")
            else:
                embs = [provider.embed(t, input_type="passage") for t in atom_texts]
        except Exception as e:
            print(f"  skip {qid} ({qtype}): {type(e).__name__}: {str(e)[:80]}",
                  file=sys.stderr)
            continue

        amat = np.asarray(embs, dtype=np.float32)
        norms = np.linalg.norm(amat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        amat = amat / norms

        sims = amat @ qvec  # (N,)

        for sim, is_gold in zip(sims.tolist(), atom_gold):
            if is_gold:
                gold_sims.append(sim)
                by_subtype[qtype]["gold"].append(sim)
            else:
                noise_sims.append(sim)
                by_subtype[qtype]["noise"].append(sim)

        if (q_idx + 1) % 10 == 0:
            print(f"  processed {q_idx + 1}/{len(sample)} questions  "
                  f"(gold={len(gold_sims)}, noise={len(noise_sims)})",
                  file=sys.stderr)
        # Light throttle between questions — OpenAI rate-limits requests/sec,
        # not just tokens/sec. Sharing the key with a running bench means
        # we have to be polite.
        import time
        time.sleep(0.5)

    print()
    print(f"=== Aggregate distribution ({len(sample)} questions) ===")
    print(f"Gold atoms: {len(gold_sims)}  (avg {len(gold_sims)/len(sample):.2f} per question)")
    print(f"Noise atoms: {len(noise_sims)}")
    print()
    _print_distribution("Gold (has_answer=True)", gold_sims)
    _print_distribution("Noise (has_answer=False)", noise_sims)
    print()

    # Crossover analysis: for each candidate threshold, compute precision
    # (% of atoms above threshold that are gold) and recall (% of gold
    # atoms that clear threshold).
    print("=== Threshold sweep (atoms above threshold) ===")
    print(f"{'thr':>5} {'gold≥thr':>10} {'noise≥thr':>10} {'precision':>10} {'recall':>10}")
    g = np.asarray(gold_sims)
    n = np.asarray(noise_sims)
    for thr in [0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05, 0.00]:
        above_g = (g >= thr).sum()
        above_n = (n >= thr).sum()
        precision = above_g / max(above_g + above_n, 1)
        recall = above_g / max(len(g), 1)
        print(f"{thr:>5.2f} {above_g:>10d} {above_n:>10d} {precision:>10.3f} {recall:>10.3f}")
    print()

    # Pick thresholds: HIGH at smallest threshold with precision ≥ 0.20
    # (relaxed from 0.90 because gold atoms are diluted by topical-but-
    # non-answer atoms in the haystack — most haystack atoms are
    # distractors, so ANY precision above the noise rate is meaningful).
    # MEDIUM at largest threshold with recall ≥ 0.80. LOW at the
    # 5th-percentile gold sim.
    high_thr = _find_high_thr(g, n)
    medium_thr = _find_recall_thr(g, target_recall=0.80)
    low_thr = float(np.percentile(g, 5)) if len(g) else 0.0

    print("=== Recommended thresholds ===")
    print(f"  confidence_sim_high   = {high_thr:.2f}  (smallest threshold where precision exceeds 5x noise rate)")
    print(f"  confidence_sim_medium = {medium_thr:.2f}  (largest threshold where ≥80% of gold atoms clear it)")
    print(f"  confidence_sim_low    = {low_thr:.2f}  (5th percentile of gold sim — below this is essentially noise)")
    print()
    print("Current defaults: 0.45 / 0.30 / 0.15")
    print()

    # Per-subtype breakdown
    print("=== Per-subtype gold sim distribution ===")
    print(f"{'subtype':30s} {'n_gold':>8} {'p50':>6} {'p25':>6} {'p10':>6} {'p5':>6}")
    for st in sorted(by_subtype.keys()):
        gs = by_subtype[st]["gold"]
        if not gs:
            continue
        arr = np.asarray(gs)
        p50, p25, p10, p5 = (np.percentile(arr, p) for p in (50, 25, 10, 5))
        print(f"{st:30s} {len(gs):>8d} {p50:>6.3f} {p25:>6.3f} {p10:>6.3f} {p5:>6.3f}")


def _print_distribution(label: str, sims: list[float]):
    if not sims:
        print(f"{label}: (empty)")
        return
    arr = np.asarray(sims)
    print(f"{label}:  n={len(arr)}  "
          f"min={arr.min():.3f}  p5={np.percentile(arr, 5):.3f}  "
          f"p25={np.percentile(arr, 25):.3f}  median={np.median(arr):.3f}  "
          f"p75={np.percentile(arr, 75):.3f}  p95={np.percentile(arr, 95):.3f}  max={arr.max():.3f}")


def _find_high_thr(gold: np.ndarray, noise: np.ndarray) -> float:
    """Smallest threshold where gold/(gold+noise) >= 5x the base rate
    of gold/(gold+noise) at threshold 0. Captures 'this is meaningfully
    more likely to be gold than random'."""
    if len(gold) == 0 or len(noise) == 0:
        return 0.45
    base = len(gold) / (len(gold) + len(noise))
    target = base * 5
    for thr in np.linspace(0.0, 0.7, 71):
        ag = (gold >= thr).sum()
        an = (noise >= thr).sum()
        if ag == 0:
            break
        prec = ag / (ag + an)
        if prec >= target:
            return float(thr)
    return 0.45


def _find_recall_thr(gold: np.ndarray, target_recall: float = 0.80) -> float:
    """Largest threshold where at least target_recall fraction of gold
    atoms clear it."""
    if len(gold) == 0:
        return 0.30
    sorted_g = np.sort(gold)
    cutoff_idx = int(len(sorted_g) * (1 - target_recall))
    return float(sorted_g[cutoff_idx])


if __name__ == "__main__":
    main()
