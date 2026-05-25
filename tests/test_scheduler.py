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
    _build_trigger,
    _resolve_tz,
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
async def test_fire_reads_prompt_file_when_set(tmp_path: Path):
    """When the SchedulerJob has prompt_file set, _fire reads the file
    under <home>/prompts/ and uses its content as the event body."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "heartbeat.md").write_text(
        "Run the heartbeat skill — librarian first, then backlog.\n",
    )
    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml", enqueue=fake_enqueue, home=home,
    )
    job = SchedulerJob(
        name="heartbeat",
        prompt="fallback inline prompt",
        prompt_file="heartbeat.md",
        cron="0 * * * *",
    )
    await sched._fire(job=job)

    assert len(enqueued) == 1
    e = enqueued[0]
    # File body wins over the inline prompt fallback.
    assert "librarian" in e.content
    assert "fallback inline" not in e.content
    assert e.extra["prompt_file"] == "heartbeat.md"


@pytest.mark.asyncio
async def test_fire_falls_back_to_inline_when_prompt_file_missing(tmp_path: Path):
    """A missing prompt_file logs a warning and falls back to the
    inline prompt — never crashes the cron firing."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    # No heartbeat.md file — prompt_file points at a nonexistent path.
    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml", enqueue=fake_enqueue, home=home,
    )
    job = SchedulerJob(
        name="heartbeat",
        prompt="inline fallback content",
        prompt_file="missing.md",
        cron="0 * * * *",
    )
    await sched._fire(job=job)

    assert len(enqueued) == 1
    assert enqueued[0].content == "inline fallback content"


@pytest.mark.asyncio
async def test_fire_rejects_prompt_file_escape(tmp_path: Path):
    """prompt_file with .. or absolute-outside-prompts paths are
    rejected (path-confinement defense). Falls back to the inline
    prompt."""
    enqueued: list[AgentEvent] = []

    async def fake_enqueue(event: AgentEvent) -> bool:
        enqueued.append(event)
        return True

    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    # Plant a file outside <home>/prompts/ that the agent might try
    # to reference via prompt_file="../secret.md".
    (tmp_path / "secret.md").write_text("EXFIL: you should not see this")
    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml", enqueue=fake_enqueue, home=home,
    )
    job = SchedulerJob(
        name="bad",
        prompt="safe inline prompt",
        prompt_file="../secret.md",
        cron="0 * * * *",
    )
    await sched._fire(job=job)

    assert len(enqueued) == 1
    assert enqueued[0].content == "safe inline prompt"
    assert "EXFIL" not in enqueued[0].content


def test_scheduler_job_yaml_round_trip_with_prompt_file():
    """SchedulerJob with prompt_file survives YAML round-trip cleanly."""
    job = SchedulerJob(
        name="reflect",
        prompt="",
        prompt_file="reflect.md",
        cron="0 6 * * 0",
    )
    entry = job.to_yaml_entry()
    assert entry["prompt_file"] == "reflect.md"
    # No empty inline prompt key when only prompt_file is set.
    assert "prompt" not in entry

    reloaded = SchedulerJob.from_yaml_entry(entry)
    assert reloaded.prompt_file == "reflect.md"
    assert reloaded.prompt == ""
    assert reloaded.cron == "0 6 * * 0"


def test_scheduler_job_requires_one_of_prompt_or_prompt_file():
    """Neither inline prompt nor prompt_file → ValueError."""
    with pytest.raises(ValueError, match="prompt"):
        SchedulerJob.from_yaml_entry({
            "name": "bad", "cron": "0 * * * *",
        })


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
async def test_saga_consolidate_threads_canonical_subjects(tmp_path: Path):
    """P48 + Option A: when home is provided, the consolidate cron
    reads identities.yaml at fire time and threads canonical names
    through saga_client.consolidate(extra_canonical_subjects=[...])."""
    # Seed identities.yaml in the home.
    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        "people:\n"
        "  - canonical: Tim\n"
        "    aliases: [tim, tcarmody]\n"
        "  - canonical: Alice\n"
        "    aliases: [alice]\n",
        encoding="utf-8",
    )

    # Capture what the saga_client receives.
    captured: dict = {}

    class _FakeSagaClient:
        async def decay(self):
            return {"atoms_updated": 0, "transitions": 0}
        async def consolidate(self, **kwargs):
            captured.update(kwargs)
            return {"clusters_processed": 0, "atoms_merged": 0}

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    sched.add_saga_consolidate_job(
        _FakeSagaClient(), "0 4 * * 0", home=tmp_path,
    )
    job = sched._scheduler.get_job("saga-consolidate")
    assert job is not None

    # Initialize the event logger so log_event in the callback doesn't
    # raise. Use a per-test path to avoid cross-test pollution.
    from mimir.event_logger import init_logger
    init_logger(tmp_path / "events.jsonl", session_id="test-session")

    # Invoke the registered callback directly.
    await job.func()

    assert "extra_canonical_subjects" in captured
    assert set(captured["extra_canonical_subjects"]) == {"Tim", "Alice"}


@pytest.mark.asyncio
async def test_saga_consolidate_no_identities_yaml_no_subjects(tmp_path: Path):
    """When identities.yaml is missing, extra_canonical_subjects stays
    None — no failure, just seed-only behavior on the saga side."""
    captured: dict = {}

    class _FakeSagaClient:
        async def decay(self):
            return {"atoms_updated": 0, "transitions": 0}
        async def consolidate(self, **kwargs):
            captured.update(kwargs)
            return {}

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    sched.add_saga_consolidate_job(
        _FakeSagaClient(), "0 4 * * 0", home=tmp_path,
    )
    job = sched._scheduler.get_job("saga-consolidate")

    from mimir.event_logger import init_logger
    init_logger(tmp_path / "events.jsonl", session_id="test-session")

    await job.func()
    # Either absent or None — both mean "didn't pass canonical subjects."
    assert not captured.get("extra_canonical_subjects")


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
        def should_fire_heartbeat(self, **_kwargs):
            return False, "plan_window_saturated:7d_opus@0.92"

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
        def should_fire_heartbeat(self, **_kwargs):
            return True, "ok"

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


