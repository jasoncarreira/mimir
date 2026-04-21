"""
LongMemEval benchmark runner for MSAM.

Usage:
    python -m msam.benchmarks.longmemeval.run_eval --limit 10
    python -m msam.benchmarks.longmemeval.run_eval            # full 500

Output: results/longmemeval/hypotheses_<run_tag>.jsonl
        one JSON object per line: {"question_id": ..., "hypothesis": ...}

Pipe to upstream evaluator:
    cd external/longmemeval/src/evaluation
    python evaluate_qa.py gpt-4o \
        ../../../../results/longmemeval/hypotheses_<run>.jsonl \
        ../../../../data/longmemeval/longmemeval_s_cleaned.json
    python print_qa_metrics.py gpt-4o \
        ../../../../results/longmemeval/hypotheses_<run>.jsonl.eval-results-gpt-4o
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

from .config import (
    BENCH_MSAM_CONFIG,
    DATASET_PATH,
    RESULTS_DIR,
    RETRIEVAL_TOP_K,
    WORK_DIR,
)
from .env_loader import load_env


def _prepare_environment(work_dir: Path):
    """Set env vars MSAM reads at import time."""
    repo_root = Path(__file__).resolve().parents[3]
    load_env(repo_root / ".env")
    os.environ["MSAM_CONFIG"] = str(BENCH_MSAM_CONFIG)
    os.environ["MSAM_DATA_DIR"] = str(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY not set (check .env)")


def _switch_db(db_path: Path):
    """Point MSAM at a new SQLite file for this question."""
    import msam.core
    import msam.triples

    msam.core.DB_PATH = db_path
    msam.triples.DB_PATH = db_path
    # Fresh schema.
    from msam.core import get_db, run_migrations
    conn = get_db()
    conn.close()
    run_migrations()
    from msam.triples import init_triples_schema
    init_triples_schema()


def _load_done(output_path: Path) -> set[str]:
    done: set[str] = set()
    if not output_path.exists():
        return done
    for line in output_path.read_text().splitlines():
        try:
            done.add(json.loads(line)["question_id"])
        except Exception:
            continue
    return done


def _format_atom_for_reader(atom: dict) -> dict:
    return {
        "content": atom.get("content", ""),
        "stream": atom.get("stream", ""),
        "score": atom.get("_combined_score", atom.get("_activation", 0)),
    }


def run(limit: int | None, run_tag: str, resume: bool, keep_dbs: bool) -> Path:
    _prepare_environment(WORK_DIR)

    # Import AFTER env is set so config resolves to the benchmark toml.
    from msam.config import reload_config
    reload_config()
    # Reset the embedding provider singleton in case something constructed it
    # before reload_config ran (msam/__init__.py eagerly imports core).
    import msam.embeddings
    msam.embeddings._provider_instance = None
    from msam.core import hybrid_retrieve
    from .ingest import ingest_question
    from .harness import read

    dataset = json.load(open(DATASET_PATH))
    if limit:
        dataset = dataset[:limit]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / f"hypotheses_{run_tag}.jsonl"
    metrics_path = RESULTS_DIR / f"metrics_{run_tag}.jsonl"
    done = _load_done(output_path) if resume else set()

    mode = "a" if resume and done else "w"
    out_f = open(output_path, mode, buffering=1)
    met_f = open(metrics_path, mode, buffering=1)

    t_start = time.time()
    n_processed = 0
    errors = 0

    for i, q in enumerate(dataset):
        qid = q["question_id"]
        if qid in done:
            continue

        db_path = WORK_DIR / f"q_{qid}.db"
        try:
            if db_path.exists():
                db_path.unlink()
            _switch_db(db_path)

            t0 = time.time()
            stats = ingest_question(q)
            t_ingest = time.time() - t0

            t0 = time.time()
            atoms = hybrid_retrieve(q["question"], mode="task", top_k=RETRIEVAL_TOP_K)
            t_retrieve = time.time() - t0

            t0 = time.time()
            reader = read(q["question"], q["question_date"], atoms)
            t_read = time.time() - t0

            record = {"question_id": qid, "hypothesis": reader["hypothesis"]}
            out_f.write(json.dumps(record) + "\n")

            met_f.write(json.dumps({
                "question_id": qid,
                "question_type": q["question_type"],
                "n_atoms_ingested": stats["ingested"],
                "n_atoms_retrieved": len(atoms),
                "ingest_s": round(t_ingest, 2),
                "retrieve_s": round(t_retrieve, 2),
                "read_s": round(t_read, 2),
                "reader_prompt_tokens": reader.get("reader_prompt_tokens"),
                "reader_completion_tokens": reader.get("reader_completion_tokens"),
            }) + "\n")

            n_processed += 1
            elapsed = time.time() - t_start
            print(
                f"[{i+1}/{len(dataset)}] {qid} ({q['question_type']}) "
                f"ingest={t_ingest:.1f}s retrieve={t_retrieve:.1f}s "
                f"read={t_read:.1f}s atoms={stats['ingested']}/{len(atoms)} "
                f"elapsed={elapsed:.0f}s",
                flush=True,
            )
        except Exception as e:
            errors += 1
            traceback.print_exc()
            met_f.write(json.dumps({"question_id": qid, "error": str(e)}) + "\n")
        finally:
            if not keep_dbs and db_path.exists():
                try:
                    db_path.unlink()
                except OSError:
                    pass

    out_f.close()
    met_f.close()

    print(
        f"\nDone. Processed {n_processed}, errors {errors}. "
        f"Total {(time.time()-t_start)/60:.1f} min. Hypotheses: {output_path}"
    )
    return output_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap number of questions")
    ap.add_argument("--run-tag", default="msam_baseline_v0", help="tag for output files")
    ap.add_argument("--resume", action="store_true", help="skip questions already in JSONL")
    ap.add_argument("--keep-dbs", action="store_true", help="don't delete per-question SQLite files")
    args = ap.parse_args()
    run(args.limit, args.run_tag, args.resume, args.keep_dbs)


if __name__ == "__main__":
    main()
