"""events.jsonl writer (SPEC §10.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mimir.event_logger import EventLogger


@pytest.mark.asyncio
async def test_log_appends_record_with_session_and_type(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")

    await logger.log("app_started", home="/h")
    await logger.log("tool_call", tool="echo", args={"text": "hi"}, turn_id="t1")

    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["type"] == "app_started"
    assert lines[0]["session_id"] == "proc-1"
    assert lines[0]["home"] == "/h"
    assert "timestamp" in lines[0]
    assert lines[1]["tool"] == "echo"


@pytest.mark.asyncio
async def test_concurrent_logs_do_not_interleave(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")

    await asyncio.gather(*(logger.log("tool_call", i=i) for i in range(50)))

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 50
    parsed = [json.loads(l) for l in lines]
    assert sorted(p["i"] for p in parsed) == list(range(50))


@pytest.mark.asyncio
async def test_max_events_trims(tmp_path: Path):
    """With hysteresis, trim fires when over cap by ≥10% (rounded up to
    at least 1 line). Between trims the file may sit between max and
    max+10%. The most-recent events are always kept."""
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1", max_events=3)

    for i in range(10):
        await logger.log("tool_call", i=i)

    parsed = [json.loads(l) for l in path.read_text().strip().splitlines()]
    # cap=3, hysteresis=max(3//10, 1)=1 → trigger at >4 lines, trim to 3.
    # The exact count at end depends on how many events landed since the
    # last trim cycle, but it's always ≤ trigger threshold and the most
    # recent events are preserved.
    assert len(parsed) <= 4
    # Recency invariant: whatever's left ends with the latest writes.
    last_i = parsed[-1]["i"]
    assert last_i == 9
    # And the kept range is contiguous (no gaps from out-of-order trim).
    indices = [p["i"] for p in parsed]
    assert indices == list(range(indices[0], indices[0] + len(indices)))


@pytest.mark.asyncio
async def test_max_events_trim_eventually_lands_on_cap(tmp_path: Path):
    """Over a long enough run, the file does come back to cap after a
    trim cycle — verifies trim-back-to-max actually happens."""
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1", max_events=10)

    for i in range(100):
        await logger.log("tool_call", i=i)

    lines = path.read_text().strip().splitlines()
    # cap=10, hysteresis=max(10//10,1)=1 → trigger at >11 lines.
    # Bound is between 10 (right after trim) and 11 (right before).
    assert 10 <= len(lines) <= 11
    parsed = [json.loads(l) for l in lines]
    assert parsed[-1]["i"] == 99
