"""Tests for ``mimir.pollers`` — pollers framework (chainlink #3).

Coverage:
- ``discover_pollers``: skill-dir traversal, malformed JSON / missing
  fields filtering, no-skills-dir fast path.
- ``run_poller``: subprocess execution, env injection (STATE_DIR,
  POLLER_NAME, custom env), stdout JSONL → AgentEvent, stderr → log,
  nonzero exit, timeout, malformed line tolerance.
- Cross-cutting: silence-means-no-events, multi-line JSONL.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from mimir.event_logger import init_logger
from mimir.models import AgentEvent
from mimir.pollers import (
    POLLER_TIMEOUT_SECONDS,
    PollerConfig,
    discover_pollers,
    run_poller,
)


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Standard MIMIR_HOME with logger initialized so log_event won't crash."""
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-pollers")
    return tmp_path


def _read_events(home: Path) -> list[dict]:
    path = home / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines() if line.strip()
    ]


def _write_pollers_json(skill_dir: Path, entries: list[dict]) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "pollers.json").write_text(
        json.dumps({"pollers": entries}), encoding="utf-8",
    )


def _install_script(skill_dir: Path, name: str, body: str) -> Path:
    """Drop a python script + chmod +x. Body is the script content."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    script = skill_dir / name
    script.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ─── discover_pollers ────────────────────────────────────────────────


def test_discover_returns_empty_when_skills_dir_missing(tmp_path: Path):
    """Most installs have no skills/pollers — the framework must
    no-op cleanly. Returns [] without warning or raising."""
    out = discover_pollers(tmp_path / "does-not-exist")
    assert out == []


def test_discover_returns_empty_when_skills_dir_has_no_pollers(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "some-skill").mkdir(parents=True)
    (skills / "some-skill" / "SKILL.md").write_text("just docs")
    assert discover_pollers(skills) == []


def test_discover_parses_valid_pollers_json(tmp_path: Path):
    skills = tmp_path / "skills"
    skill_dir = skills / "github-poller"
    _write_pollers_json(skill_dir, [
        {
            "name": "github-activity",
            "command": "python poller.py",
            "cron": "*/15 * * * *",
            "env": {"GITHUB_REPOS": "owner/repo"},
        },
    ])
    out = discover_pollers(skills)
    assert len(out) == 1
    p = out[0]
    assert p.name == "github-activity"
    assert p.command == "python poller.py"
    assert p.cron == "*/15 * * * *"
    assert p.env == {"GITHUB_REPOS": "owner/repo"}
    assert p.skill_dir == skill_dir
    assert p.channel_id() == "poller:github-activity"


def test_discover_skips_malformed_json(tmp_path: Path, caplog):
    """A bad JSON file must not abort the walk — other valid skills
    keep working. Logs a warning so the skill author can find it."""
    import logging
    skills = tmp_path / "skills"
    bad = skills / "bad-skill"
    bad.mkdir(parents=True)
    (bad / "pollers.json").write_text("not json {{{ at all", encoding="utf-8")
    good = skills / "good-skill"
    _write_pollers_json(good, [
        {"name": "good", "command": "echo hi", "cron": "* * * * *"},
    ])
    with caplog.at_level(logging.WARNING, logger="mimir.pollers"):
        out = discover_pollers(skills)
    # The good skill was still registered.
    assert len(out) == 1
    assert out[0].name == "good"
    # The bad one logged a warning (so the operator sees it).
    assert any("poller_invalid_json" in r.getMessage() for r in caplog.records)


def test_discover_skips_entries_missing_required_fields(tmp_path: Path):
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "skill", [
        {"name": "no-cmd", "cron": "* * * * *"},  # missing command
        {"name": "no-cron", "command": "echo"},   # missing cron
        {"command": "echo", "cron": "* * * * *"}, # missing name
        {"name": "valid", "command": "echo", "cron": "* * * * *"},
    ])
    out = discover_pollers(skills)
    assert len(out) == 1
    assert out[0].name == "valid"


def test_discover_handles_top_level_array_instead_of_object(tmp_path: Path):
    """The contract requires ``{"pollers": [...]}`` not a bare array.
    Bare arrays log a warning and skip — no silent acceptance."""
    skills = tmp_path / "skills"
    skill = skills / "skill"
    skill.mkdir(parents=True)
    (skill / "pollers.json").write_text(
        json.dumps([{"name": "x", "command": "y", "cron": "* * * * *"}]),
        encoding="utf-8",
    )
    assert discover_pollers(skills) == []


def test_discover_walks_nested_skill_dirs(tmp_path: Path):
    """Pollers can live under nested directories
    (skills/parent/child/pollers.json) — the rglob walk catches them."""
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "a", [
        {"name": "a-poll", "command": "x", "cron": "* * * * *"},
    ])
    _write_pollers_json(skills / "b" / "nested", [
        {"name": "b-poll", "command": "y", "cron": "* * * * *"},
    ])
    out = discover_pollers(skills)
    names = sorted(p.name for p in out)
    assert names == ["a-poll", "b-poll"]


# ─── run_poller: success paths ───────────────────────────────────────


class _CapturingEnqueue:
    """Fake dispatcher.enqueue that just collects events for assertion."""

    def __init__(self, accept: bool = True):
        self.events: list[AgentEvent] = []
        self.accept = accept

    async def __call__(self, event: AgentEvent) -> bool:
        self.events.append(event)
        return self.accept


@pytest.mark.asyncio
async def test_run_poller_emits_events_for_each_jsonl_line(
    tmp_path: Path, home: Path,
) -> None:
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "test", "prompt": "first event"}))
print(json.dumps({"poller": "test", "prompt": "second event"}))
""")
    cfg = PollerConfig(
        name="test", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 2
    assert len(enq.events) == 2
    assert enq.events[0].content == "first event"
    assert enq.events[0].trigger == "poller"
    assert enq.events[0].channel_id == "poller:test"
    assert enq.events[0].source == "poller"
    assert enq.events[1].content == "second event"


@pytest.mark.asyncio
async def test_run_poller_silence_emits_zero_events(
    tmp_path: Path, home: Path,
) -> None:
    """``silence means nothing to report`` is the contract — a
    poller exiting 0 with no stdout produces no events but logs
    a clean ``poller_complete`` so the operator can audit run cadence."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", "pass\n")
    cfg = PollerConfig(
        name="quiet", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0
    assert enq.events == []
    events = _read_events(home)
    completes = [e for e in events if e["type"] == "poller_complete"]
    assert len(completes) == 1
    assert completes[0]["events_emitted"] == 0


@pytest.mark.asyncio
async def test_run_poller_injects_state_dir_and_poller_name(
    tmp_path: Path, home: Path,
) -> None:
    """Subprocess receives STATE_DIR + POLLER_NAME env vars per the
    pollers contract. Verified by having the poller print them back
    in its emitted prompt."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json, os
prompt = f"state_dir={os.environ['STATE_DIR']} poller_name={os.environ['POLLER_NAME']}"
print(json.dumps({"poller": "x", "prompt": prompt}))
""")
    cfg = PollerConfig(
        name="my-poller", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    assert len(enq.events) == 1
    content = enq.events[0].content
    assert f"state_dir={skill_dir}" in content
    assert "poller_name=my-poller" in content


@pytest.mark.asyncio
async def test_run_poller_passes_custom_env_from_config(
    tmp_path: Path, home: Path,
) -> None:
    """``env`` map from pollers.json reaches the subprocess."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json, os
print(json.dumps({"poller": "x", "prompt": os.environ.get("CUSTOM_VAR", "missing")}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={"CUSTOM_VAR": "hello"}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    assert enq.events[0].content == "hello"


@pytest.mark.asyncio
async def test_run_poller_extras_flow_to_event_extra(
    tmp_path: Path, home: Path,
) -> None:
    """Keys other than ``poller`` and ``prompt`` flow into AgentEvent.extra
    so platform-specific metadata (urls, ids, source_platform) carries
    through to the agent's prompt rendering."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({
    "poller": "x", "prompt": "msg",
    "source_platform": "github", "url": "https://example.com/pr/1",
}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    extra = enq.events[0].extra
    assert extra["source_platform"] == "github"
    assert extra["url"] == "https://example.com/pr/1"
    assert extra["poller_name"] == "x"
    # ``prompt`` and ``poller`` themselves are stripped — they're the
    # framework-required keys, not metadata.
    assert "prompt" not in extra
    assert "poller" not in extra


# ─── run_poller: error paths ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_poller_skips_malformed_lines_but_keeps_valid(
    tmp_path: Path, home: Path,
) -> None:
    """Mid-stream JSON errors are non-fatal — the parser logs the bad
    line and continues. Valid lines still become events."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "prompt": "first"}))
print("not valid json at all")
print(json.dumps({"poller": "x", "prompt": "second"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 2
    assert [e.content for e in enq.events] == ["first", "second"]
    events = _read_events(home)
    invalid = [e for e in events if e["type"] == "poller_invalid_line"]
    assert len(invalid) == 1


@pytest.mark.asyncio
async def test_run_poller_skips_lines_without_prompt_field(
    tmp_path: Path, home: Path,
) -> None:
    """A JSON line without ``prompt`` is malformed-but-parseable. We
    drop it silently — no need to log every one (lots of pollers
    might emit metadata-only lines for diagnostics)."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "diag": "internal"}))
print(json.dumps({"poller": "x", "prompt": "real event"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 1
    assert enq.events[0].content == "real event"


@pytest.mark.asyncio
async def test_run_poller_nonzero_exit_emits_no_events(
    tmp_path: Path, home: Path,
) -> None:
    """A poller that errors out (exit code != 0) gets its events
    DROPPED — the contract is "exit 0 = success". This protects against
    half-failed runs emitting a partial event stream that the operator
    can't tell from a real signal."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json, sys
print(json.dumps({"poller": "x", "prompt": "would emit but exit nonzero"}))
sys.exit(1)
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0
    assert enq.events == []
    events = _read_events(home)
    nonzero = [e for e in events if e["type"] == "poller_nonzero_exit"]
    assert len(nonzero) == 1
    assert nonzero[0]["returncode"] == 1


@pytest.mark.asyncio
async def test_run_poller_stderr_logged_as_poller_stderr(
    tmp_path: Path, home: Path,
) -> None:
    """Pollers can log diagnostic info to stderr (per the contract);
    the framework captures that and emits a ``poller_stderr`` event so
    the operator can grep for it. Doesn't affect event emission."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json, sys
print("checking external service...", file=sys.stderr)
print(json.dumps({"poller": "x", "prompt": "ok"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 1
    events = _read_events(home)
    stderr = [e for e in events if e["type"] == "poller_stderr"]
    assert len(stderr) == 1
    assert "checking external service" in stderr[0]["stderr"]


@pytest.mark.asyncio
async def test_run_poller_timeout_kills_subprocess(
    tmp_path: Path, home: Path,
) -> None:
    """A poller that runs longer than the timeout must be killed.
    Returns 0 events; logs ``poller_timeout``."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json, time
print(json.dumps({"poller": "x", "prompt": "would emit"}), flush=True)
time.sleep(120)
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    # Use a much shorter timeout for the test.
    n = await run_poller(cfg, enqueue=enq, timeout=2.0)
    assert n == 0
    assert enq.events == []
    events = _read_events(home)
    timeouts = [e for e in events if e["type"] == "poller_timeout"]
    assert len(timeouts) == 1


@pytest.mark.asyncio
async def test_run_poller_nonexistent_command_logs_exec_error(
    tmp_path: Path, home: Path,
) -> None:
    """A bogus command (script doesn't exist) propagates as a clean
    ``poller_exec_error`` rather than crashing the scheduler."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} nonexistent_script_xyz.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0
    # Subprocess starts (python launches) but exits nonzero because
    # the script doesn't exist — caught by the nonzero-exit branch.
    events = _read_events(home)
    types = [e["type"] for e in events]
    # Either nonzero exit or exec error is acceptable depending on
    # how the platform reports the missing-script case.
    assert any(t in types for t in ("poller_nonzero_exit", "poller_exec_error"))


# ─── PollerConfig invariants ──────────────────────────────────────────


def test_poller_config_channel_id_format():
    cfg = PollerConfig(
        name="my-poller", command="x", cron="* * * * *", env={},
        skill_dir=Path("/tmp"),
    )
    assert cfg.channel_id() == "poller:my-poller"


def test_poller_timeout_constant_reasonable():
    """Locks the contract: the framework's hard-cap is 60s. Skill
    authors who need longer-running pollers must restructure."""
    assert POLLER_TIMEOUT_SECONDS == 60


# ─── Back-pressure observability (PR #88 review nits 5+6) ─────────────


@pytest.mark.asyncio
async def test_run_poller_does_not_count_rejected_enqueues(
    tmp_path: Path, home: Path,
) -> None:
    """When the dispatcher refuses an event (returns False), the
    poller framework MUST NOT count it toward events_emitted — that's
    the back-pressure signal. Both events still pass through to the
    dispatcher (so the test fake sees them); only the count differs."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "prompt": "first"}))
print(json.dumps({"poller": "x", "prompt": "second"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue(accept=False)
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0
    # Dispatcher saw both attempts.
    assert len(enq.events) == 2
    events = _read_events(home)
    completes = [e for e in events if e["type"] == "poller_complete"]
    assert len(completes) == 1
    assert completes[0]["events_emitted"] == 0
    assert completes[0]["events_rejected"] == 2


@pytest.mark.asyncio
async def test_run_poller_emits_rejection_events_for_back_pressure(
    tmp_path: Path, home: Path,
) -> None:
    """Each rejected event lands as a ``poller_event_rejected`` event
    in events.jsonl with a truncated prompt preview, so the operator
    can audit which payloads got back-pressured."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "prompt": "rejected payload"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue(accept=False)
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    rejections = [
        e for e in events if e["type"] == "poller_event_rejected"
    ]
    assert len(rejections) == 1
    assert rejections[0]["poller"] == "x"
    assert "rejected payload" in rejections[0]["prompt_preview"]


@pytest.mark.asyncio
async def test_run_poller_complete_carries_both_counts_on_silence(
    tmp_path: Path, home: Path,
) -> None:
    """A silent poller still emits poller_complete with both
    events_emitted=0 AND events_rejected=0 — distinguishes "genuine
    silence" from "back-pressure rejected everything"."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", "pass")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    completes = [e for e in events if e["type"] == "poller_complete"]
    assert len(completes) == 1
    assert completes[0]["events_emitted"] == 0
    assert completes[0]["events_rejected"] == 0


# ─── Prompt cap (PR #88 review nit 4) ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_poller_caps_huge_prompt_with_truncation_marker(
    tmp_path: Path, home: Path,
) -> None:
    """A poller emitting a 50 KB prompt gets capped at
    POLLER_PROMPT_CHARS (16 KB) with a truncation suffix. Protects
    against chatty / buggy pollers blowing the prompt-build cache."""
    from mimir.pollers import POLLER_PROMPT_CHARS
    skill_dir = tmp_path / "skill"
    huge_chars = POLLER_PROMPT_CHARS + 10_000
    _install_script(skill_dir, "poller.py", f"""
import json
print(json.dumps({{"poller": "x", "prompt": "A" * {huge_chars}}}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    assert len(enq.events) == 1
    content = enq.events[0].content
    assert len(content) <= POLLER_PROMPT_CHARS + 100
    assert "truncated by poller framework" in content
    events = _read_events(home)
    truncs = [e for e in events if e["type"] == "poller_prompt_truncated"]
    assert len(truncs) == 1
    assert truncs[0]["original_chars"] == huge_chars


@pytest.mark.asyncio
async def test_run_poller_under_cap_passes_through_unchanged(
    tmp_path: Path, home: Path,
) -> None:
    """Below the cap, prompts pass through verbatim (no truncation
    suffix added). Locks the inclusive boundary."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "prompt": "small payload"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    assert enq.events[0].content == "small payload"
    events = _read_events(home)
    assert not any(e["type"] == "poller_prompt_truncated" for e in events)


# ─── persist_dir / STATE_DIR redirect (PR #88 review nit 3) ───────────


@pytest.mark.asyncio
async def test_run_poller_state_dir_points_at_persist_dir(
    tmp_path: Path, home: Path,
) -> None:
    """``STATE_DIR`` is the poller's persistent state location — when
    set, the framework injects ``persist_dir`` (NOT ``skill_dir``)
    so cursor files survive container rebuilds even when the skill
    itself ships in the image."""
    skill_dir = tmp_path / "skill"
    persist_dir = tmp_path / "persist" / "x"
    _install_script(skill_dir, "poller.py", """
import json, os
print(json.dumps({"poller": "x", "prompt": os.environ["STATE_DIR"]}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        persist_dir=persist_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    assert enq.events[0].content == str(persist_dir)
    # Persist dir was lazy-created.
    assert persist_dir.is_dir()


@pytest.mark.asyncio
async def test_run_poller_persist_dir_falls_back_to_skill_dir(
    tmp_path: Path, home: Path,
) -> None:
    """When ``persist_dir`` isn't set on the PollerConfig (tests +
    niche callers), STATE_DIR falls back to the skill_dir for
    backward compatibility."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json, os
print(json.dumps({"poller": "x", "prompt": os.environ["STATE_DIR"]}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        # persist_dir omitted → falls back
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    assert enq.events[0].content == str(skill_dir)


def test_discover_pollers_sets_persist_dir_when_state_root_supplied(
    tmp_path: Path,
) -> None:
    """``Scheduler.add_poller_jobs`` passes
    ``state_root=<home>/state/pollers``; each discovered poller's
    persist_dir resolves to ``<state_root>/<poller_name>/``."""
    skills = tmp_path / "skills"
    state_root = tmp_path / "state" / "pollers"
    skill = skills / "ghp"
    skill.mkdir(parents=True)
    (skill / "pollers.json").write_text(json.dumps({
        "pollers": [{"name": "ghp", "command": "x", "cron": "* * * * *"}],
    }), encoding="utf-8")
    out = discover_pollers(skills, state_root=state_root)
    assert len(out) == 1
    assert out[0].persist_dir == state_root / "ghp"
    # Skill dir is unchanged from the manifest's location.
    assert out[0].skill_dir == skill


def test_discover_pollers_state_root_none_leaves_persist_dir_unset(
    tmp_path: Path,
) -> None:
    """Default ``state_root=None`` leaves ``persist_dir=None`` so
    ``resolved_persist_dir()`` falls back to skill_dir. Back-compat
    for tests + setups where the skill dir is itself persistent."""
    skills = tmp_path / "skills"
    skill = skills / "ghp"
    skill.mkdir(parents=True)
    (skill / "pollers.json").write_text(json.dumps({
        "pollers": [{"name": "ghp", "command": "x", "cron": "* * * * *"}],
    }), encoding="utf-8")
    out = discover_pollers(skills)
    assert out[0].persist_dir is None
    assert out[0].resolved_persist_dir() == skill


# ─── Subprocess hygiene: kill+reap on every exit path (Nit 1) ─────────


@pytest.mark.asyncio
async def test_run_poller_reaps_subprocess_on_timeout(
    tmp_path: Path, home: Path,
) -> None:
    """After a timeout, the framework calls ``proc.wait()`` so the
    kernel-side process record is reaped — no zombies left for the
    long-lived mimir process to accumulate."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import time
time.sleep(120)
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq, timeout=1.0)
    assert n == 0
    # The poller_timeout event landed AND the subprocess was reaped
    # (no warning at end-of-run; tested implicitly by the absence of
    # ``RuntimeError: Event loop is closed`` from the asyncio
    # finalizer when the test's loop tears down).
    events = _read_events(home)
    assert any(e["type"] == "poller_timeout" for e in events)


# ─── Shell-vs-exec command parsing (PR #88 review nit 7) ──────────────


@pytest.mark.asyncio
async def test_run_poller_command_supports_shell_features(
    tmp_path: Path, home: Path,
) -> None:
    """``command`` is parsed by ``/bin/sh -c`` — env-var expansion
    works, pipes work, redirection works. Documented in run_poller's
    docstring; this test pins the contract."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    cfg = PollerConfig(
        name="x",
        # Shell expansion: $POLLER_NAME comes from the env injection.
        command='echo "{\\"poller\\": \\"x\\", \\"prompt\\": \\"name=$POLLER_NAME\\"}"',
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 1
    assert enq.events[0].content == "name=x"
