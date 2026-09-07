from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.commitments.models import CommitmentRecord, CommitmentVisibility
from mimir.commitments.store import CommitmentsStore
from mimir.models import AgentEvent
from mimir.pollers import PollerConfig
from mimir.scheduler import SCHEDULER_CHANNEL_PREFIX, Scheduler
from mimir.scheduler_dashboard import build_scheduler_dashboard_payload


@pytest.fixture
def dashboard_scheduler(tmp_path):
    job = SimpleNamespace(id=f"{SCHEDULER_CHANNEL_PREFIX}example", kwargs={"job": SimpleNamespace(name="example")})
    return SimpleNamespace(
        _scheduler=SimpleNamespace(get_jobs=lambda: [job]),
        _pollers={"example": PollerConfig("example", "true", "* * * * *", {}, tmp_path)},
    )


@pytest.mark.parametrize("error_kind", [
    "poller_misfired", "poller_nonzero_exit", "poller_timeout", "poller_exec_error",
    "poller_enqueue_error", "poller_event_rejected", "poller_circuit_open",
    "poller_missing_required_env", "scheduled_tick_dropped", "scheduled_job_misfired",
])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("suppressed", [False, True])
@pytest.mark.parametrize("success_ts,error_ts,recovered", [
    pytest.param("2026-09-07T12:00:00Z", "2026-09-06T12:00:00Z", True, id="success-after-error"),
    pytest.param("2026-09-06T12:00:00Z", "2026-09-07T12:00:00Z", False, id="error-after-success"),
    pytest.param(None, "2026-09-07T12:00:00Z", False, id="no-success"),
    pytest.param("2026-09-07T12:00:00Z", "2026-09-07T12:00:00Z", False, id="tie"),
    pytest.param("2026-09-07T12:00:00Z", None, False, id="missing-error-time"),
    pytest.param("2026-09-07T12:00:00Z", "bad", False, id="invalid-error-time"),
    pytest.param("bad", "2026-09-07T12:00:00Z", False, id="invalid-success-time"),
    pytest.param("", "2026-09-07T12:00:00Z", False, id="missing-success-time"),
    pytest.param("2026-09-07T12:00:00", "2026-09-06T12:00:00Z", False, id="naive-success-time"),
    pytest.param("2026-09-07T12:00:00Z", "2026-09-06T12:00:00", False, id="naive-error-time"),
    pytest.param("2026-09-07T11:00:00Z", "2026-09-07T12:00:00+02:00", True, id="offset-recovery"),
    pytest.param("2026-09-07T12:00:00+02:00", "2026-09-07T11:00:00Z", False, id="offset-error"),
])
def test_dashboard_error_recovery(dashboard_scheduler, error_kind, reverse, suppressed, success_ts, error_ts, recovered):
    poller = error_kind.startswith("poller_")
    name_key = "poller" if poller else "schedule_name"
    success_kind = "poller_complete" if poller else "scheduled_tick"
    suppressed_kind = "poller_fire_suppressed" if poller else "scheduled_tick_suppressed"
    events = [
        {"type": error_kind, name_key: "example", "timestamp": error_ts},
    ]
    if suppressed:
        events.append({"type": suppressed_kind, name_key: "example", "timestamp": "2026-09-08T12:00:00Z", "reason": "quota"})
    if success_ts is not None:
        events.append({"type": success_kind, name_key: "example", "ts": success_ts})
    if reverse:
        events.reverse()
    payload = build_scheduler_dashboard_payload(scheduler=dashboard_scheduler, commitments_store=None, events=events)
    row = payload["pollers" if poller else "schedules"][0]
    assert row["recent_error"] == (None if recovered else error_kind)
    assert row["suppression_reason"] == ("quota" if suppressed else None)
    assert row["recent_result"] == (None if success_ts is None else "emitted=0 rejected=0" if poller else "scheduled_tick")
    if error_ts and all(ts is None or ts.endswith("Z") or "+" in ts for ts in (success_ts, error_ts)):
        assert row["last_run_at"] == ("2026-09-08T12:00:00Z" if suppressed else success_ts if recovered else error_ts)