# ---- identities-populate cron ----------------------------------------


class _FakeRegistry:
    """Minimal stand-in for ChannelRegistry — only exposes ``bridges()``."""

    def __init__(self, bridges: list[object]) -> None:
        self._bridges = bridges

    def bridges(self) -> list[object]:
        return list(self._bridges)


class _FakeBridge:
    def __init__(self, name: str) -> None:
        self.name = name


def test_add_identities_populate_job_validates_cron(tmp_path: Path):
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    with pytest.raises(ValueError):
        sched.add_identities_populate_job(
            tmp_path, "this is not cron", _FakeRegistry([]),
        )


def test_add_identities_populate_job_disabled_when_blank(tmp_path: Path):
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched.add_identities_populate_job(
        tmp_path, "", _FakeRegistry([]),
    ) is False
    assert sched.add_identities_populate_job(
        tmp_path, "   ", _FakeRegistry([]),
    ) is False


def test_add_identities_populate_job_registers(tmp_path: Path):
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched.add_identities_populate_job(
        tmp_path, "0 6 * * *", _FakeRegistry([]),
    ) is True
    job = sched._scheduler.get_job("identities-populate")
    assert job is not None


@pytest.mark.asyncio
async def test_identities_populate_callback_resolves_bridges_by_name(
    tmp_path: Path, monkeypatch
):
    """Callback finds discord + slack bridges by ``bridge.name`` and
    threads them into populate_all."""
    captured: dict = {}

    async def _fake_populate_all(
        home, *, discord_bridge=None, slack_bridge=None, dry_run=False,
    ):
        captured["home"] = home
        captured["discord_bridge"] = discord_bridge
        captured["slack_bridge"] = slack_bridge
        captured["dry_run"] = dry_run
        return {"people_added": 0, "channels_added": 0}

    monkeypatch.setattr(
        "mimir.identities_populator.populate_all", _fake_populate_all,
    )

    discord = _FakeBridge("discord")
    slack = _FakeBridge("slack")
    web = _FakeBridge("web")  # ignored
    registry = _FakeRegistry([discord, web, slack])

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    sched.add_identities_populate_job(tmp_path, "0 6 * * *", registry)
    job = sched._scheduler.get_job("identities-populate")
    assert job is not None

    await job.func()

    assert captured["home"] == tmp_path
    assert captured["discord_bridge"] is discord
    assert captured["slack_bridge"] is slack
    assert captured["dry_run"] is False


@pytest.mark.asyncio
async def test_identities_populate_callback_handles_no_bridges(
    tmp_path: Path, monkeypatch
):
    """No connected bridges → populate_all called with both None and the
    callback completes cleanly. Populator's own contract handles the
    empty case as a no-op."""
    captured: dict = {}

    async def _fake_populate_all(
        home, *, discord_bridge=None, slack_bridge=None, dry_run=False,
    ):
        captured["discord_bridge"] = discord_bridge
        captured["slack_bridge"] = slack_bridge
        return {"people_added": 0, "channels_added": 0}

    monkeypatch.setattr(
        "mimir.identities_populator.populate_all", _fake_populate_all,
    )

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    sched.add_identities_populate_job(tmp_path, "0 6 * * *", _FakeRegistry([]))
    job = sched._scheduler.get_job("identities-populate")
    await job.func()

    assert captured["discord_bridge"] is None
    assert captured["slack_bridge"] is None


@pytest.mark.asyncio
async def test_identities_populate_callback_swallows_populator_errors(
    tmp_path: Path, monkeypatch
):
    """Best-effort scheduled job: a populator exception is logged via
    log_event but doesn't propagate (so APScheduler doesn't disable
    the job)."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated bridge failure")

    monkeypatch.setattr(
        "mimir.identities_populator.populate_all", _boom,
    )

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    sched.add_identities_populate_job(tmp_path, "0 6 * * *", _FakeRegistry([]))
    job = sched._scheduler.get_job("identities-populate")

    # Should not raise.
    await job.func()


# ---- Named-callable registry (chainlink #44 follow-up: scheduler.yaml
# unification for non-LLM crons; see state/spec/scheduler-callable-jobs.md) ----


def test_callable_yaml_field_round_trips(tmp_path: Path):
    """SchedulerJob.from_yaml_entry accepts ``callable: <name>`` and
    to_yaml_entry serializes it back."""
    raw = {"name": "saga-consolidate", "cron": "0 4 * * *",
           "callable": "saga-consolidate"}
    job = SchedulerJob.from_yaml_entry(raw)
    assert job.callable_name == "saga-consolidate"
    assert job.cron == "0 4 * * *"
    assert job.prompt == ""
    assert job.prompt_file is None
    serialized = job.to_yaml_entry()
    assert serialized["callable"] == "saga-consolidate"
    assert "channel_id" not in serialized  # callable entries don't carry channel_id


def test_callable_mutually_exclusive_with_prompt():
    """prompt + callable, prompt_file + callable, all three: rejected."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        SchedulerJob.from_yaml_entry({
            "name": "bad", "cron": "0 * * * *",
            "prompt": "x", "callable": "y",
        })
    with pytest.raises(ValueError, match="mutually exclusive"):
        SchedulerJob.from_yaml_entry({
            "name": "bad", "cron": "0 * * * *",
            "prompt_file": "x.md", "callable": "y",
        })


def test_callable_entry_rejects_time_of_day():
    """Callable entries take cron only — time_of_day is for prompt entries."""
    with pytest.raises(ValueError, match="time_of_day"):
        SchedulerJob.from_yaml_entry({
            "name": "bad", "time_of_day": "08:00", "callable": "y",
        })


def test_callable_entry_with_empty_cron_is_valid():
    """Empty cron on a callable entry is the operator's explicit
    'disable this callable for this deployment' signal — must parse."""
    job = SchedulerJob.from_yaml_entry({
        "name": "saga-consolidate", "callable": "saga-consolidate",
    })
    assert job.callable_name == "saga-consolidate"
    assert job.cron is None


