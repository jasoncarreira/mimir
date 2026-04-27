"""scheduler.yaml LLM-tick jobs (SPEC §3.5, §7.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from mimir.event_logger import init_logger
from mimir.models import AgentEvent
from mimir.scheduler import (
    Scheduler,
    SchedulerJob,
    _scheduler_channel_id,
    load_jobs,
    write_jobs,
)


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


def test_job_yaml_round_trip(tmp_path: Path):
    path = tmp_path / "scheduler.yaml"
    jobs = [
        SchedulerJob(name="morning", prompt="review", cron="0 8 * * *", channel_id=None),
        SchedulerJob(name="hourly", prompt="check", time_of_day="07:30", channel_id="bench-1"),
    ]
    write_jobs(path, jobs)
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed[0]["name"] == "morning"
    assert parsed[0]["cron"] == "0 8 * * *"
    assert parsed[1]["time_of_day"] == "07:30"

    reloaded = load_jobs(path)
    assert len(reloaded) == 2
    assert reloaded[0].cron == "0 8 * * *"
    assert reloaded[1].time_of_day == "07:30"
    assert reloaded[1].channel_id == "bench-1"


def test_load_jobs_skips_invalid_entries(tmp_path: Path):
    path = tmp_path / "scheduler.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"name": "good", "prompt": "ok", "cron": "* * * * *"},
                {"name": "missing-prompt", "cron": "* * * * *"},
                {"name": "both-set", "prompt": "x", "cron": "* * * * *", "time_of_day": "08:00"},
                {"name": "neither-set", "prompt": "y"},
            ]
        )
    )
    jobs = load_jobs(path)
    assert [j.name for j in jobs] == ["good"]


def test_load_jobs_handles_missing_file(tmp_path: Path):
    assert load_jobs(tmp_path / "nope.yaml") == []


def test_scheduler_channel_id_synthetic_for_global():
    assert _scheduler_channel_id("nightly", None) == "scheduler:nightly"
    assert _scheduler_channel_id("nightly", "real-channel") == "real-channel"


@pytest.mark.asyncio
async def test_add_job_persists_to_yaml_and_replaces_by_name(tmp_path: Path):
    path = tmp_path / "scheduler.yaml"
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    sched = Scheduler(scheduler_yaml=path, enqueue=fake_enqueue)

    await sched.add_job(SchedulerJob(name="a", prompt="hello", cron="0 8 * * *"))
    await sched.add_job(SchedulerJob(name="a", prompt="changed", cron="0 9 * * *"))

    jobs = await sched.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].prompt == "changed"
    assert jobs[0].cron == "0 9 * * *"


@pytest.mark.asyncio
async def test_add_job_validates_trigger(tmp_path: Path):
    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml", enqueue=lambda e: asyncio.sleep(0, result=True)
    )
    with pytest.raises(ValueError):
        await sched.add_job(SchedulerJob(name="bad", prompt="x", cron="not a cron"))


@pytest.mark.asyncio
async def test_remove_job(tmp_path: Path):
    path = tmp_path / "s.yaml"

    async def noop(_e):
        return True

    sched = Scheduler(scheduler_yaml=path, enqueue=noop)
    await sched.add_job(SchedulerJob(name="keep", prompt="x", cron="0 0 * * *"))
    await sched.add_job(SchedulerJob(name="drop", prompt="y", cron="0 1 * * *"))
    assert await sched.remove_job("drop") is True
    assert await sched.remove_job("nonexistent") is False
    jobs = await sched.list_jobs()
    assert [j.name for j in jobs] == ["keep"]


@pytest.mark.asyncio
async def test_fire_enqueues_scheduled_tick_event(tmp_path: Path):
    """Calling _fire directly (bypassing the cron trigger) must produce an
    AgentEvent with the right shape."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=fake_enqueue)
    job = SchedulerJob(name="morning", prompt="review extended memory", cron="0 8 * * *")
    await sched._fire(job=job)

    assert len(enqueued) == 1
    e = enqueued[0]
    assert e.trigger == "scheduled_tick"
    assert e.content == "review extended memory"
    assert e.channel_id == "scheduler:morning"
    assert e.extra["schedule_name"] == "morning"


@pytest.mark.asyncio
async def test_reload_registers_yaml_jobs(tmp_path: Path):
    path = tmp_path / "scheduler.yaml"
    write_jobs(
        path,
        [
            SchedulerJob(name="a", prompt="x", cron="0 8 * * *"),
            SchedulerJob(name="b", prompt="y", time_of_day="09:00"),
        ],
    )

    async def noop(_e):
        return True

    sched = Scheduler(scheduler_yaml=path, enqueue=noop)
    stats = sched.reload()
    assert stats == {"registered": 2, "invalid": 0}

    job_ids = {j.id for j in sched._scheduler.get_jobs()}
    assert "scheduler:a" in job_ids
    assert "scheduler:b" in job_ids


@pytest.mark.asyncio
async def test_reload_drops_jobs_no_longer_in_yaml(tmp_path: Path):
    path = tmp_path / "scheduler.yaml"

    async def noop(_e):
        return True

    sched = Scheduler(scheduler_yaml=path, enqueue=noop)
    await sched.add_job(SchedulerJob(name="going", prompt="x", cron="0 8 * * *"))
    assert "scheduler:going" in {j.id for j in sched._scheduler.get_jobs()}
    write_jobs(path, [])  # Drop everything from disk
    sched.reload()
    assert "scheduler:going" not in {j.id for j in sched._scheduler.get_jobs()}
