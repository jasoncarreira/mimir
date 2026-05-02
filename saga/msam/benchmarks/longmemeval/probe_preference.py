"""
One-shot probe: rerun the 30 single-session-preference questions with a
larger reader budget to see whether the 19/30 hitting the 512-token cap
is the dominant driver of the 23.3% preference accuracy on the v0 baseline.

Writes hypotheses_pref_probe_<tag>.jsonl in the same results dir as the
main run, so the upstream evaluator can grade it directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .config import BENCH_MSAM_CONFIG, DATASET_PATH, RESULTS_DIR, RETRIEVAL_TOP_K, WORK_DIR
from .env_loader import load_env


def _prepare_environment():
    repo_root = Path(__file__).resolve().parents[3]
    load_env(repo_root / ".env")
    os.environ["MSAM_CONFIG"] = str(BENCH_MSAM_CONFIG)
    os.environ["MSAM_DATA_DIR"] = str(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for var in ("OPENAI_API_KEY", "MINIMAX_API_KEY"):
        if not os.environ.get(var):
            sys.exit(f"{var} not set (check .env)")


def _switch_db(db_path: Path):
    import msam.core
    import msam.triples
    msam.core.DB_PATH = db_path
    msam.triples.DB_PATH = db_path
    from msam.core import get_db, run_migrations
    get_db().close()
    run_migrations()
    from msam.triples import init_triples_schema
    init_triples_schema()


def run(max_tokens: int, run_tag: str) -> Path:
    _prepare_environment()

    from msam.config import reload_config
    reload_config()
    import msam.embeddings
    msam.embeddings._provider_instance = None

    from msam.core import hybrid_retrieve
    from .ingest import ingest_question
    from .harness import build_prompt, call_minimax

    dataset = json.load(open(DATASET_PATH))
    pref = [q for q in dataset if q["question_type"] == "single-session-preference"]
    print(f"preference questions: {len(pref)}  reader max_tokens={max_tokens}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"hypotheses_pref_probe_{run_tag}.jsonl"
    out_f = open(out_path, "w", buffering=1)

    t_start = time.time()
    for i, q in enumerate(pref, 1):
        qid = q["question_id"]
        db_path = WORK_DIR / f"q_{qid}.db"
        try:
            if db_path.exists():
                db_path.unlink()
            _switch_db(db_path)

            t0 = time.time()
            ingest_question(q)
            t_ing = time.time() - t0

            t0 = time.time()
            atoms = hybrid_retrieve(q["question"], mode="task", top_k=RETRIEVAL_TOP_K)
            t_ret = time.time() - t0

            t0 = time.time()
            messages = build_prompt(q["question"], q["question_date"], atoms)
            result = call_minimax(messages, max_tokens=max_tokens)
            t_read = time.time() - t0

            out_f.write(json.dumps({
                "question_id": qid,
                "hypothesis": result["text"],
            }) + "\n")

            print(
                f"[{i}/{len(pref)}] {qid} ingest={t_ing:.1f}s retrieve={t_ret:.1f}s "
                f"read={t_read:.1f}s completion_tokens={result.get('completion_tokens')}",
                flush=True,
            )
        except Exception as e:
            traceback.print_exc()
            print(f"  ERROR on {qid}: {e}")
        finally:
            if db_path.exists():
                try:
                    db_path.unlink()
                except OSError:
                    pass

    out_f.close()
    print(f"\nDone in {(time.time()-t_start)/60:.1f} min. Hypotheses: {out_path}")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--run-tag", default="max1024")
    args = ap.parse_args()
    run(args.max_tokens, args.run_tag)


if __name__ == "__main__":
    main()