def test_register_callable_installs_at_default_cron(tmp_path: Path):
    """No yaml override → APScheduler job installed at default_cron."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    async def _fn():
        return None
    assert sched.register_callable(
        "demo", _fn, default_cron="0 4 * * *",
    ) is True
    job = sched._scheduler.get_job("demo")
    assert job is not None
    assert "demo" in sched.registered_callables()


def test_register_callable_skips_install_when_cron_empty(tmp_path: Path):
    """Empty default cron + no yaml override → no APScheduler job
    installed; registration still recorded (so a yaml entry can later
    activate it)."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    async def _fn():
        return None
    assert sched.register_callable(
        "demo", _fn, default_cron="",
    ) is False
    assert sched._scheduler.get_job("demo") is None
    # Registration kept so future yaml override can install.
    assert "demo" in sched.registered_callables()


def test_register_callable_yaml_override_wins(tmp_path: Path):
    """yaml entry naming the callable beats default_cron."""
    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text(
        "- name: saga-nightly\n"
        "  callable: demo\n"
        "  cron: \"30 3 * * *\"\n",
        encoding="utf-8",
    )

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=yaml_path, enqueue=noop)

    async def _fn():
        return None
    assert sched.register_callable(
        "demo", _fn, default_cron="0 4 * * *",
    ) is True
    job = sched._scheduler.get_job("demo")
    assert job is not None
    # APScheduler stringifies the trigger with the cron fields.
    trigger_str = str(job.trigger)
    assert "minute='30'" in trigger_str
    assert "hour='3'" in trigger_str


def test_register_callable_yaml_explicit_disable(tmp_path: Path):
    """yaml entry with empty cron disables the callable, beating a
    non-empty default."""
    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text(
        "- name: disable-me\n"
        "  callable: demo\n",
        encoding="utf-8",
    )

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=yaml_path, enqueue=noop)

    async def _fn():
        return None
    assert sched.register_callable(
        "demo", _fn, default_cron="0 4 * * *",
    ) is False
    assert sched._scheduler.get_job("demo") is None
    assert "demo" in sched.registered_callables()


def test_register_callable_invalid_cron_raises(tmp_path: Path):
    """Invalid effective cron (default or yaml) raises ValueError."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    async def _fn():
        return None
    with pytest.raises(ValueError, match="invalid cron"):
        sched.register_callable(
            "demo", _fn, default_cron="not a cron",
        )


def test_reload_re_resolves_registered_callables(tmp_path: Path):
    """Adding a yaml entry post-startup + reload() updates the
    callable's cron in APScheduler."""
    yaml_path = tmp_path / "s.yaml"

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=yaml_path, enqueue=noop)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")
    job_before = sched._scheduler.get_job("demo")
    assert "hour='4'" in str(job_before.trigger)

    # Mutate yaml externally (simulating an add_schedule MCP call).
    yaml_path.write_text(
        "- name: saga-shifted\n"
        "  callable: demo\n"
        "  cron: \"0 6 * * *\"\n",
        encoding="utf-8",
    )

    sched.reload()
    job_after = sched._scheduler.get_job("demo")
    assert job_after is not None
    assert "hour='6'" in str(job_after.trigger)


def test_reload_warns_on_unregistered_callable(tmp_path: Path, caplog):
    """A yaml entry naming an unregistered callable is warn-skipped,
    not an error."""
    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text(
        "- name: stale\n"
        "  callable: never-registered\n"
        "  cron: \"0 4 * * *\"\n",
        encoding="utf-8",
    )

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=yaml_path, enqueue=noop)
    import logging
    with caplog.at_level(logging.WARNING):
        result = sched.reload()
    assert result == {"registered": 0, "invalid": 0}
    # The log should mention the unregistered callable.
    assert any("never-registered" in r.message for r in caplog.records)


def test_reload_skips_callable_yaml_entries_for_prompt_dispatch(tmp_path: Path):
    """Callable-typed yaml entries don't end up as scheduler:* prompt
    jobs in APScheduler — they're handled by the registry path only."""
    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text(
        "- name: saga-nightly\n"
        "  callable: demo\n"
        "  cron: \"0 4 * * *\"\n"
        "- name: morning-review\n"
        "  prompt: \"Review notes.\"\n"
        "  cron: \"0 8 * * *\"\n",
        encoding="utf-8",
    )

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=yaml_path, enqueue=noop)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="")

    result = sched.reload()
    # Only the prompt entry is counted as 'registered' (registered=1).
    assert result == {"registered": 1, "invalid": 0}

    # Prompt-style yaml entry → scheduler:morning-review.
    assert sched._scheduler.get_job("scheduler:morning-review") is not None
    # Callable-style yaml entry → demo (no scheduler: prefix).
    assert sched._scheduler.get_job("demo") is not None
    # No scheduler:saga-nightly should exist (it's a callable entry).
    assert sched._scheduler.get_job("scheduler:saga-nightly") is None


@pytest.mark.asyncio
async def test_add_job_validates_callable_is_registered(tmp_path: Path):
    """add_job MCP tool refuses to write a yaml entry for an
    unregistered callable — would be dead-on-arrival."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    job = SchedulerJob(
        name="bogus", callable_name="never-registered",
        cron="0 4 * * *",
    )
    with pytest.raises(ValueError, match="not registered"):
        await sched.add_job(job)


@pytest.mark.asyncio
async def test_add_job_with_registered_callable_persists(tmp_path: Path):
    """add_job for a registered callable writes yaml + reload picks
    up the new cron."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")

    job = SchedulerJob(
        name="demo-shifted", callable_name="demo",
        cron="0 6 * * *",
    )
    await sched.add_job(job)
    # yaml mutation triggered reload; callable picked up new cron.
    apjob = sched._scheduler.get_job("demo")
    assert "hour='6'" in str(apjob.trigger)


