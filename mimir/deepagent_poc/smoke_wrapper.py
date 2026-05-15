"""Smoke for the pre-message wrapper pattern (Day 2 of the PoC).

Same 2 questions as ``smoke.py`` but instead of relying on the agent
to call ``memory_query`` as a tool, we PRE-INJECT the memory context
into the HumanMessage before ``agent.ainvoke``. This mirrors mimir's
existing pre-message-hook semantics (one fetch per turn, before the
first model call).

What this proves:
1. The wrapper pattern works end-to-end
2. Agent can answer correctly even with memory_query tool NOT registered
3. Token usage compares fairly vs the tool-only path
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from deepagents import create_deep_agent

from mimir.memory.client import MemoryClient
from .agent import SYSTEM_PROMPT, resolve_model
from .pre_message_hook import invoke_with_pre_message
from .turn_logger import (
    TurnLogger,
    TurnRecord,
    derive_result_fields,
    extract_turn_events,
    make_turn_id,
    truncate_input,
)

from benchmarks.longmemeval_via_memory.runner import _ingest_question


SMOKE_DATASET = Path("/Users/jcarreira/projects/odin/mimir/saga/data/longmemeval/longmemeval_s_cleaned.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_agent_no_tools(model: str = "openai:gpt-5.4-nano"):
    """Agent with NO custom tools — memory must come from the wrapper."""
    return create_deep_agent(
        model=resolve_model(model),
        tools=[],  # pre-injection only — no agent-callable memory tool
        system_prompt=SYSTEM_PROMPT,
    )


async def run_one_question(
    question: dict, *,
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

    agent = _make_agent_no_tools()

    prompt = question["question"]
    turn_id = make_turn_id()
    print(f"  [{qid}] pre_message + ainvoke({prompt[:80]}…)", file=sys.stderr)

    t0 = time.time()
    error: str | None = None
    messages: list = []
    output = ""
    pre = None
    try:
        result, pre = await invoke_with_pre_message(
            agent,
            memory_client=client,
            question=prompt,
        )
        messages = result.get("messages", [])
        events, output = extract_turn_events(messages)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        events = []
    t_agent = time.time() - t0

    result_fields = derive_result_fields(messages)

    # Capture pre-message timing inline as a synthetic event so the
    # turn record reflects the wrapper's cost. Not required for
    # downstream tooling — informational.
    if pre is not None:
        events.insert(0, {
            "type": "tool_call",
            "id": "pre_message_hook",
            "name": "pre_message_memory_query",
            "args": {"query": prompt[:100]},
            "_synthetic": True,
        })
        events.insert(1, {
            "type": "tool_result",
            "id": "pre_message_hook",
            "name": "pre_message_memory_query",
            "content": pre.memory_block[:500],
            "is_error": False,
            "_synthetic": True,
        })

    record = TurnRecord(
        ts=_utc_now(),
        turn_id=turn_id,
        session_id=session_id,
        saga_session_id=None,
        trigger="user_message",
        channel_id=f"bench-{qid}",
        input=truncate_input(prompt),
        saga_atom_ids=(pre.saga_atom_ids if pre else []),
        events=events,
        output=output[:2048],
        duration_ms=int(t_agent * 1000),
        error=error,
        **result_fields,
    )
    await turn_logger.write(record)
    print(
        f"  [{qid}] ingest={t_ingest:.0f}s cons={t_consolidate:.0f}s "
        f"agent={t_agent:.0f}s (pre_ms={pre.pre_message_ms if pre else '?'}, "
        f"n_atoms={ingest_stats.get('ingested')}, clusters={n_clusters})",
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
        "pre_message_ms": pre.pre_message_ms if pre else None,
        "n_atoms_injected": len(pre.saga_atom_ids) if pre else 0,
    }


async def main() -> int:
    work_dir = Path(__file__).resolve().parent / "_smoke_wrapper_work"
    work_dir.mkdir(exist_ok=True)
    turns_path = work_dir / "turns.jsonl"
    if turns_path.exists():
        turns_path.unlink()
    turn_logger = TurnLogger(turns_path)
    session_id = f"poc-wrapper-smoke-{int(time.time())}"

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

    print(f"running {len(questions)} questions through wrapper PoC "
          f"(NO memory_query tool — pre-inject only)", file=sys.stderr)
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
    print("Wrapper smoke results")
    print("=" * 70)
    for r in results:
        print(f"\nQID: {r['qid']}")
        print(f"  Q:    {r['question']}")
        print(f"  GOLD: {r['answer']}")
        print(f"  HYP:  {r['hypothesis']}")
        print(f"  meta: n_messages={r['n_messages']} stop={r['stop_reason']}")
        print(f"  pre_message_ms={r['pre_message_ms']} "
              f"n_atoms_injected={r['n_atoms_injected']}")
        if r["usage"]:
            u = r["usage"]
            print(f"  usage: in={u['input_tokens']} out={u['output_tokens']} "
                  f"cache_read={u['cache_read_input_tokens']}")
        if r["error"]:
            print(f"  ERROR: {r['error']}")
    print()
    print(f"turns.jsonl: {turns_path}  ({sum(1 for _ in turns_path.open())} records)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