@pytest.mark.parametrize("poller", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("error_ts", [None, "bad", "2026-09-07T09:00:00-04:00"])
def test_dashboard_does_not_lose_unorderable_or_newer_errors(dashboard_scheduler, poller, reverse, error_ts):
    name_key = "poller" if poller else "schedule_name"
    error_kind = "poller_misfired" if poller else "scheduled_job_misfired"
    success_kind = "poller_complete" if poller else "scheduled_tick"
    events = [
        {"type": error_kind, name_key: "example", "timestamp": "2026-09-07T10:00:00Z"},
        {"type": error_kind, name_key: "example", "timestamp": error_ts, "reason": "still failing"},
        {"type": success_kind, name_key: "example", "timestamp": "2026-09-07T12:00:00Z"},
    ]
    if reverse:
        events.reverse()
    payload = build_scheduler_dashboard_payload(scheduler=dashboard_scheduler, commitments_store=None, events=events)
    row = payload["pollers" if poller else "schedules"][0]
    assert row["recent_error"] == "still failing"
    if error_ts and error_ts != "bad":
        assert row["last_run_at"] == error_ts


def _drop_pollers_skill(
    skills_dir: Path,
    name: str,
    cron: str = "*/5 * * * *",
    *,
    skill_name: str | None = None,
) -> Path:
    skill = skills_dir / (skill_name or name)
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "pollers.json").write_text(json.dumps({
        "pollers": [{"name": name, "command": "true", "cron": cron}],
    }), encoding="utf-8")
    return skill


@pytest.mark.asyncio
async def test_scheduler_dashboard_renders_owned_private_commitments(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "commitments.jsonl")
    await store.add(CommitmentRecord(
        id="c-private",
        channel_id="alice-channel",
        text="Alice owned commitment",
        owner_principal="alice",
        visibility=CommitmentVisibility.PRIVATE.value,
    ))

    payload = build_scheduler_dashboard_payload(
        scheduler=None,
        commitments_store=store,
        events=[],
    )

    assert [(row["id"], row["text"]) for row in payload["commitments"]] == [
        ("c-private", "Alice owned commitment")
    ]


@pytest.mark.asyncio
async def test_scheduler_dashboard_surfaces_poller_usage(tmp_path: Path):
    async def noop(_event: AgentEvent) -> bool:
        return True

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "turns.jsonl").write_text(
        json.dumps({
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "channel_id": "poller:github-activity",
            "total_cost_usd": 0.25,
        }) + "\n",
        encoding="utf-8",
    )
    sched = Scheduler(
        scheduler_yaml=tmp_path / "scheduler.yaml",
        enqueue=noop,
        home=tmp_path,
    )
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "github-activity", skill_name="github-poller")
    sched.add_poller_jobs(skills)

    payload = build_scheduler_dashboard_payload(
        scheduler=sched,
        commitments_store=None,
        events=[],
    )

    row = payload["pollers"][0]
    assert row["name"] == "github-activity"
    assert row["usage"]["poller"] == "github-activity"
    assert row["usage"]["windows"]["1h"]["agent_turns"] == 1
    assert row["usage"]["windows"]["1h"]["total_cost_usd"] == 0.25


@pytest.mark.asyncio
async def test_scheduler_dashboard_surfaces_missing_poller_cost_as_null(tmp_path: Path):
    async def noop(_event: AgentEvent) -> bool:
        return True

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "turns.jsonl").write_text(
        json.dumps({
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "channel_id": "poller:github-activity",
            "total_cost_usd": None,
        }) + "\n",
        encoding="utf-8",
    )
    sched = Scheduler(
        scheduler_yaml=tmp_path / "scheduler.yaml",
        enqueue=noop,
        home=tmp_path,
    )
    skills = tmp_path / "skills"
    _drop_pollers_skill(skills, "github-activity", skill_name="github-poller")
    sched.add_poller_jobs(skills)

    payload = build_scheduler_dashboard_payload(
        scheduler=sched,
        commitments_store=None,
        events=[],
    )

    window = payload["pollers"][0]["usage"]["windows"]["1h"]
    assert window["agent_turns"] == 1
    assert window["total_cost_usd"] is None