@pytest.mark.asyncio
async def test_add_job_callable_with_empty_cron_disables(tmp_path: Path):
    """add_job with callable + empty cron is the explicit-disable
    path; yaml gets the entry, APScheduler drops the job."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    async def _fn():
        return None
    sched.register_callable("demo", _fn, default_cron="0 4 * * *")
    assert sched._scheduler.get_job("demo") is not None

    job = SchedulerJob(
        name="disable-demo", callable_name="demo", cron=None,
    )
    await sched.add_job(job)
    assert sched._scheduler.get_job("demo") is None
    # Registration still in place — operator can re-enable later.
    assert "demo" in sched.registered_callables()


# ---- pollers framework integration (chainlink #3) ----------------------


import json as _json


def _drop_pollers_skill(skills_dir: Path, name: str, cron: str = "* * * * *") -> Path:
    """Helper: build a minimal valid skill dir with a no-op poller."""
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "pollers.json").write_text(_json.dumps({
        "pollers": [{"name": name, "command": "true", "cron": cron}],
    }), encoding="utf-8")
    return skill


@pytest.mark.asyncio
async def test_add_poller_jobs_returns_zero_when_skills_dir_missing(tmp_path: Path):
    """No skills directory → no pollers, no error. Most installs."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    n = sched.add_poller_jobs(tmp_path / "no-such-dir")
    assert n == 0
    assert sched.registered_pollers() == []


@pytest.mark.asyncio
async def test_add_poller_jobs_registers_apscheduler_jobs(tmp_path: Path):
    """Each discovered poller becomes an APScheduler job with a
    ``poller:<name>`` id — visible to APScheduler's get_job()."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "p1")
    _drop_pollers_skill(skills, "p2")

    n = sched.add_poller_jobs(skills)
    assert n == 2
    assert sched.registered_pollers() == ["p1", "p2"]
    assert sched._scheduler.get_job("poller:p1") is not None
    assert sched._scheduler.get_job("poller:p2") is not None


@pytest.mark.asyncio
async def test_reload_pollers_picks_up_new_skill(tmp_path: Path):
    """The MCP-tool path: agent installs a new skill, calls
    reload_pollers, the new skill goes live without a container
    restart."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "first")
    n = sched.add_poller_jobs(skills)
    assert n == 1
    assert sched.registered_pollers() == ["first"]

    # Drop a second skill and reload.
    _drop_pollers_skill(skills, "second")
    n2 = await sched.reload_pollers()
    assert n2["total"] == 2
    assert sched.registered_pollers() == ["first", "second"]
    assert sched._scheduler.get_job("poller:second") is not None


@pytest.mark.asyncio
async def test_reload_pollers_drops_removed_skills(tmp_path: Path):
    """Removing a skill's pollers.json (or the whole skill) and
    reloading must drop the corresponding APScheduler job — otherwise
    a removed skill keeps firing forever."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "to-keep")
    skill_to_drop = _drop_pollers_skill(skills, "to-drop")
    sched.add_poller_jobs(skills)
    assert "to-drop" in sched.registered_pollers()

    # Delete the skill's pollers.json (simulating an uninstall).
    (skill_to_drop / "pollers.json").unlink()
    n = await sched.reload_pollers()
    assert n["total"] == 1
    assert sched.registered_pollers() == ["to-keep"]
    assert sched._scheduler.get_job("poller:to-drop") is None


@pytest.mark.asyncio
async def test_reload_pollers_no_op_when_never_added(tmp_path: Path):
    """reload_pollers before add_poller_jobs is a no-op (returns 0).
    Protects the MCP tool from being called too early or in tests
    that didn't wire the framework."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    n = await sched.reload_pollers()
    assert n["total"] == 0


@pytest.mark.asyncio
async def test_add_poller_jobs_skips_invalid_cron(tmp_path: Path):
    """A poller with a malformed cron logs a warning and gets dropped;
    other pollers in the same dir still register."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    bad = skills / "bad"
    bad.mkdir(parents=True)
    (bad / "pollers.json").write_text(_json.dumps({
        "pollers": [
            {"name": "bad", "command": "x", "cron": "not a cron"},
        ],
    }), encoding="utf-8")
    _drop_pollers_skill(skills, "good")
    n = sched.add_poller_jobs(skills)
    assert n == 1
    assert sched.registered_pollers() == ["good"]


# ─── PR #107: APScheduler misfire visibility + concurrency cap + lock ─


@pytest.mark.asyncio
async def test_pollers_misfire_grace_time_is_5s(tmp_path: Path):
    """Poller registrations use ``misfire_grace_time=5`` (was 60).
    The lower value + EVENT_JOB_MISSED listener means a 60s-timeout
    poller that overruns into the next minute's fire emits a
    ``poller_misfired`` event instead of silently dropping."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "p1")
    sched.add_poller_jobs(skills)
    job = sched._scheduler.get_job("poller:p1")
    assert job is not None
    assert job.misfire_grace_time == 5


