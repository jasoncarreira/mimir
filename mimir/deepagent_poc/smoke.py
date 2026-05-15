"""End-to-end smoke for the deepagents PoC.

Runs 2 LongMemEval-S questions through:
  - Per-question MemoryClient (mimir.memory) ingests the haystack
  - Bench consolidation runs (so observations + triples exist)
  - The minimal deepagent invokes memory_query and answers
  - turn_logger writes a turns.jsonl record per question

Verifies:
  1. The pipeline runs without errors
  2. ``turns.jsonl`` lands at mimir's existing schema (TurnRecord)
  3. The agent's answer matches the LongMemEval gold (manual eyeball)
  4. Cache hit rate visible in usage_metadata

Usage:
    cd <worktree>
    uv run python -m mimir.deepagent_poc.smoke
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage

from mimir.memory.client import MemoryClient
from .agent import make_agent
from .turn_logger import (
    TurnLogger,
    TurnRecord,
    derive_result_fields,
    extract_turn_events,
    make_turn_id,
    truncate_input,
)

# Reuse the proven ingest helper from the existing via_memory bench —
# embeds haystack, backdates created_at to session dates.
from benchmarks.longmemeval_via_memory.runner import (
    _ingest_question,
    _parse_question_date,
)


SMOKE_DATASET = Path("/Users/jcarreira/projects/odin/mimir/saga/data/longmemeval/longmemeval_s_cleaned.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_one_question(
    question: dict, *,
    work_dir: Path,
    turn_logger: TurnLogger,
    session_id: str,
) -> dict:
    qid = question["question_id"]
    db_path = work_dir / f"q_{qid}.db"
    # Fresh DB per question
    for suffix in ("", "-wal", "-shm"):
        f = db_path.with_suffix(db_path.suffix + suffix) if suffix else db_path
        if f.exists():
            f.unlink()

    client = MemoryClient(db_path=db_path, embedding_dim=None)

    print(f"  [{qid}] ingesting…", file=sys.stderr, flush=True)
    t0 = time.time()
    ingest_stats = await _ingest_question(client, question)
    t_ingest = time.time() - t0

    print(f"  [{qid}] consolidating…", file=sys.stderr, flush=True)
    t0 = time.time()
    try:
        cresult = await client.consolidate() or {}
        n_clusters = (
            cresult.get("clusters_formed")
            or len(cresult.get("observations_emitted") or [])
            or 0
        )
    except Exception as exc:
        print(f"    consolidation failed: {exc}", file=sys.stderr)
        n_clusters = 0
    t_consolidate = time.time() - t0

    # Build the deepagent with this question's memory client
    agent = make_agent(
        client,
        extra_system=f"Today's date: {question['question_date']}",
    )

    prompt = question["question"]
    turn_id = make_turn_id()
    print(f"  [{qid}] agent.ainvoke({prompt[:80]}…)", file=sys.stderr)

    t0 = time.time()
    error: str | None = None
    messages: list = []
    output = ""
    try:
        result = await agent.ainvoke({"messages": [HumanMessage(content=prompt)]})
        messages = result.get("messages", [])
        events, output = extract_turn_events(messages)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        events = []
    t_agent = time.time() - t0

    result_fields = derive_result_fields(messages)

    record = TurnRecord(
        ts=_utc_now(),
        turn_id=turn_id,
        session_id=session_id,
        saga_session_id=None,  # PoC: no saga-session wiring yet
        trigger="user_message",
        channel_id=f"bench-{qid}",
        input=truncate_input(prompt),
        events=events,
        output=output[:2048],
        duration_ms=int(t_agent * 1000),
        error=error,
        **result_fields,
    )
    await turn_logger.write(record)
    print(
        f"  [{qid}] ingest={t_ingest:.0f}s cons={t_consolidate:.0f}s "
        f"agent={t_agent:.0f}s (n_atoms={ingest_stats.get('ingested')}, "
        f"clusters={n_clusters})",
        file=sys.stderr,
    )
    return {
        "qid": qid,
        "question": question["question"],
        "answer": question.get("answer"),
        "hypothesis": output[:500],
        "error": error,
        "n_messages": len(messages),
        "usage": result_fields.get("usage"),
        "stop_reason": result_fields.get("stop_reason"),
    }


async def main() -> int:
    work_dir = Path(__file__).resolve().parent / "_smoke_work"
    work_dir.mkdir(exist_ok=True)
    turns_path = work_dir / "turns.jsonl"
    if turns_path.exists():
        turns_path.unlink()
    turn_logger = TurnLogger(turns_path)
    session_id = f"poc-smoke-{int(time.time())}"

    if not SMOKE_DATASET.exists():
        print(f"dataset not found: {SMOKE_DATASET}", file=sys.stderr)
        return 2
    data = json.load(SMOKE_DATASET.open())
    # First 2 questions are single-session-user (easiest bucket, saga 94%)
    questions = data[:2]

    # The bench expects saga.toml in MIMIR_HOME for embeddings config.
    # Reuse the via_mimir _write_bench_saga_toml helper to seed one.
    if "MIMIR_HOME" not in os.environ:
        os.environ["MIMIR_HOME"] = str(work_dir / "mimir_home")
    home = Path(os.environ["MIMIR_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    from benchmarks.longmemeval_via_mimir.runner import _write_bench_saga_toml
    _write_bench_saga_toml(home)
    os.environ["SAGA_CONFIG"] = str(home / "saga.toml")
    # SAGA_DATA_DIR is what _ingest_question's batch embed reads (max chars etc.)
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)

    print(f"running {len(questions)} questions through deepagents PoC", file=sys.stderr)
    results = []
    for q in questions:
        try:
            r = await run_one_question(
                q,
                work_dir=work_dir,
                turn_logger=turn_logger,
                session_id=session_id,
            )
        except Exception as exc:
            print(f"question {q['question_id']} crashed: {exc}", file=sys.stderr)
            continue
        results.append(r)

    print()
    print("=" * 70)
    print("Results summary")
    print("=" * 70)
    for r in results:
        print(f"\nQID: {r['qid']}")
        print(f"  Q:    {r['question']}")
        print(f"  GOLD: {r['answer']}")
        print(f"  HYP:  {r['hypothesis']}")
        print(f"  meta: n_messages={r['n_messages']} stop={r['stop_reason']}")
        if r["usage"]:
            u = r["usage"]
            print(f"  usage: in={u['input_tokens']} out={u['output_tokens']} "
                  f"cache_read={u['cache_read_input_tokens']} "
                  f"cache_create={u['cache_creation_input_tokens']}")
        if r["error"]:
            print(f"  ERROR: {r['error']}")
    print()
    print(f"turns.jsonl written to {turns_path}")
    print(f"  records: {sum(1 for _ in turns_path.open())}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
