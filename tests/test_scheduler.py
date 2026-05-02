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


def test_add_introspection_report_job_validates_cron(tmp_path: Path):
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    # Invalid cron raises.
    with pytest.raises(ValueError):
        sched.add_introspection_report_job(tmp_path, "this is not cron")


def test_add_introspection_report_job_disabled_when_blank(tmp_path: Path):
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched.add_introspection_report_job(tmp_path, "") is False
    assert sched.add_introspection_report_job(tmp_path, "   ") is False


def test_add_introspection_report_job_registers(tmp_path: Path):
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched.add_introspection_report_job(tmp_path, "0 14 * * 5") is True
    job = sched._scheduler.get_job("introspection-report")
    assert job is not None


@pytest.mark.asyncio
async def test_introspection_report_callback_writes_report_and_emits(tmp_path: Path):
    """End-to-end: invoke the registered callback directly and verify it
    produces a state/reports/ file. Uses a degraded-heartbeat scenario
    so the algedonic emit also fires."""
    import json
    from datetime import datetime, timedelta, timezone

    home = tmp_path
    logs = home / "logs"
    logs.mkdir(exist_ok=True)
    # 4 fired ticks, only 1 successful turn → 25% pipeline rate.
    base = datetime.now(tz=timezone.utc)
    with (logs / "events.jsonl").open("w") as f:
        for i in range(4):
            f.write(json.dumps({
                "timestamp": (base - timedelta(hours=i + 1)).isoformat(),
                "type": "scheduled_tick",
                "session_id": "s",
            }) + "\n")
    with (logs / "turns.jsonl").open("w") as f:
        f.write(json.dumps({
            "ts": (base - timedelta(hours=1)).isoformat(),
            "turn_id": "t1", "session_id": "s", "saga_session_id": None,
            "trigger": "scheduled_tick", "channel_id": "c", "input": "",
            "events": [], "duration_ms": 100, "error": None,
        }) + "\n")
        f.write(json.dumps({
            "ts": (base - timedelta(hours=2)).isoformat(),
            "turn_id": "t2", "session_id": "s", "saga_session_id": None,
            "trigger": "scheduled_tick", "channel_id": "c", "input": "",
            "events": [], "duration_ms": 100, "error": "boom",
        }) + "\n")

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched.add_introspection_report_job(
        home, "0 14 * * 5",
        emit_algedonic=True, health_threshold=0.80,
    )

    # Pull the registered callable and invoke it directly. APScheduler
    # adds args/kwargs metadata; the bare async closure has no params.
    job = sched._scheduler.get_job("introspection-report")
    callback = job.func
    await callback()

    # Report file written.
    reports = list((home / "state" / "reports").glob("introspection-*.md"))
    assert len(reports) == 1
    assert "Heartbeat / scheduled-tick health" in reports[0].read_text()

    # Algedonic event appended (find by type — log_event from the
    # callback's success path may also have written to events.jsonl
    # if the global event-logger singleton was initialized).
    events = (logs / "events.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in events]
    assert "heartbeat_health_degraded" in types
    health = next(
        json.loads(line) for line in events
        if json.loads(line)["type"] == "heartbeat_health_degraded"
    )
    assert health["success_rate"] == 0.25


@pytest.mark.asyncio
async def test_fire_consults_arbiter_and_suppresses(tmp_path: Path):
    """§12.4: when the homeostat returns SUPPRESS, _fire must skip the
    enqueue and instead emit a scheduled_tick_suppressed event."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    class _SuppressingArbiter:
        def should_fire_heartbeat(self):
            from mimir.budget import HeartbeatDecision
            return HeartbeatDecision.SUPPRESS, "plan_window_saturated:7d_opus@0.92"

    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml",
        enqueue=fake_enqueue,
        arbiter=_SuppressingArbiter(),
    )
    job = SchedulerJob(name="morning", prompt="x", cron="0 8 * * *")
    await sched._fire(job=job)

    assert enqueued == []  # suppressed, no enqueue


@pytest.mark.asyncio
async def test_fire_consults_arbiter_and_fires(tmp_path: Path):
    """When arbiter returns FIRE, _fire proceeds normally."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    class _FiringArbiter:
        def should_fire_heartbeat(self):
            from mimir.budget import HeartbeatDecision
            return HeartbeatDecision.FIRE, "ok"

    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml",
        enqueue=fake_enqueue,
        arbiter=_FiringArbiter(),
    )
    job = SchedulerJob(name="morning", prompt="x", cron="0 8 * * *")
    await sched._fire(job=job)
    assert len(enqueued) == 1


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