@pytest.mark.asyncio
async def test_pollers_concurrency_cap_default_is_8(tmp_path: Path):
    """``MIMIR_MAX_CONCURRENT_POLLERS`` defaults to 8. The semaphore
    is constructed in __init__ so reading at runtime confirms the
    binding."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched._poller_concurrency_cap == 8
    # Semaphore starts fully available — 8 acquire-without-block possible.
    for _ in range(8):
        assert not sched._poller_semaphore.locked()
        await sched._poller_semaphore.acquire()
    assert sched._poller_semaphore.locked()
    # Release for cleanup.
    for _ in range(8):
        sched._poller_semaphore.release()


@pytest.mark.asyncio
async def test_pollers_concurrency_cap_env_override(
    tmp_path: Path, monkeypatch
):
    """``MIMIR_MAX_CONCURRENT_POLLERS=N`` overrides the default."""
    async def noop(_e):
        return True
    monkeypatch.setenv("MIMIR_MAX_CONCURRENT_POLLERS", "3")
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched._poller_concurrency_cap == 3


@pytest.mark.asyncio
async def test_pollers_concurrency_cap_invalid_env_falls_back(
    tmp_path: Path, monkeypatch
):
    """Garbage value in the env var → default 8 (no crash)."""
    async def noop(_e):
        return True
    monkeypatch.setenv("MIMIR_MAX_CONCURRENT_POLLERS", "not a number")
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched._poller_concurrency_cap == 8


@pytest.mark.asyncio
async def test_pollers_concurrency_cap_clamps_to_one(
    tmp_path: Path, monkeypatch
):
    """Zero / negative → 1 (degenerate single-fire mode, not 0
    which would deadlock)."""
    async def noop(_e):
        return True
    monkeypatch.setenv("MIMIR_MAX_CONCURRENT_POLLERS", "0")
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert sched._poller_concurrency_cap == 1


@pytest.mark.asyncio
async def test_reinstall_pollers_keeps_present_pollers_in_dict(tmp_path: Path):
    """PR #107 review fix: ``_reinstall_pollers`` no longer
    clears-then-rebuilds; it does in-place per-entry pre-population
    (each new poller is set in ``self._pollers[name]`` BEFORE its
    ``add_job`` call) and removes only stale entries after discovery.
    A reload-with-same-skills-present produces no observable gap in
    the dict — no entry is ever absent during the reload window.

    See ``test_reinstall_pollers_pre_populates_dict_before_add_job``
    for the direct ordering proof; this test exercises the
    same-set-reload happy path."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "p1")
    sched.add_poller_jobs(skills)
    assert "p1" in sched._pollers
    poller_before = sched._pollers["p1"]

    # Reload with the same skill present — entry stays in the dict
    # throughout (in-place update, not clear-and-rebuild).
    n = await sched.reload_pollers()
    assert n["total"] == 1
    assert "p1" in sched._pollers
    # The poller config is re-loaded but identity-equivalent.
    assert sched._pollers["p1"].name == poller_before.name
    assert sched._pollers["p1"].cron == poller_before.cron


@pytest.mark.asyncio
async def test_reinstall_pollers_drops_removed_skills_from_dict(
    tmp_path: Path,
):
    """Stale entries cleanup: a poller that was registered last time
    but is gone from disk this time gets removed from the dict via
    the new-names diff (computed up-front from the discovery pass)."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "to-drop")
    _drop_pollers_skill(skills, "to-keep")
    sched.add_poller_jobs(skills)
    assert "to-drop" in sched._pollers
    assert "to-keep" in sched._pollers

    # Remove the to-drop skill on disk, then reload.
    import shutil
    shutil.rmtree(skills / "to-drop")
    n = await sched.reload_pollers()
    assert n["total"] == 1
    assert "to-keep" in sched._pollers
    # Stale entry dropped.
    assert "to-drop" not in sched._pollers


@pytest.mark.asyncio
async def test_reload_pollers_serializes_via_mutate_lock(tmp_path: Path):
    """``reload_pollers`` acquires ``self._mutate_lock`` to serialize
    against concurrent ``add_job`` / ``remove_job`` mutations.
    Exercise the lock path; the test passes if reload completes
    without a race-induced crash."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "p1")
    sched.add_poller_jobs(skills)

    # Race two concurrent reloads + a yaml-mutation through the lock.
    job = SchedulerJob(name="t", cron="*/5 * * * *", prompt="ping")
    results = await asyncio.gather(
        sched.reload_pollers(),
        sched.reload_pollers(),
        sched.add_job(job),
        return_exceptions=True,
    )
    # All three return without exceptions (lock serialized them).
    assert all(not isinstance(r, BaseException) for r in results), results
    # Pollers still registered after the chaos.
    assert sched._pollers.get("p1") is not None


# ─── PR #107 review-fix: misfire listener + behavioral concurrency cap ─


@pytest.mark.asyncio
async def test_on_job_missed_emits_poller_misfired_for_poller_jobs(
    tmp_path: Path, monkeypatch
):
    """The EVENT_JOB_MISSED listener emits ``poller_misfired`` when
    the missed job's id is prefixed ``poller:``. PR #107 review fix —
    the headline behavioral change wasn't covered by the original
    tests, so a future refactor that broke the listener wiring would
    go undetected."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import patch

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    # Capture log_event calls.
    captured: list[tuple[str, dict]] = []

    async def fake_log_event(event_type: str, **kwargs):
        captured.append((event_type, kwargs))

    with patch("mimir.scheduler.log_event", new=fake_log_event):
        # Synthesize the EVENT_JOB_MISSED payload. apscheduler's
        # JobExecutionEvent has these fields; SimpleNamespace is
        # enough for our listener.
        fake_event = SimpleNamespace(
            job_id="poller:my-skill",
            scheduled_run_time=datetime(
                2026, 5, 10, 12, 0, tzinfo=timezone.utc,
            ),
        )
        sched._on_job_missed(fake_event)
        # The listener schedules the log call as a task; yield once.
        await asyncio.sleep(0)

    assert len(captured) == 1
    event_type, kwargs = captured[0]
    assert event_type == "poller_misfired"
    assert kwargs["job_id"] == "poller:my-skill"
    assert kwargs["scheduled_run_time"] == "2026-05-10T12:00:00+00:00"


@pytest.mark.asyncio
async def test_on_job_missed_emits_scheduled_job_misfired_for_other_jobs(
    tmp_path: Path,
):
    """Non-poller jobs (LLM ticks, OAuth quota poll, callable jobs)
    emit ``scheduled_job_misfired`` so the operator can disambiguate
    poller-cron mismatches from other scheduling problems."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import patch

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    captured: list[tuple[str, dict]] = []

    async def fake_log_event(event_type: str, **kwargs):
        captured.append((event_type, kwargs))

    with patch("mimir.scheduler.log_event", new=fake_log_event):
        fake_event = SimpleNamespace(
            job_id="scheduler:morning-ping",
            scheduled_run_time=datetime(
                2026, 5, 10, 8, 0, tzinfo=timezone.utc,
            ),
        )
        sched._on_job_missed(fake_event)
        await asyncio.sleep(0)

    assert len(captured) == 1
    event_type, _ = captured[0]
    assert event_type == "scheduled_job_misfired"


