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
    from msam.core import hybrid_retrieve, mark_contributions, resolve_contradictions_to_supersedes
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

            # P1: consolidate clusters into observations before retrieval,
            # so the observation_bonus has material to boost.
            from msam.config import get_config
            _c = get_config()
            t_consolidate = 0.0
            clusters_consolidated = 0
            if _c('consolidation', 'enabled', False):
                from msam.consolidation import ConsolidationEngine
                t0 = time.time()
                try:
                    cresult = ConsolidationEngine().consolidate()
                    clusters_consolidated = cresult.get("clusters_consolidated", 0)
                except Exception as ce:
                    import traceback as _tb
                    _tb.print_exc()
                    print(f"  consolidation error on {qid}: {ce}")
                t_consolidate = time.time() - t0

            # P4-bench: detect contradictions among raw atoms and write
            # supersedes edges so hybrid_retrieve can demote stale evidence.
            # Targets the knowledge-update subtype (user changed their mind).
            t_supersede = 0.0
            supersedes_written = 0
            if _c('retrieval', 'enable_supersedes_resolution', True):
                t0 = time.time()
                try:
                    sres = resolve_contradictions_to_supersedes(
                        threshold=_c('retrieval', 'supersedes_resolution_threshold', 0.85),
                    )
                    supersedes_written = sres.get("supersedes_written", 0)
                except Exception as se:
                    print(f"  supersedes resolution error on {qid}: {se}")
                t_supersede = time.time() - t0

            t0 = time.time()
            from datetime import datetime, timezone
            try:
                ref_date = datetime.strptime(q["question_date"], "%Y/%m/%d (%a) %H:%M").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                ref_date = None
            two_tier = bool(_c('retrieval', 'two_tier_enabled', False))
            retrieved = hybrid_retrieve(
                q["question"],
                mode="task",
                top_k=RETRIEVAL_TOP_K,
                reference_date=ref_date,
                two_tier=two_tier,
            )
            t_retrieve = time.time() - t0

            if two_tier:
                n_obs = len(retrieved.get("observations", []))
                n_raw = len(retrieved.get("raws", []))
                n_retrieved = n_obs + n_raw
            else:
                n_obs = 0
                n_raw = len(retrieved)
                n_retrieved = n_raw

            t0 = time.time()
            reader = read(q["question"], q["question_date"], retrieved)
            t_read = time.time() - t0

            record = {"question_id": qid, "hypothesis": reader["hypothesis"]}
            out_f.write(json.dumps(record) + "\n")

            # P10: contribution tracking for diagnostics. Logs which retrieved
            # atoms ended up in the response (heuristic phrase/keyword overlap)
            # and populates co_retrieval pairs. Doesn't affect this question's
            # retrieval (per-question DB), but gives us per-question_type
            # contribution_rate and turns the feedback loop into a real signal
            # if we ever switch to a shared-DB run.
            retrieved_ids: list[str] = []
            if two_tier:
                for o in retrieved.get("observations", []) or []:
                    if o.get("id"):
                        retrieved_ids.append(o["id"])
                for r in retrieved.get("raws", []) or []:
                    if r.get("id"):
                        retrieved_ids.append(r["id"])
            else:
                for r in retrieved or []:
                    if r.get("id"):
                        retrieved_ids.append(r["id"])

            contribution_rate = None
            n_contributed = None
            if retrieved_ids and _c('benchmark', 'enable_mark_contributions', True):
                try:
                    contrib = mark_contributions(retrieved_ids, reader["hypothesis"])
                    contribution_rate = contrib.get("contribution_rate")
                    n_contributed = contrib.get("contributed")
                except Exception:
                    pass

            met_f.write(json.dumps({
                "question_id": qid,
                "question_type": q["question_type"],
                "n_atoms_ingested": stats["ingested"],
                "n_session_boundaries": stats.get("session_boundaries", 0),
                "n_observations": n_obs,
                "n_raws": n_raw,
                "n_atoms_retrieved": n_retrieved,
                "n_contributed": n_contributed,
                "contribution_rate": contribution_rate,
                "supersedes_written": supersedes_written,
                "ingest_s": round(t_ingest, 2),
                "consolidate_s": round(t_consolidate, 2),
                "supersede_s": round(t_supersede, 2),
                "clusters_consolidated": clusters_consolidated,
                "retrieve_s": round(t_retrieve, 2),
                "read_s": round(t_read, 2),
                "reader_prompt_tokens": reader.get("reader_prompt_tokens"),
                "reader_completion_tokens": reader.get("reader_completion_tokens"),
            }) + "\n")

            n_processed += 1
            elapsed = time.time() - t_start
            atoms_out = f"{n_obs}obs+{n_raw}raws" if two_tier else f"{n_raw}"
            print(
                f"[{i+1}/{len(dataset)}] {qid} ({q['question_type']}) "
                f"ingest={t_ingest:.1f}s cons={t_consolidate:.1f}s(n={clusters_consolidated}) "
                f"super={t_supersede:.1f}s(s={supersedes_written}) "
                f"retrieve={t_retrieve:.1f}s read={t_read:.1f}s "
                f"atoms={stats['ingested']}/{atoms_out} "
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
