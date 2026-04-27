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
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1", max_events=3)

    for i in range(10):
        await logger.log("tool_call", i=i)

    parsed = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(parsed) == 3
    assert [p["i"] for p in parsed] == [7, 8, 9]
