"""Tests for the ``list_schedules`` tool.

Regression coverage: ``list_schedules`` was reading ``j.last_run`` /
``j.next_fire`` on the YAML-config :class:`mimir.scheduler.SchedulerJob`
dataclass, which doesn't have those attributes (they live on
apscheduler's runtime ``Job``). Every call by the agent crashed the
turn — observed in production on muninn-mimir (2026-05-21 morning):
``'SchedulerJob' object has no attribute 'last_run'`` blew up
``morning-briefing`` / ``ai-news-check`` / ``moltbook-browse`` turns,
which is why Muninn went silent on Discord for a day.

This test pins the tool's output to config fields only, so adding
runtime fields later (via a proper apscheduler join) is an additive
change that won't accidentally re-introduce the same attribute-error
path.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from mimir.scheduler import SchedulerJob
from mimir.tools.registry import _STATE, list_schedules


class _StubScheduler:
    """Minimal stand-in for ``mimir.scheduler.Scheduler``: returns whatever
    list of :class:`SchedulerJob` (and, for #522, poller details) we set up.
    The real scheduler's ``list_jobs`` is ``async``; we mirror that."""

    def __init__(self, jobs: list[SchedulerJob], pollers=()) -> None:
        self._jobs = jobs
        self._pollers = [dict(p) for p in pollers]

    async def list_jobs(self) -> list[SchedulerJob]:
        return list(self._jobs)

    def registered_poller_details(self) -> list[dict[str, str]]:
        return [dict(p) for p in self._pollers]


@pytest.fixture
def stub_scheduler():
    """Install a stub Scheduler in the tool's _STATE for the test,
    restore on teardown so other tests don't inherit it."""
    prev = _STATE.get("scheduler")
    yield lambda jobs, pollers=(): _STATE.__setitem__(
        "scheduler", _StubScheduler(jobs, pollers)
    )
    _STATE["scheduler"] = prev


# ─── Regression: was crashing on j.last_run / j.next_fire ──────────────


@pytest.mark.asyncio
async def test_list_schedules_does_not_crash_on_pure_yaml_jobs(stub_scheduler):
    """Real ``SchedulerJob`` objects from YAML never have ``last_run``
    or ``next_fire``. The tool must NOT touch those attributes."""
    stub_scheduler([
        SchedulerJob(
            name="heartbeat",
            prompt_file="heartbeat.md",
            cron="0 * * * *",
            channel_id=None,
        ),
    ])
    result = await list_schedules.ainvoke({})
    # Crash would have produced a string starting "list_schedules failed:".
    assert "failed" not in result.lower()
    parsed = json.loads(result)
    assert parsed[0]["name"] == "heartbeat"
    assert parsed[0]["cron"] == "0 * * * *"
    # The fields that used to crash MUST NOT be in the output —
    # adding them back would re-introduce the production bug.
    assert "last_run" not in parsed[0]
    assert "next_fire" not in parsed[0]


# ─── Output surfaces the prompt-source field (whichever is set) ────────


@pytest.mark.asyncio
async def test_list_schedules_surfaces_prompt_file(stub_scheduler):
    stub_scheduler([
        SchedulerJob(
            name="morning-briefing", prompt_file="morning-briefing.md",
            cron="0 8 * * *", channel_id=None,
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert parsed[0]["prompt_file"] == "morning-briefing.md"


@pytest.mark.asyncio
async def test_list_schedules_surfaces_inline_prompt(stub_scheduler):
    stub_scheduler([
        SchedulerJob(
            name="custom",
            prompt="Check the deploy queue and report.",
            cron="*/15 * * * *",
            channel_id="C12345",
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert parsed[0]["prompt"] == "Check the deploy queue and report."
    assert parsed[0]["channel_id"] == "C12345"


@pytest.mark.asyncio
async def test_list_schedules_truncates_long_inline_prompts(stub_scheduler):
    """Inline prompts can be hundreds of lines (entire workflows); the
    tool's job is a quick listing, not full prompt rendering."""
    long_prompt = "Step one. " * 100  # >200 chars
    stub_scheduler([
        SchedulerJob(
            name="long-one", prompt=long_prompt,
            cron="0 0 * * *", channel_id=None,
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert parsed[0]["prompt"].endswith("...")
    assert len(parsed[0]["prompt"]) <= 205  # 200 chars + the "..."


@pytest.mark.asyncio
async def test_list_schedules_surfaces_callable_name(stub_scheduler):
    """Some jobs are code-callable references rather than prompts."""
    stub_scheduler([
        SchedulerJob(
            name="saga-consolidate", callable_name="saga-consolidate",
            cron="0 4 * * *", channel_id=None,
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert parsed[0]["callable"] == "saga-consolidate"
    assert "prompt" not in parsed[0]
    assert "prompt_file" not in parsed[0]


@pytest.mark.asyncio
async def test_list_schedules_surfaces_time_of_day(stub_scheduler):
    """``time_of_day`` is the alternative to ``cron`` style; the tool
    should surface it instead of pretending cron is the only option."""
    stub_scheduler([
        SchedulerJob(
            name="evening", prompt_file="evening.md",
            cron=None, time_of_day="22:00", channel_id=None,
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert parsed[0]["time_of_day"] == "22:00"


# ─── Empty / no-scheduler cases ────────────────────────────────────────


# ─── #523/#522: priority + type discriminator + poller surfacing ──────


@pytest.mark.asyncio
async def test_list_schedules_includes_type_and_priority(stub_scheduler):
    """#523: every job entry carries its arbiter ``priority`` and a ``type``
    discriminator so jobs and pollers are distinguishable."""
    stub_scheduler([
        SchedulerJob(
            name="heartbeat", prompt_file="heartbeat.md",
            cron="0 * * * *", channel_id=None, priority="high",
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert parsed[0]["type"] == "job"
    assert parsed[0]["priority"] == "high"


@pytest.mark.asyncio
async def test_list_schedules_surfaces_pollers(stub_scheduler):
    """#522: skill pollers (a separate registry) appear alongside jobs,
    labeled ``type: poller``, with their cron + priority."""
    stub_scheduler(
        [SchedulerJob(name="heartbeat", prompt_file="heartbeat.md",
                      cron="0 * * * *", channel_id=None)],
        pollers=[{"name": "worklink-ready-queue", "cron": "*/10 * * * *",
                  "priority": "normal"}],
    )
    parsed = json.loads(await list_schedules.ainvoke({}))
    by_type: dict[str, list] = {}
    for entry in parsed:
        by_type.setdefault(entry["type"], []).append(entry)
    assert [j["name"] for j in by_type["job"]] == ["heartbeat"]
    assert len(by_type["poller"]) == 1
    poller = by_type["poller"][0]
    assert poller["name"] == "worklink-ready-queue"
    assert poller["cron"] == "*/10 * * * *"
    assert poller["priority"] == "normal"


@pytest.mark.asyncio
async def test_list_schedules_no_scheduler_configured():
    prev = _STATE.get("scheduler")
    _STATE["scheduler"] = None
    try:
        result = await list_schedules.ainvoke({})
        assert "no scheduler configured" in result
    finally:
        _STATE["scheduler"] = prev


@pytest.mark.asyncio
async def test_list_schedules_no_jobs(stub_scheduler):
    stub_scheduler([])
    result = await list_schedules.ainvoke({})
    assert "no scheduled jobs" in result


@pytest.mark.asyncio
async def test_list_schedules_poller_only_deployment_not_reported_empty(stub_scheduler):
    """#522 regression (mimir review on PR #728): an install with NO yaml-config
    jobs but registered pollers must surface the pollers, not report
    '(no scheduled jobs)' — that empty-check used to fire before poller rows
    were appended, recreating the exact poller-only visibility gap."""
    stub_scheduler(
        [],
        pollers=[{"name": "github-activity", "cron": "*/5 * * * *", "priority": "normal"}],
    )
    result = await list_schedules.ainvoke({})
    assert "no scheduled jobs" not in result
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["type"] == "poller"
    assert parsed[0]["name"] == "github-activity"


@pytest.mark.asyncio
async def test_list_schedules_multiple_jobs_round_trip(stub_scheduler):
    """The real muninn schedule mixes prompt_file (most jobs), inline
    prompt (a couple of one-liners), and callable_name (saga jobs).
    All three need to coexist in the output."""
    stub_scheduler([
        SchedulerJob(
            name="heartbeat", prompt_file="heartbeat.md",
            cron="0 * * * *", channel_id=None,
        ),
        SchedulerJob(
            name="process-conditional-todos",
            prompt="Run python3 scripts/process_conditional_todos.py ...",
            cron="0 7 * * *", channel_id=None,
        ),
        SchedulerJob(
            name="saga-consolidate", callable_name="saga-consolidate",
            cron="0 4 * * *", channel_id=None,
        ),
    ])
    parsed = json.loads(await list_schedules.ainvoke({}))
    assert len(parsed) == 3
    by_name = {p["name"]: p for p in parsed}
    assert "prompt_file" in by_name["heartbeat"]
    assert "prompt" in by_name["process-conditional-todos"]
    assert "callable" in by_name["saga-consolidate"]
