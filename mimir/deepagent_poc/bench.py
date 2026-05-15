"""LongMemEval-S bench through the deepagents PoC.

Per-question lifecycle (mirrors via_mimir's runner_memory.py but via
the deepagents path):

  1. Fresh per-question MemoryClient
  2. Ingest haystack (saga embedding provider via _ingest_question)
  3. Consolidate
  4. Build a deepagent (model + tools + system prompt)
  5. run_turn(agent, ...) — pre-message → ainvoke → post-message
  6. Append hypothesis to JSONL (incremental, crash-survivable)

Compared to via_mimir bench:
  - No mimir agent loop (no ClientPool, no SDK lifecycle, no
    scheduler.yaml-to-strip, no algedonic/usage/self-state blocks
    in the prompt — clean deepagents prompt by construction).
  - turns.jsonl written through our adapter
  - Each question is a fresh deepagent (could share — singleton is
    thread-safe — but per-question fits the "fresh DB" bench model)

Usage:
    set -a; source .env; set +a
    uv run python -m mimir.deepagent_poc.bench --limit 10 \\
        --run-tag deepagents_poc_10q
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mimir.memory.client import MemoryClient
from .agent import make_agent
from .memory_tool import set_memory_client
from .turn_logger import TurnLogger
from .turn_runner import run_turn

from benchmarks.longmemeval_via_memory.runner import _ingest_question


DEFAULT_DATASET = Path(
    "/Users/jcarreira/projects/odin/mimir/saga/data/longmemeval/longmemeval_s_cleaned.json"
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LongMemEval-S bench through the deepagents PoC.",
    )
    p.add_argument("--limit", type=int, default=10,
                   help="Question cap (default 10)")
    p.add_argument("--run-tag", default="deepagents_poc",
                   help="Run identifier for output files")
    p.add_argument("--output-dir",
                   default="/Users/jcarreira/projects/odin/mimir/results/longmemeval_via_mimir",
                   help="Where hypotheses + runlog land")
    p.add_argument("--model", default="openai:gpt-5.4-nano",
                   help="Model spec passed to make_agent. See "
                        "mimir.deepagent_poc.agent.resolve_model.")
    p.add_argument("--dataset", default=None,
                   help="Override dataset path")
    return p.parse_args(argv)


async def run_one(
    question: dict,
    *,
    work_dir: Path,
    turn_logger: TurnLogger,
    hypotheses_handle,
    session_id: str,
    model_spec: str,
) -> dict:
    qid = question["question_id"]
    db_path = work_dir / f"q_{qid}.db"
    for suffix in ("", "-wal", "-shm"):
        f = db_path.with_suffix(db_path.suffix + suffix) if suffix else db_path
        if f.exists():
            f.unlink()

    client = MemoryClient(db_path=db_path, embedding_dim=None)
    set_memory_client(client)  # makes memory_query / memory_store tools work

    t0 = time.time()
    ingest_stats = await _ingest_question(client, question)
    t_ingest = time.time() - t0

    t0 = time.time()
    try:
        cresult = await client.consolidate() or {}
        n_clusters = (
            cresult.get("clusters_formed")
            or len(cresult.get("observations_emitted") or [])
            or 0
        )
    except Exception as exc:
        print(f"    consolidation failed for {qid}: {exc}", file=sys.stderr)
        n_clusters = 0
    t_consolidate = time.time() - t0

    # Per-question agent. The CompiledStateGraph is thread-safe and
    # could be shared, but per-question matches the bench's "isolated
    # workspaces" model and keeps memory_query's tool-state injection
    # straightforward (set_memory_client is called per-question).
    agent = make_agent(client, model=model_spec)
    saga_session_id = f"saga-bench-{qid}"
    extra_system = f"Today's date: {question.get('question_date','')}"

    t0 = time.time()
    outcome = await run_turn(
        agent,
        memory_client=client,
        question=question["question"],
        session_id=session_id,
        channel_id=f"bench-{qid}",
        saga_session_id=saga_session_id,
        turn_logger=turn_logger,
        reference_date=None,  # haystack date comes from agent's extra_system
    )
    t_agent = time.time() - t0

    record = {"question_id": qid, "hypothesis": outcome.output}
    hypotheses_handle.write(json.dumps(record) + "\n")
    hypotheses_handle.flush()

    n_credited = (
        len(outcome.post_message.atom_ids_credited)
        if outcome.post_message else 0
    )
    print(
        f"  [{qid}] ingest={t_ingest:.0f}s cons={t_consolidate:.0f}s "
        f"(n={n_clusters}) agent={t_agent:.0f}s "
        f"credited={n_credited} | hyp={outcome.output[:60]}…",
        file=sys.stderr,
    )

    return {
        "qid": qid,
        "ok": outcome.error is None,
        "ingest_s": t_ingest,
        "consolidate_s": t_consolidate,
        "agent_s": t_agent,
        "n_credited": n_credited,
        "error": outcome.error,
    }


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / f"deepagents_work_{args.run_tag}"
    work_dir.mkdir(parents=True, exist_ok=True)

    home = work_dir / "mimir_home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["MIMIR_HOME"] = str(home)
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)
    from benchmarks.longmemeval_via_mimir.runner import _write_bench_saga_toml
    _write_bench_saga_toml(home)
    os.environ["SAGA_CONFIG"] = str(home / "saga.toml")

    turns_path = work_dir / "turns.jsonl"
    if turns_path.exists():
        turns_path.unlink()
    turn_logger = TurnLogger(turns_path)
    session_id = f"deepagents-bench-{args.run_tag}-{int(time.time())}"

    hypotheses_path = output_dir / f"hypotheses_{args.run_tag}.jsonl"
    if hypotheses_path.exists():
        hypotheses_path.unlink()
    hypotheses_handle = hypotheses_path.open("a", encoding="utf-8")

    dataset_path = Path(args.dataset) if args.dataset else DEFAULT_DATASET
    if not dataset_path.exists():
        print(f"dataset not found: {dataset_path}", file=sys.stderr)
        return 2
    data = json.load(dataset_path.open())
    if args.limit:
        data = data[: args.limit]

    print(f"running {len(data)} questions through deepagents PoC "
          f"(model={args.model})", file=sys.stderr)
    print(f"  hypotheses → {hypotheses_path}", file=sys.stderr)
    print(f"  turns.jsonl → {turns_path}", file=sys.stderr)

    summary: list[dict] = []
    failed: list[str] = []
    for i, q in enumerate(data, 1):
        print(f"[{i}/{len(data)}] {q['question_id']}", file=sys.stderr)
        try:
            s = await run_one(
                q,
                work_dir=work_dir,
                turn_logger=turn_logger,
                hypotheses_handle=hypotheses_handle,
                session_id=session_id,
                model_spec=args.model,
            )
        except Exception as exc:
            print(f"  question {q['question_id']} crashed: {exc}",
                  file=sys.stderr)
            failed.append(q["question_id"])
            continue
        summary.append(s)

    hypotheses_handle.close()
    print(file=sys.stderr)
    print(f"wrote {len(summary)} hypotheses to {hypotheses_path}", file=sys.stderr)
    if failed:
        print(f"failed/skipped: {len(failed)}", file=sys.stderr)
        for fq in failed[:10]:
            print(f"  {fq}", file=sys.stderr)
    # Aggregate timing
    if summary:
        agent_times = [s["agent_s"] for s in summary]
        cons_times = [s["consolidate_s"] for s in summary]
        ing_times = [s["ingest_s"] for s in summary]
        credited = [s["n_credited"] for s in summary]
        print(file=sys.stderr)
        print(f"per-question avg: ingest={sum(ing_times)/len(ing_times):.0f}s "
              f"consolidate={sum(cons_times)/len(cons_times):.0f}s "
              f"agent={sum(agent_times)/len(agent_times):.0f}s "
              f"credited={sum(credited)/len(credited):.0f}",
              file=sys.stderr)
    return 0 if summary else 1


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
