"""End-to-end smoke through the bundled ``run_turn``.

Same 2 questions, but going through the full deepagents pipeline:
  pre-message hook → ainvoke → extract → post-message credit pass
  → turn_logger.write → TurnOutcome

Verifies the credit-pass flow: after agent answers, atoms that
contributed get +1.0 feedback_positive access_events written.
"""
from __future__ import annotations

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


SMOKE_DATASET = Path(
    "/Users/jcarreira/projects/odin/mimir/saga/data/longmemeval/longmemeval_s_cleaned.json"
)


async def run_one_question(
    question: dict,
    *,
    work_dir: Path,
    turn_logger: TurnLogger,
    session_id: str,
) -> dict:
    qid = question["question_id"]
    db_path = work_dir / f"q_{qid}.db"
    for suffix in ("", "-wal", "-shm"):
        f = db_path.with_suffix(db_path.suffix + suffix) if suffix else db_path
        if f.exists():
            f.unlink()

    client = MemoryClient(db_path=db_path, embedding_dim=None)
    set_memory_client(client)

    print(f"  [{qid}] ingest+consolidate…", file=sys.stderr, flush=True)
    await _ingest_question(client, question)
    try:
        await client.consolidate()
    except Exception as exc:
        print(f"    consolidation failed: {exc}", file=sys.stderr)

    agent = make_agent(client)

    saga_session_id = f"saga-bench-{qid}"
    print(f"  [{qid}] run_turn({question['question'][:80]}…)", file=sys.stderr)

    outcome = await run_turn(
        agent,
        memory_client=client,
        question=question["question"],
        session_id=session_id,
        channel_id=f"bench-{qid}",
        saga_session_id=saga_session_id,
        turn_logger=turn_logger,
    )

    # Verify feedback fired: query access_events for feedback_positive
    # rows on atoms that were credited.
    feedback_rows: list[dict] = []
    if outcome.post_message and outcome.post_message.atom_ids_credited:
        conn = client._ensure_conn()
        for aid in outcome.post_message.atom_ids_credited[:5]:  # sample
            row = conn.execute(
                "SELECT atom_id, source, weight FROM access_events "
                "WHERE atom_id = ? AND source = 'feedback_positive' "
                "ORDER BY ts DESC LIMIT 1",
                (aid,),
            ).fetchone()
            if row:
                feedback_rows.append({"atom_id": row[0], "source": row[1], "weight": row[2]})

    return {
        "qid": qid,
        "question": question["question"],
        "answer": question.get("answer"),
        "hypothesis": outcome.output[:300],
        "duration_ms": outcome.duration_ms,
        "n_atoms_credited": (
            len(outcome.post_message.atom_ids_credited)
            if outcome.post_message else 0
        ),
        "post_message_ms": outcome.post_message.post_message_ms if outcome.post_message else None,
        "feedback_ok": outcome.post_message.feedback_ok if outcome.post_message else None,
        "feedback_error": outcome.post_message.feedback_error if outcome.post_message else None,
        "feedback_rows": feedback_rows,
        "error": outcome.error,
    }


async def main() -> int:
    work_dir = Path(__file__).resolve().parent / "_smoke_full_work"
    work_dir.mkdir(exist_ok=True)
    turns_path = work_dir / "turns.jsonl"
    if turns_path.exists():
        turns_path.unlink()
    turn_logger = TurnLogger(turns_path)
    session_id = f"poc-full-smoke-{int(time.time())}"

    if not SMOKE_DATASET.exists():
        print(f"dataset not found: {SMOKE_DATASET}", file=sys.stderr)
        return 2
    data = json.load(SMOKE_DATASET.open())
    questions = data[:2]

    if "MIMIR_HOME" not in os.environ:
        os.environ["MIMIR_HOME"] = str(work_dir / "mimir_home")
    home = Path(os.environ["MIMIR_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    from benchmarks.longmemeval_via_mimir.runner import _write_bench_saga_toml
    _write_bench_saga_toml(home)
    os.environ["SAGA_CONFIG"] = str(home / "saga.toml")
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)

    print(f"running {len(questions)} questions through FULL pipeline "
          f"(pre→invoke→post→log)", file=sys.stderr)
    results = []
    for q in questions:
        try:
            r = await run_one_question(
                q, work_dir=work_dir,
                turn_logger=turn_logger, session_id=session_id,
            )
        except Exception as exc:
            print(f"question {q['question_id']} crashed: {exc}", file=sys.stderr)
            continue
        results.append(r)

    print()
    print("=" * 70)
    print("Full pipeline smoke results")
    print("=" * 70)
    for r in results:
        print(f"\nQID: {r['qid']}")
        print(f"  Q:    {r['question']}")
        print(f"  GOLD: {r['answer']}")
        print(f"  HYP:  {r['hypothesis']}")
        print(f"  duration_ms: {r['duration_ms']}")
        print(f"  post-message: n_atoms_credited={r['n_atoms_credited']} "
              f"ms={r['post_message_ms']} ok={r['feedback_ok']}")
        if r["feedback_error"]:
            print(f"    feedback_error: {r['feedback_error']}")
        for fr in r["feedback_rows"]:
            print(f"    fb_row: atom={fr['atom_id'][:8]}... weight={fr['weight']}")
        if r["error"]:
            print(f"  ERROR: {r['error']}")
    print()
    print(f"turns.jsonl records: {sum(1 for _ in turns_path.open())}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
