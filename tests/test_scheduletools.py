"""Schedule MCP tools (SPEC §7.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mimir.event_logger import init_logger
from mimir.scheduler import Scheduler
from mimir.scheduletools import build_schedule_tools


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not registered")


def _make_scheduler(tmp_path: Path) -> Scheduler:
    async def noop(_e):
        return True

    return Scheduler(scheduler_yaml=tmp_path / "scheduler.yaml", enqueue=noop)


@pytest.mark.asyncio
async def test_add_then_list_round_trip(tmp_path: Path):
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}

    out = await tools["add_schedule"].handler({
        "name": "morning",
        "prompt": "review notes",
        "cron": "0 8 * * *",
    })
    assert out.get("is_error") is not True

    listed = await tools["list_schedules"].handler({})
    body = listed["content"][0]["text"]
    parsed = yaml.safe_load(body)
    assert parsed[0]["name"] == "morning"
    assert parsed[0]["cron"] == "0 8 * * *"


@pytest.mark.asyncio
async def test_add_replaces_by_name(tmp_path: Path):
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}

    await tools["add_schedule"].handler({
        "name": "x", "prompt": "v1", "cron": "0 8 * * *"
    })
    await tools["add_schedule"].handler({
        "name": "x", "prompt": "v2", "time_of_day": "09:00"
    })

    listed = await tools["list_schedules"].handler({})
    parsed = yaml.safe_load(listed["content"][0]["text"])
    assert len(parsed) == 1
    assert parsed[0]["prompt"] == "v2"
    assert parsed[0]["time_of_day"] == "09:00"
    assert "cron" not in parsed[0]


@pytest.mark.asyncio
async def test_add_rejects_both_or_neither_trigger(tmp_path: Path):
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}

    out = await tools["add_schedule"].handler({"name": "x", "prompt": "p"})
    assert out.get("is_error") is True

    out = await tools["add_schedule"].handler({
        "name": "x", "prompt": "p", "cron": "* * * * *", "time_of_day": "09:00"
    })
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_add_rejects_bad_cron(tmp_path: Path):
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}
    out = await tools["add_schedule"].handler({
        "name": "x", "prompt": "p", "cron": "not a cron expression"
    })
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_remove_returns_clear_message(tmp_path: Path):
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}

    await tools["add_schedule"].handler({
        "name": "y", "prompt": "p", "cron": "0 8 * * *"
    })
    out = await tools["remove_schedule"].handler({"name": "y"})
    assert out.get("is_error") is not True

    out2 = await tools["remove_schedule"].handler({"name": "missing"})
    assert "no job named" in out2["content"][0]["text"]


@pytest.mark.asyncio
async def test_list_when_empty(tmp_path: Path):
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}
    out = await tools["list_schedules"].handler({})
    assert "no schedules" in out["content"][0]["text"]
