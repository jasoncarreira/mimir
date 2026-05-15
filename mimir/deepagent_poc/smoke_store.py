"""Tool migration smoke: verify the agent uses memory_store correctly.

Two-step scenario:
  1. Agent receives "Please remember: my favorite color is blue."
     → agent should call memory_store(content="User's favorite color
       is blue", stream="semantic")
  2. Agent then receives "What's my favorite color?"
     → agent should call memory_query and answer "blue"

Verifies:
- memory_store @tool dispatches correctly via deepagents
- write tool path round-trips through MemoryClient.store
- DB has the atom after the store call
- query in step 2 surfaces the stored atom
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from langchain_core.messages import HumanMessage

from mimir.memory.client import MemoryClient
from .agent import make_agent
from .memory_tool import set_memory_client
from .turn_logger import TurnLogger, extract_turn_events


async def main() -> int:
    work_dir = Path(__file__).resolve().parent / "_smoke_store_work"
    work_dir.mkdir(exist_ok=True)
    db_path = work_dir / "memory.db"
    for suffix in ("", "-wal", "-shm"):
        f = db_path.with_suffix(db_path.suffix + suffix) if suffix else db_path
        if f.exists():
            f.unlink()

    if "MIMIR_HOME" not in os.environ:
        os.environ["MIMIR_HOME"] = str(work_dir / "mimir_home")
    home = Path(os.environ["MIMIR_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    from benchmarks.longmemeval_via_mimir.runner import _write_bench_saga_toml
    _write_bench_saga_toml(home)
    os.environ["SAGA_CONFIG"] = str(home / "saga.toml")
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)

    client = MemoryClient(db_path=db_path, embedding_dim=None)
    set_memory_client(client)
    agent = make_agent(client)

    print("=" * 70)
    print("Step 1: ask agent to store a fact")
    print("=" * 70)
    result1 = await agent.ainvoke({
        "messages": [HumanMessage(
            content=(
                "Please remember this fact about me for future conversations: "
                "my favorite color is blue. Use the memory_store tool — "
                "store it as a 'semantic' stream atom."
            )
        )],
    })
    events1, output1 = extract_turn_events(result1["messages"])
    tool_calls1 = [(e["name"], e.get("args")) for e in events1 if e["type"] == "tool_call"]
    print(f"  output: {output1[:200]}")
    print(f"  tool calls: {tool_calls1}")

    # Verify atom in DB
    conn = client._ensure_conn()
    rows = conn.execute(
        "SELECT id, content, stream, source_type FROM atoms "
        "WHERE tombstoned=0 ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    print(f"\n  atoms in DB after Step 1: {len(rows)}")
    for r in rows:
        print(f"    {r[0][:12]}... stream={r[2]} source={r[3]}")
        print(f"      content: {r[1][:150]}")

    print()
    print("=" * 70)
    print("Step 2: query — does the agent recall it?")
    print("=" * 70)
    result2 = await agent.ainvoke({
        "messages": [HumanMessage(content="What's my favorite color?")]
    })
    events2, output2 = extract_turn_events(result2["messages"])
    tool_calls2 = [(e["name"], e.get("args")) for e in events2 if e["type"] == "tool_call"]
    print(f"  output: {output2[:200]}")
    print(f"  tool calls: {tool_calls2}")

    # Verdict
    correct = "blue" in output2.lower()
    used_store = any(name == "memory_store" for name, _ in tool_calls1)
    used_query = any(name == "memory_query" for name, _ in tool_calls2)

    print()
    print("=" * 70)
    print("Verdict")
    print("=" * 70)
    print(f"  agent called memory_store in Step 1:  {used_store}")
    print(f"  atom was persisted to DB:             {len(rows) > 0}")
    print(f"  agent called memory_query in Step 2:  {used_query}")
    print(f"  agent answered with 'blue':            {correct}")
    overall = used_store and len(rows) > 0 and used_query and correct
    print(f"  END-TO-END:                            {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