@pytest.mark.asyncio
async def test_fire_poller_serializes_through_semaphore(
    tmp_path: Path, monkeypatch
):
    """Behavioral test of the concurrency cap: spawn N+1 concurrent
    ``_fire_poller`` calls with a mocked ``run_poller`` that sleeps,
    assert max concurrency stays <= cap. Pre-PR #107 a buggy skill
    with many siblings could fork-bomb the host; this proves the
    semaphore actually serializes."""
    monkeypatch.setenv("MIMIR_MAX_CONCURRENT_POLLERS", "2")

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    for n in ("p1", "p2", "p3", "p4"):
        _drop_pollers_skill(skills, n)
    sched.add_poller_jobs(skills)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_run_poller(poller, enqueue):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        # Hold the slot long enough that all 4 fires overlap if
        # unbounded.
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

    monkeypatch.setattr("mimir.scheduler.run_poller", fake_run_poller)

    # Fire 4 concurrent _fire_poller calls. Cap is 2; the other 2 wait.
    await asyncio.gather(
        sched._fire_poller(poller_name="p1"),
        sched._fire_poller(poller_name="p2"),
        sched._fire_poller(poller_name="p3"),
        sched._fire_poller(poller_name="p4"),
    )
    assert max_in_flight <= 2, (
        f"semaphore cap=2 failed; max_in_flight={max_in_flight}"
    )
    # All four eventually completed.
    assert in_flight == 0


@pytest.mark.asyncio
async def test_reinstall_pollers_pre_populates_dict_before_add_job(
    tmp_path: Path, monkeypatch
):
    """PR #107 review fix: ``self._pollers[name]`` is set BEFORE
    ``add_job`` so a fire that lands during job registration finds
    the poller in the dict.

    Verifies the ordering by intercepting ``add_job`` with a callback
    that probes ``self._pollers``. If pre-population didn't happen,
    the probe would see the missing key and the assertion would fail.
    """
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "p1")

    # Wrap add_job to probe self._pollers at call time.
    real_add_job = sched._scheduler.add_job
    probed_states: list[bool] = []

    def probe_add_job(*args, **kwargs):
        # Look up the poller name from the kwargs payload.
        name = kwargs.get("kwargs", {}).get("poller_name")
        # The poller MUST already be in self._pollers by now (PR
        # #107 ordering fix). Pre-fix, this would be False.
        probed_states.append(name in sched._pollers)
        return real_add_job(*args, **kwargs)

    monkeypatch.setattr(sched._scheduler, "add_job", probe_add_job)

    sched.add_poller_jobs(skills)

    assert probed_states == [True], (
        "self._pollers[name] must be set BEFORE add_job; "
        f"got probe states {probed_states}"
    )


# ─── chainlink #84: preserve pollers on malformed-manifest reload ────


@pytest.mark.asyncio
async def test_reinstall_pollers_preserves_entries_from_corrupted_manifest(
    tmp_path: Path,
):
    """chainlink #84 acceptance: an operator edits a working
    ``pollers.json`` and introduces a syntax error. On the next
    reload, the previously-installed poller from that manifest must
    NOT be silently dropped — the pre-edit cron job stays registered,
    the dict entry stays put. Sibling skills with clean manifests
    reload normally."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "doomed-then-saved")
    _drop_pollers_skill(skills, "always-fine")
    sched.add_poller_jobs(skills)
    assert "doomed-then-saved" in sched._pollers
    assert "always-fine" in sched._pollers
    pre_edit_cfg = sched._pollers["doomed-then-saved"]

    # Operator-typo simulation: corrupt the manifest mid-edit.
    (skills / "doomed-then-saved" / "pollers.json").write_text(
        "{ this is not json", encoding="utf-8",
    )

    n = await sched.reload_pollers()

    # Both pollers still present in the live dict — the corrupted-
    # manifest one was preserved (chainlink #84 fix), the clean one
    # reloaded normally.
    assert "always-fine" in sched._pollers
    assert "doomed-then-saved" in sched._pollers
    # Preserved config is the SAME object (in-place preservation,
    # not a re-add with new identity) — proves the scheduler kept
    # the prior PollerConfig rather than constructing a fresh one
    # from a manifest it couldn't parse.
    assert sched._pollers["doomed-then-saved"] is pre_edit_cfg
    # PR #141 review item #2: ``reload_pollers`` returns the live
    # total (preserved + freshly-installed), not just Phase 3
    # installs — matches ``registered_pollers()`` semantics so the
    # MCP reply's count agrees with the names list. Pre-fix this
    # returned 1 (only the cleanly-reinstalled poller); now returns
    # 2 (the preserved one + the cleanly-reinstalled one).
    assert n["total"] == 2, (
        "reload_pollers should return the live total "
        "(preserved + fresh), matching registered_pollers()"
    )
    assert n["total"] == len(sched.registered_pollers())

    # APScheduler job for the preserved poller is still registered.
    job_ids = {j.id for j in sched._scheduler.get_jobs()}
    assert "poller:doomed-then-saved" in job_ids
    assert "poller:always-fine" in job_ids


@pytest.mark.asyncio
async def test_reload_pollers_emits_invalid_manifest_event(
    tmp_path: Path,
):
    """chainlink #84: when a manifest fails to JSON-parse on reload,
    a ``poller_reload_invalid_manifest`` algedonic event lands in
    events.jsonl with the failing path, the parse error message, and
    the list of preserved poller names. This is the algedonic signal
    the operator needs — without it, a silent drop would show up only
    in mysteriously-stale reload-tool reply counts."""
    from unittest.mock import patch

    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "p1")
    _drop_pollers_skill(skills, "p2")
    sched.add_poller_jobs(skills)

    # Corrupt p1's manifest.
    bad_path = skills / "p1" / "pollers.json"
    bad_path.write_text("garbage {", encoding="utf-8")

    captured: list[tuple[str, dict]] = []

    async def fake_log_event(event_type: str, **kwargs):
        captured.append((event_type, kwargs))

    with patch("mimir.scheduler.log_event", new=fake_log_event):
        await sched.reload_pollers()

    invalid_events = [
        (et, kw) for et, kw in captured
        if et == "poller_reload_invalid_manifest"
    ]
    assert len(invalid_events) == 1
    _et, payload = invalid_events[0]
    assert payload["manifest_path"] == str(bad_path)
    assert "JSONDecodeError" in payload["error"]
    # The preserved-pollers list carries the names that were rescued
    # from the broken manifest path (here, just "p1").
    assert payload["preserved_pollers"] == ["p1"]


@pytest.mark.asyncio
async def test_reload_pollers_drops_poller_when_manifest_deleted(
    tmp_path: Path,
):
    """Negative case for chainlink #84 fix: if a ``pollers.json`` is
    REMOVED entirely (not corrupted, just absent — operator
    intentionally uninstalled the skill), the poller from it must
    still be dropped on reload. Preserve-on-parse-fail must NOT
    over-correct into preserve-on-anything-missing."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "to-uninstall")
    _drop_pollers_skill(skills, "to-keep")
    sched.add_poller_jobs(skills)
    assert "to-uninstall" in sched._pollers

    # Remove the manifest entirely (clean uninstall — file gone).
    import shutil
    shutil.rmtree(skills / "to-uninstall")
    n = await sched.reload_pollers()
    assert n["total"] == 1
    assert "to-keep" in sched._pollers
    # Clean deletion still drops the poller. This is the explicit
    # negative-case guard against over-correcting the chainlink #84
    # fix into "preserve on anything missing".
    assert "to-uninstall" not in sched._pollers


