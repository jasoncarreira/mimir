from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mimir.models import AgentEvent
from mimir.scheduler import Scheduler
from mimir.scheduler_dashboard import build_scheduler_dashboard_payload


def _drop_pollers_skill(skills_dir: Path, name: str, cron: str = "*/5 * * * *") -> Path:
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "pollers.json").write_text(json.dumps({
        "pollers": [{"name": name, "command": "true", "cron": cron}],
    }), encoding="utf-8")
    return skill


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
    _drop_pollers_skill(skills, "github-activity")
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
    _drop_pollers_skill(skills, "github-activity")
    sched.add_poller_jobs(skills)

    payload = build_scheduler_dashboard_payload(
        scheduler=sched,
        commitments_store=None,
        events=[],
    )

    window = payload["pollers"][0]["usage"]["windows"]["1h"]
    assert window["agent_turns"] == 1
    assert window["total_cost_usd"] is None
