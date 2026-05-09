"""End-to-end test for ``spawn_claude_code`` (chainlink #62 verification
of #50 §A "Smoke test").

All other ``test_spawn.py`` tests use ``_FakeRegistry``; none exercise
the full chain through a real ``ShellJobRegistry`` + real subprocess +
real waiter-thread completion callback. This test fills that gap with
a fake ``claude`` shim on ``PATH`` so the spawn doesn't need to call
the real bundled CLI (which would cost money and pull in the sandboxed
``HOME=/mimir-home`` OAuth credentials path).

What's covered:
- ``ShellJobRegistry`` actually calls Popen with ``start_new_session=True``,
  the fake ``claude`` writes a result JSON, exits 0
- the waiter thread invokes ``_on_complete``
- ``_on_complete`` parses the JSON, schedules the async writes onto
  the test's loop via ``schedule_from_thread``
- ``events.jsonl`` ends up with both ``claude_code_spawn_started``
  and ``claude_code_spawn_completed``
- ``turns.jsonl`` ends up with the synthetic ``kind=claude_code_spawn``
  record (the path that lets ``aggregate_usage`` see spawn cost natively)
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path

import pytest

from mimir.event_logger import init_logger
from mimir.shell_jobs import ShellJobRegistry
from mimir.spawn import build_spawn_tool
from mimir.turn_logger import TurnLogger


def _read_events(home: Path) -> list[dict]:
    path = home / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _read_turns(home: Path) -> list[dict]:
    path = home / "logs" / "turns.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _install_fake_claude(bin_dir: Path, *, result_json: dict) -> Path:
    """Drop a ``claude`` shim under ``bin_dir`` that prints the given JSON
    blob to stdout and exits 0. Returns the binary path. Caller is
    responsible for prepending ``bin_dir`` to ``PATH``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "claude"
    # The real CLI prints "Shell cwd was reset to..." after the JSON when
    # invoked under bash -c, so the parser scans the tail. Mirror that
    # with a trailing noise line to exercise the parser path.
    payload = json.dumps(result_json)
    script = (
        "#!/bin/bash\n"
        "# Fake claude CLI for spawn_claude_code e2e test.\n"
        "# Ignores all flags; prints the canned result and exits.\n"
        f"cat <<'EOF'\n{payload}\nShell cwd was reset to /mimir-home\nEOF\n"
        "exit 0\n"
    )
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


@pytest.mark.asyncio
async def test_spawn_claude_code_full_chain_real_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full end-to-end chain:
    handler → registry.spawn → real Popen → fake claude prints result →
    waiter thread → on_complete → schedule_from_thread → events.jsonl
    + turns.jsonl writes.
    """
    home = tmp_path / "mimir-home"
    home.mkdir()
    (home / "logs").mkdir()
    (home / "state" / "spawns").mkdir(parents=True)

    init_logger(home / "logs" / "events.jsonl", session_id="test-spawn-e2e")

    fake_bin = tmp_path / "bin"
    result_json = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "total_cost_usd": 0.123,
        "duration_ms": 850,
        "modelUsage": {
            "claude-opus-4-7": {
                "inputTokens": 1234,
                "outputTokens": 567,
                "cacheCreationInputTokens": 100,
                "cacheReadInputTokens": 8000,
            }
        },
        "result": "ok",
    }
    _install_fake_claude(fake_bin, result_json=result_json)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    registry = ShellJobRegistry(jobs_dir=home / "shell-jobs")
    turn_logger = TurnLogger(home / "logs" / "turns.jsonl")

    loop = asyncio.get_running_loop()
    scheduled: list[asyncio.Task] = []

    def schedule(coro):
        # Production uses run_coroutine_threadsafe from the waiter thread;
        # that's the right shape here too because the registry's _waiter
        # thread is what calls _on_complete (which then calls this).
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        scheduled.append(fut)  # type: ignore[arg-type]

    chain_calls: list[str] = []

    def chain(job):
        chain_calls.append(job.job_id)

    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=turn_logger,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=schedule,
        chain_on_complete=chain,
    )

    out = await tool_def.handler({
        "brief": "End-to-end smoke test.",
        "working_dir": str(tmp_path),
        "agent": "code-implementer",
        "model": "opus",
        "max_turns": 5,
        "max_budget_usd": 1.0,
        "timeout_sec": 30,
    })
    assert out.get("is_error") is not True, out
    job_id = out["content"][0]["text"]
    # The text payload is "spawn_claude_code complete (job_id=..., ...)"
    # — easier to extract via the registry's own state.
    [job] = registry.all_jobs()
    job_id = job.job_id

    # Wait for the waiter thread to mark the job done AND for the
    # async-scheduled writes to drain onto our loop.
    deadline = time.time() + 10.0
    while time.time() < deadline and registry.get(job_id).exit_code is None:  # type: ignore[union-attr]
        await asyncio.sleep(0.05)
    assert registry.get(job_id).exit_code == 0, "fake claude should exit 0"

    # Drain any tasks the waiter thread scheduled onto our loop.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if scheduled and all(f.done() for f in scheduled):
            break
        await asyncio.sleep(0.05)
    # Concurrent.futures.Future raises on .result() if the coro errored —
    # surface it for debugging.
    for fut in scheduled:
        fut.result(timeout=1.0)

    # Events: started + completed both landed.
    events = _read_events(home)
    types = [e["type"] for e in events]
    assert "claude_code_spawn_started" in types, f"got {types}"
    assert "claude_code_spawn_completed" in types, f"got {types}"

    started = next(e for e in events if e["type"] == "claude_code_spawn_started")
    # cmd_argv carries the redacted argv (review nit #2 from PR #84).
    assert "cmd_argv" in started
    assert any("<brief at " in s for s in started["cmd_argv"] if isinstance(s, str))
    assert "End-to-end smoke test." not in str(started["cmd_argv"])
    assert started.get("resolved_model") == "opus"
    assert started.get("resolved_max_turns") == 5

    completed = next(e for e in events if e["type"] == "claude_code_spawn_completed")
    # Event payload uses ``cost_usd`` (the synthetic turn record uses
    # ``total_cost_usd`` because that's what aggregate_usage reads).
    assert completed.get("cost_usd") == pytest.approx(0.123)
    assert completed.get("terminal_reason") == "completed"
    assert completed.get("exit_code") == 0
    assert completed.get("agent") == "code-implementer"

    # Turns: synthetic kind=claude_code_spawn record with cost + usage.
    turns = _read_turns(home)
    spawn_records = [t for t in turns if t.get("kind") == "claude_code_spawn"]
    assert len(spawn_records) == 1, f"got {turns}"
    rec = spawn_records[0]
    assert rec.get("total_cost_usd") == pytest.approx(0.123)
    assert rec.get("usage") == {
        "input_tokens": 1234,
        "output_tokens": 567,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 8000,
    }
    assert rec.get("input", "").startswith("spawn:code-implementer:")

    # Chain: shell_job_complete handoff fired exactly once.
    assert chain_calls == [job_id]

    # Brief landed on disk and was not redacted there (only argv/events
    # redact — the brief file is the canonical content store).
    spawns = list((home / "state" / "spawns").glob("*.md"))
    assert len(spawns) == 1
    assert spawns[0].read_text() == "End-to-end smoke test."