@pytest.mark.asyncio
async def test_reload_pollers_preserves_apscheduler_job_for_broken_manifest(
    tmp_path: Path,
):
    """The poller_invalid_manifest fix must keep the APScheduler job
    registered too — not just the dict entry. Otherwise the dict
    would carry a stale config that never fires, which is just
    "silent drop" by a different name."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "keep-firing")
    sched.add_poller_jobs(skills)
    assert sched._scheduler.get_job("poller:keep-firing") is not None

    (skills / "keep-firing" / "pollers.json").write_text(
        "{ broken json", encoding="utf-8",
    )
    await sched.reload_pollers()

    # Job still registered with APScheduler — fires will continue
    # using the last-known-good cron.
    job = sched._scheduler.get_job("poller:keep-firing")
    assert job is not None, (
        "APScheduler job for poller with corrupted manifest must be "
        "preserved (chainlink #84) — dict entry alone isn't enough."
    )


# ─── MIMIR_SCHEDULER_TZ — configurable scheduler timezone ──────────────


def test_resolve_tz_returns_zoneinfo_for_valid_name():
    """ZoneInfo lookup for canonical IANA names succeeds.

    ``America/New_York`` is the canonical zone Muninn deploys in;
    ``UTC`` is the back-compat default. Both must resolve without
    falling back."""
    from zoneinfo import ZoneInfo

    ny = _resolve_tz("America/New_York")
    assert isinstance(ny, ZoneInfo)
    assert str(ny) == "America/New_York"

    utc = _resolve_tz("UTC")
    assert isinstance(utc, ZoneInfo)
    assert str(utc) == "UTC"


def test_resolve_tz_falls_back_to_utc_on_invalid(caplog):
    """A typo / unknown zone falls back to UTC with a logged warning
    rather than crashing the scheduler. Wrong-but-functioning beats
    agent-offline-on-startup."""
    from zoneinfo import ZoneInfo

    import logging
    caplog.set_level(logging.WARNING, logger="mimir.scheduler")
    result = _resolve_tz("Earth/Atlantis")
    assert isinstance(result, ZoneInfo)
    assert str(result) == "UTC"
    # Warning should mention the bad value so the operator sees it.
    assert any(
        "Earth/Atlantis" in record.message for record in caplog.records
    )


def test_scheduler_default_tz_is_utc(tmp_path: Path):
    """Constructing Scheduler without ``scheduler_tz`` keeps the
    pre-PR behavior (UTC). Mimirbot relies on this — it doesn't set
    ``MIMIR_SCHEDULER_TZ`` and its scheduler.yaml is UTC-shaped."""
    async def noop(_e):
        return True
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
    assert str(sched._tz) == "UTC"


def test_scheduler_honors_configured_tz(tmp_path: Path):
    """When ``scheduler_tz`` is set, the underlying APScheduler is
    constructed with that zone — so cron expressions in scheduler.yaml
    fire in the configured local time, DST-aware via system tzdata."""
    async def noop(_e):
        return True
    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml",
        enqueue=noop,
        scheduler_tz="America/New_York",
    )
    assert str(sched._tz) == "America/New_York"
    # The internal AsyncIOScheduler must also carry that zone — without
    # this, _build_trigger could be in ET but the scheduler itself would
    # mis-interpret missed-job / coalesce windows.
    assert str(sched._scheduler.timezone) == "America/New_York"


def test_scheduler_invalid_tz_falls_back_to_utc(tmp_path: Path, caplog):
    """Misconfigured operator (typo in ``MIMIR_SCHEDULER_TZ``) gets a
    working scheduler in UTC + a logged warning, not a crash on
    startup."""
    import logging
    async def noop(_e):
        return True
    caplog.set_level(logging.WARNING, logger="mimir.scheduler")
    sched = Scheduler(
        scheduler_yaml=tmp_path / "s.yaml",
        enqueue=noop,
        scheduler_tz="Definitely/NotARealZone",
    )
    assert str(sched._tz) == "UTC"
    assert any(
        "Definitely/NotARealZone" in record.message
        for record in caplog.records
    )


def test_build_trigger_threads_tz_through_to_cron():
    """``_build_trigger`` must build CronTriggers anchored in the
    passed-in tz so the scheduler can interpret each cron line in
    the operator's local wall-clock time."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    job = SchedulerJob(name="briefing", prompt="x", cron="0 8 * * *")
    trigger = _build_trigger(job, ny)
    # APScheduler's CronTrigger stores its zone; str() round-trip
    # confirms the right zone landed.
    assert str(trigger.timezone) == "America/New_York"


