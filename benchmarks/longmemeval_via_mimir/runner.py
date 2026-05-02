"""LongMemEval through mimir's BenchBridge dispatch path (v0.5 §3).

Boots mimir in-process with `_InProcessSaga` and a `BenchBridge` outbound,
ingests each LongMemEval question's haystack into a per-question saga
SQLite database, then drives the question through ``POST /event`` so
mimir's pre-message hook (saga query + contextual rewrite +
session_boundaries surfacing) and post-message hook (mark_contributions)
both fire. The agent's reply is captured from the BenchBridge stream
and written to a hypotheses JSONL the existing saga evaluator scores.

Usage:
    cd mimir
    uv run python -m benchmarks.longmemeval_via_mimir.runner \\
        --limit 5 \\
        --run-tag mimir_v0_5_smoke \\
        --output-dir results/longmemeval_via_mimir/

For a full 500-question run, drop ``--limit`` (and budget several hours
plus an OpenAI API key for the judge).

This file is intentionally a *driver*. Heavy lifting (atom storage,
retrieval, scoring) reuses saga's bench infrastructure. The reason for
this harness existing AT ALL is the cache/contextual-rewrite/credit-pass
interactions that are invisible to saga-direct benches — see V0.5.md §3.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .route import channel_id_for, question_to_event
from .score import evaluate_command, write_hypotheses_jsonl


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="benchmarks.longmemeval_via_mimir.runner",
        description=(
            "Run LongMemEval through mimir's BenchBridge dispatch path. "
            "Captures cache, contextual rewrite, and credit-pass effects "
            "that are invisible to saga-direct benches."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="number of questions to run; default=all 500",
    )
    p.add_argument(
        "--run-tag", required=True,
        help="identifier for the output filename (e.g. mimir_v0_5_smoke)",
    )
    p.add_argument(
        "--output-dir", default="results/longmemeval_via_mimir/",
        help="directory for hypothesis JSONL output",
    )
    p.add_argument(
        "--dataset-path", default=None,
        help="override the LongMemEval dataset JSON path (defaults to "
             "saga.benchmarks.longmemeval.config.DATASET_PATH)",
    )
    p.add_argument(
        "--keep-dbs", action="store_true",
        help="keep per-question SQLite databases after the run "
             "(useful for offline inspection)",
    )
    p.add_argument(
        "--mimir-home", default=None,
        help="MIMIR_HOME for the bench agent (defaults to a per-run "
             "tmpdir under the output dir)",
    )
    return p.parse_args(argv)


def _resolve_dataset(args: argparse.Namespace) -> Path:
    if args.dataset_path:
        return Path(args.dataset_path)
    from saga.benchmarks.longmemeval.config import DATASET_PATH
    return DATASET_PATH


def _switch_saga_db(db_path: Path) -> None:
    """Point the in-process saga at a fresh SQLite file for this question.

    Mirrors saga.benchmarks.longmemeval.run_eval._switch_db; saga's bench
    tooling is the source of truth for the per-question DB lifecycle. We
    repeat it here because the integration runner doesn't go through
    run_eval — mimir's BenchBridge owns the dispatch loop, and it would
    be confusing to import the saga runner just for this one helper."""
    import saga.core
    import saga.triples
    saga.core.DB_PATH = db_path
    saga.triples.DB_PATH = db_path
    if db_path.exists():
        db_path.unlink()
    from saga.core import get_db, run_migrations
    conn = get_db()
    conn.close()
    run_migrations()
    from saga.triples import init_triples_schema
    init_triples_schema()


async def _run_one_question(
    *,
    question: dict[str, Any],
    dispatcher: Any,
    bench_bridge: Any,
    bench_stream: io.StringIO,
    aiohttp_app: Any,
) -> dict[str, Any] | None:
    """Drive a single LongMemEval question through mimir's dispatcher.

    Returns the hypothesis record `{"question_id", "hypothesis"}` or None
    on failure (logged + skipped).
    """
    from aiohttp.test_utils import TestClient, TestServer
    from saga.benchmarks.longmemeval.ingest import ingest_question

    qid = question["question_id"]

    # Per-question saga DB (replicates saga's bench isolation).
    work_dir = Path(os.environ.get("SAGA_DATA_DIR", "."))
    db_path = work_dir / f"q_{qid}.db"
    _switch_saga_db(db_path)
    ingest_question(question)

    # BenchBridge captures outbound to the supplied stream.
    pos_before = bench_stream.tell()

    async with TestClient(TestServer(aiohttp_app)) as client:
        body = question_to_event(question)
        resp = await client.post("/event", json=body)
        if resp.status != 200:
            return None
        await dispatcher.drain()

    bench_stream.seek(pos_before)
    new_output = bench_stream.read()
    hypothesis = _extract_hypothesis(new_output, qid)
    if hypothesis is None:
        return None
    return {"question_id": qid, "hypothesis": hypothesis}


def _extract_hypothesis(stream_text: str, question_id: str) -> str | None:
    """BenchBridge writes lines like::

        [mimir:bench send_message channel=bench-<qid> msg_id=<m>] <text>

    We capture *every* outbound line for this channel and concatenate
    them — agents sometimes send multi-line answers as separate messages.
    """
    needle = f"channel={channel_id_for(question_id)} "
    pieces: list[str] = []
    for line in stream_text.splitlines():
        if needle not in line:
            continue
        if "send_message_attachments" in line:
            continue
        marker = "] "
        idx = line.find(marker)
        if idx < 0:
            continue
        pieces.append(line[idx + len(marker):])
    if not pieces:
        return None
    return "\n".join(pieces).strip()


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    dataset_path = _resolve_dataset(args)
    if not dataset_path.exists():
        print(f"dataset not found: {dataset_path}", file=sys.stderr)
        return 2
    dataset = json.loads(dataset_path.read_text())
    if args.limit:
        dataset = dataset[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    hypotheses_path = output_dir / f"hypotheses_{args.run_tag}.jsonl"

    # Mimir home for this run.
    if args.mimir_home:
        home = Path(args.mimir_home)
    else:
        home = output_dir / f"mimir_home_{args.run_tag}"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["MIMIR_HOME"] = str(home)
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)

    # Build the bench app.
    from mimir.cli import setup_home as _setup_home
    _setup_home(home)
    from mimir.config import Config
    cfg = Config.from_env()
    cfg = replace(cfg, home=home)

    from mimir import server as mimir_server
    bench_stream = io.StringIO()

    app = mimir_server.build_app(cfg)
    dispatcher = app["dispatcher"]
    # Re-target BenchBridge at our StringIO so we can scrape the agent's
    # outbound. The default BenchBridge writes to sys.stdout (which the
    # external runner harness scrapes); for in-process bench we capture
    # to memory.
    channels = app["channels"]
    bench_bridge = next(
        (b for b in channels.bridges if getattr(b, "name", None) == "bench"),
        None,
    )
    assert bench_bridge is not None, "BenchBridge missing — server.build_app changed?"
    bench_bridge.stream = bench_stream

    written: list[dict] = []
    failed: list[str] = []
    for q in dataset:
        try:
            rec = await _run_one_question(
                question=q,
                dispatcher=dispatcher,
                bench_bridge=bench_bridge,
                bench_stream=bench_stream,
                aiohttp_app=app,
            )
        except Exception as exc:  # noqa: BLE001 — keep going on per-question crashes
            print(f"  question {q['question_id']} crashed: {exc}", file=sys.stderr)
            failed.append(q["question_id"])
            continue

        if rec is None:
            failed.append(q["question_id"])
            continue
        written.append(rec)

    n = write_hypotheses_jsonl(hypotheses_path, written)
    print(f"wrote {n} hypotheses to {hypotheses_path}")
    if failed:
        print(f"  {len(failed)} questions failed (no hypothesis captured)")
    print(f"to score, run:\n  {evaluate_command(hypotheses_path, dataset_path)}")
    return 0 if not failed else 1


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
