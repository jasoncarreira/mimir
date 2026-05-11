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


# ─── reload_pollers MCP reply — PR #141 review items #1+2 ──────────


def _write_manifest(skills_dir: Path, name: str, body: str) -> Path:
    """Write ``skills_dir/<name>/pollers.json`` with ``body``; create
    the skill subdir if needed. Returns the manifest path."""
    sd = skills_dir / name
    sd.mkdir(parents=True, exist_ok=True)
    p = sd / "pollers.json"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_reload_pollers_reply_clean_path(tmp_path: Path):
    """A clean reload renders only the ok-line — no parse-failure
    warning suffix (PR #141 review item #1, negative case)."""
    sched = _make_scheduler(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_manifest(
        skills, "alpha",
        '{"pollers": [{"name": "alpha", "command": "echo hi",'
        ' "cron": "* * * * *"}]}',
    )
    sched.add_poller_jobs(skills)
    tools = {t.name: t for t in build_schedule_tools(sched)}

    out = await tools["reload_pollers"].handler({})
    body = out["content"][0]["text"]
    assert "reload_pollers ok: 1 poller(s) registered" in body
    assert "alpha" in body
    assert "warning" not in body, (
        "clean reload should not render the parse-failure suffix"
    )


@pytest.mark.asyncio
async def test_reload_pollers_reply_surfaces_invalid_manifest(tmp_path: Path):
    """When a manifest fails to JSON-parse on reload, the MCP reply
    flags the parse failure inline AND the count reflects the live
    total (preserved + freshly-installed) — not just Phase 3 installs
    (PR #141 review items #1+2 together)."""
    sched = _make_scheduler(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    alpha = _write_manifest(
        skills, "alpha",
        '{"pollers": [{"name": "alpha", "command": "echo hi",'
        ' "cron": "* * * * *"}]}',
    )
    _write_manifest(
        skills, "beta",
        '{"pollers": [{"name": "beta", "command": "echo hi",'
        ' "cron": "* * * * *"}]}',
    )
    # Bootstrap: both pollers install cleanly.
    sched.add_poller_jobs(skills)
    assert sorted(sched.registered_pollers()) == ["alpha", "beta"]

    # Operator mid-edit: alpha's manifest gets a syntax error.
    alpha.write_text("{not valid json", encoding="utf-8")
    tools = {t.name: t for t in build_schedule_tools(sched)}

    out = await tools["reload_pollers"].handler({})
    body = out["content"][0]["text"]
    # Item #2: count matches names list (2, not 1).
    assert "2 poller(s) registered" in body, (
        f"expected live-total count of 2, got: {body!r}"
    )
    assert "alpha" in body and "beta" in body
    # Item #1: warning suffix flags the parse failure inline + preserved.
    assert "warning" in body.lower()
    assert "1 manifest failed to parse" in body
    assert "preserved 1 prior poller" in body
    assert "alpha" in body  # preserved poller named in the suffix
    assert "events.jsonl" in body


@pytest.mark.asyncio
async def test_reload_pollers_zero_with_invalid_manifest(tmp_path: Path):
    """When a manifest is invalid AND no pollers end up registered,
    the reply still surfaces the parse failure — the 0-pollers
    early-return path must not swallow the warning."""
    sched = _make_scheduler(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    # Bootstrap with a valid manifest so add_poller_jobs wires
    # _pollers_dir; otherwise reload_pollers no-ops at the
    # self._pollers_dir is None check.
    bad = _write_manifest(skills, "doomed", '{"pollers": []}')
    sched.add_poller_jobs(skills)
    bad.write_text("not json", encoding="utf-8")
    tools = {t.name: t for t in build_schedule_tools(sched)}

    out = await tools["reload_pollers"].handler({})
    body = out["content"][0]["text"]
    # 0-with-warning path: does NOT take the no-skills-dir early-return.
    assert "no <home>/.claude/skills" not in body
    assert "warning" in body.lower()
    assert "1 manifest failed to parse" in body