def test_build_trigger_defaults_to_utc_when_tz_omitted():
    """Back-compat: bench/test call sites that haven't been updated
    to pass tz must still get a UTC-anchored trigger (matches pre-PR
    behavior)."""
    job = SchedulerJob(name="x", prompt="y", cron="0 0 * * *")
    trigger = _build_trigger(job)
    assert str(trigger.timezone) == "UTC"


@pytest.mark.asyncio
async def test_commitments_due_check_error_includes_traceback(
    tmp_path: Path, monkeypatch
):
    """commitments_due_check_error event must include a ``traceback``
    field so operators can debug cron-fired failures without a repro
    (chainlink #99)."""
    import json

    events_path = tmp_path / "events.jsonl"
    init_logger(events_path, session_id="test-cdc-err")

    async def noop(_e):
        return True

    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    # Stub check_due_and_expired to raise unconditionally.
    import mimir.commitments.poller as _poller_mod

    async def _raise(*a, **kw):
        raise RuntimeError("boom from test")

    monkeypatch.setattr(_poller_mod, "check_due_and_expired", _raise)

    # Use a sentinel store object — won't be called because the stub raises.
    sched.add_commitments_due_check_job(
        object(), "*/5 * * * *"  # type: ignore[arg-type]
    )
    job = sched._scheduler.get_job("commitments-due-check")
    assert job is not None

    await job.func()

    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    error_events = [e for e in events if e.get("type") == "commitments_due_check_error"]
    assert len(error_events) == 1, f"expected 1 error event, got {error_events}"
    evt = error_events[0]
    # Payload fields are flat (EventLogger.record does rec.update(payload)).
    assert "RuntimeError" in evt["error"]
    assert "traceback" in evt, "traceback field missing from error event (chainlink #99)"
    assert "RuntimeError" in evt["traceback"]
    assert "boom from test" in evt["traceback"]


# ─── saga_consolidate_error traceback (PR #345 follow-up) ─────────────


@pytest.mark.asyncio
async def test_saga_consolidate_error_includes_traceback(tmp_path: Path):
    """When the saga-consolidate cron raises, the emitted
    ``saga_consolidate_error`` event must include a ``traceback`` field
    so operators can diagnose without trawling container logs. Same
    pattern PR #345 applied to ``commitments_due_check_error`` —
    extended here to the next-closest error event for consistency.
    """
    import json
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    class _RaisingSagaClient:
        async def consolidate(self, **kwargs):
            raise RuntimeError("boom from saga consolidate")

    async def noop(_e):
        return True

    events_path = tmp_path / "events.jsonl"
    init_logger(events_path, session_id="test-saga-consolidate-tb")
    try:
        sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)
        sched.add_saga_consolidate_job(
            _RaisingSagaClient(), "0 4 * * 0", home=tmp_path,
        )
        job = sched._scheduler.get_job("saga-consolidate")
        assert job is not None
        await job.func()

        # Find the error event.
        events = [
            json.loads(line) for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        err_events = [e for e in events if e.get("type") == "saga_consolidate_error"]
        assert len(err_events) == 1, f"expected 1 error event, got {len(err_events)}"
        ev = err_events[0]
        # Traceback is captured + names the exception.
        assert "traceback" in ev
        assert "RuntimeError" in ev["traceback"]
        assert "boom from saga consolidate" in ev["traceback"]
    finally:
        _reset_logger_for_tests()


# ─── chainlink #118: asyncio strong-ref for fire-and-forget tasks ────────────


@pytest.mark.asyncio
async def test_on_job_missed_task_is_held_in_background_tasks(tmp_path: Path):
    """_on_job_missed uses _spawn() which holds a strong ref in
    _background_tasks until the task completes (chainlink #118).
    Regression: bare loop.create_task() without a retained reference
    could be GC'd before it finishes on a busy event loop."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import patch

    async def noop(_e):
        return True

    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    log_started = asyncio.Event()
    log_unblocked = asyncio.Event()

    async def blocking_log_event(event_type: str, **kwargs):
        log_started.set()
        await log_unblocked.wait()

    with patch("mimir.scheduler.log_event", new=blocking_log_event):
        fake_event = SimpleNamespace(
            job_id="poller:my-skill",
            scheduled_run_time=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )
        sched._on_job_missed(fake_event)
        # Yield until the task starts — proves it was actually scheduled.
        await asyncio.wait_for(log_started.wait(), timeout=1.0)

        # While the task is suspended, the strong-ref set must hold it.
        assert len(sched._background_tasks) == 1, (
            "Expected 1 in-flight task in _background_tasks"
        )

        # Unblock the task and let it finish.
        log_unblocked.set()
        # Two yields: first lets the task run to completion; second lets
        # the loop.call_soon-scheduled done_callback (discard) execute.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # After completion the done-callback must have removed the entry.
    assert len(sched._background_tasks) == 0, (
        "_background_tasks should be empty after task completes"
    )


@pytest.mark.asyncio
async def test_dispatch_invalid_manifest_events_task_is_held_in_background_tasks(
    tmp_path: Path,
):
    """_dispatch_invalid_manifest_events uses _spawn() which holds strong refs
    for each event task until completion (chainlink #118)."""
    from unittest.mock import patch

    async def noop(_e):
        return True

    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=noop)

    logged: list[str] = []
    log_unblocked = asyncio.Event()

    async def blocking_log_event(event_type: str, **kwargs):
        logged.append(event_type)
        await log_unblocked.wait()

    events = [
        {"manifest_path": "/a.json", "error": "bad json"},
        {"manifest_path": "/b.json", "error": "missing field"},
    ]
    with patch("mimir.scheduler.log_event", new=blocking_log_event):
        sched._dispatch_invalid_manifest_events(events)
        # Yield to let tasks start.
        await asyncio.sleep(0)

        # Both tasks should be in flight.
        assert len(sched._background_tasks) == 2, (
            f"Expected 2 in-flight tasks, got {len(sched._background_tasks)}"
        )

        log_unblocked.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(sched._background_tasks) == 0
    assert len(logged) == 2
