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


# ---- callable-param path (chainlink #44 follow-up) -----


@pytest.mark.asyncio
async def test_add_schedule_with_callable_validates_registry(tmp_path: Path):
    """add_schedule with callable= for an unregistered name returns
    a clear error listing what IS registered."""
    sched = _make_scheduler(tmp_path)
    tools = {t.name: t for t in build_schedule_tools(sched)}

    out = await tools["add_schedule"].handler({
        "name": "x", "callable": "no-such-callable", "cron": "0 4 * * *",
    })
    assert out.get("is_error") is True
    assert "not registered" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_add_schedule_with_callable_writes_yaml_and_reloads(tmp_path: Path):
    """add_schedule with callable= for a registered callable writes
    the yaml entry and the scheduler picks up the new cron."""
    sched = _make_scheduler(tmp_path)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")

    tools = {t.name: t for t in build_schedule_tools(sched)}
    out = await tools["add_schedule"].handler({
        "name": "demo-shifted", "callable": "demo", "cron": "0 6 * * *",
    })
    assert out.get("is_error") is not True

    listed = await tools["list_schedules"].handler({})
    parsed = yaml.safe_load(listed["content"][0]["text"])
    assert parsed[0]["name"] == "demo-shifted"
    assert parsed[0]["callable"] == "demo"
    assert parsed[0]["cron"] == "0 6 * * *"

    # APScheduler picked up the override.
    apjob = sched._scheduler.get_job("demo")
    assert "hour='6'" in str(apjob.trigger)


@pytest.mark.asyncio
async def test_add_schedule_callable_empty_cron_disables(tmp_path: Path):
    """callable + empty cron is the explicit-disable path; yaml gets
    the entry and the APScheduler job is dropped."""
    sched = _make_scheduler(tmp_path)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")
    assert sched._scheduler.get_job("demo") is not None

    tools = {t.name: t for t in build_schedule_tools(sched)}
    out = await tools["add_schedule"].handler({
        "name": "disable-demo", "callable": "demo",
    })
    # Note: no cron AND no time_of_day — for callable entries this is
    # the explicit-disable signal, NOT an error.
    assert out.get("is_error") is not True
    assert sched._scheduler.get_job("demo") is None


@pytest.mark.asyncio
async def test_add_schedule_callable_rejects_time_of_day(tmp_path: Path):
    """callable entries don't accept time_of_day."""
    sched = _make_scheduler(tmp_path)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")

    tools = {t.name: t for t in build_schedule_tools(sched)}
    out = await tools["add_schedule"].handler({
        "name": "x", "callable": "demo", "time_of_day": "08:00",
    })
    assert out.get("is_error") is True
    assert "time_of_day" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_add_schedule_rejects_prompt_and_callable_together(tmp_path: Path):
    """prompt + callable simultaneously is the mutex-violation path."""
    sched = _make_scheduler(tmp_path)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")

    tools = {t.name: t for t in build_schedule_tools(sched)}
    out = await tools["add_schedule"].handler({
        "name": "x", "prompt": "hi", "callable": "demo", "cron": "0 4 * * *",
    })
    assert out.get("is_error") is True
    assert "mutually exclusive" in out["content"][0]["text"]
