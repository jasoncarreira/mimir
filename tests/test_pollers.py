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
    through to the agent's prompt rendering. Per-item metadata lives
    under ``extra.items[i]`` (a list of dicts, one per item in the
    batch) so the structure is uniform across batch_size=1 and
    batch_size>1 — the agent doesn't need to special-case batched
    vs unbatched events."""
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
    # Top-level batch metadata.
    assert extra["poller_name"] == "x"
    assert extra["batch_index"] == 0
    assert extra["batch_count"] == 1
    # Per-item metadata under .items[0].
    assert len(extra["items"]) == 1
    item = extra["items"][0]
    assert item["source_platform"] == "github"
    assert item["url"] == "https://example.com/pr/1"
    # ``prompt`` and ``poller`` themselves are stripped — they're the
    # framework-required keys, not metadata.
    assert "prompt" not in item
    assert "poller" not in item


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


# ─── batch_size: framework-level event coalescing ─────────────────────


def test_discover_pollers_default_batch_size_is_one(tmp_path: Path):
    """``batch_size`` defaults to 1 — preserves the open-strix-
    equivalent shape (one AgentEvent per emitted JSONL line) for
    pollers that don't opt in to coalescing."""
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "skill", [
        {"name": "p", "command": "x", "cron": "* * * * *"},
    ])
    [p] = discover_pollers(skills)
    assert p.batch_size == 1


def test_discover_pollers_reads_batch_size_from_json(tmp_path: Path):
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "skill", [
        {"name": "p", "command": "x", "cron": "* * * * *", "batch_size": 5},
    ])
    [p] = discover_pollers(skills)
    assert p.batch_size == 5


def test_discover_pollers_garbage_batch_size_falls_back(
    tmp_path: Path, caplog,
):
    """Negative / zero / non-integer batch_size logs a warning + uses
    the default. Protects against typos silently breaking coalescing."""
    import logging
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "skill", [
        {"name": "p", "command": "x", "cron": "* * * * *", "batch_size": -1},
    ])
    with caplog.at_level(logging.WARNING, logger="mimir.pollers"):
        [p] = discover_pollers(skills)
    assert p.batch_size == 1
    assert any(
        "poller_invalid_batch_size" in r.getMessage() for r in caplog.records
    )


def test_discover_pollers_batch_size_string_falls_back(tmp_path: Path):
    """JSON value that's a string (e.g. ``"5"``) is coerced via int()
    and accepted (defensive: tolerate operator-supplied stringly-typed
    config)."""
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "skill", [
        {"name": "p", "command": "x", "cron": "* * * * *", "batch_size": "5"},
    ])
    [p] = discover_pollers(skills)
    assert p.batch_size == 5


@pytest.mark.asyncio
async def test_run_poller_batch_size_one_emits_per_item(
    tmp_path: Path, home: Path,
) -> None:
    """batch_size=1 (default) preserves per-item-per-event emission —
    3 stdout lines → 3 AgentEvents, one per item. Verifies that
    batching doesn't change behavior for pollers that don't opt in."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
for i in range(3):
    print(json.dumps({"poller": "x", "prompt": f"item {i}"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=1,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 3
    assert [e.content for e in enq.events] == ["item 0", "item 1", "item 2"]
    # Single-item batches retain the verbatim prompt — no header.
    assert all("reported" not in e.content for e in enq.events)
    # Each event's extra carries batch_index 0 + batch_count 3.
    indices = [e.extra["batch_index"] for e in enq.events]
    counts = {e.extra["batch_count"] for e in enq.events}
    assert indices == [0, 1, 2]
    assert counts == {3}


@pytest.mark.asyncio
async def test_run_poller_batch_size_above_one_coalesces(
    tmp_path: Path, home: Path,
) -> None:
    """batch_size=5 with 3 items → 1 AgentEvent containing the
    rendered batch (header + 3 numbered items)."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "prompt": "first event"}))
print(json.dumps({"poller": "x", "prompt": "second event"}))
print(json.dumps({"poller": "x", "prompt": "third event"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=5,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    # 3 items, batch_size=5 → 1 batch → 1 AgentEvent.
    assert n == 1
    assert len(enq.events) == 1
    content = enq.events[0].content
    # Header indicates the batch.
    assert "x reported 3 items" in content
    # Each item rendered as a numbered bullet.
    assert "1. first event" in content
    assert "2. second event" in content
    assert "3. third event" in content
    # Single-batch fires don't include the "batch X of Y" suffix —
    # only multi-batch fires do.
    assert "batch 1 of" not in content
    # Extra carries batch metadata + per-item items.
    extra = enq.events[0].extra
    assert extra["batch_index"] == 0
    assert extra["batch_count"] == 1
    assert len(extra["items"]) == 3


@pytest.mark.asyncio
async def test_run_poller_batch_overflow_emits_multiple_events(
    tmp_path: Path, home: Path,
) -> None:
    """12 items with batch_size=5 → 3 AgentEvents (5 + 5 + 2). Each
    carries ``batch_index`` and ``batch_count=3`` so the agent can
    tell it's seeing part of a multi-batch fire."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
for i in range(12):
    print(json.dumps({"poller": "x", "prompt": f"item {i}"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=5,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 3
    assert len(enq.events) == 3
    # First batch: items 0-4. Second: 5-9. Third: 10-11.
    sizes = [len(e.extra["items"]) for e in enq.events]
    assert sizes == [5, 5, 2]
    # Each batch carries its own index + the total count.
    indices = [e.extra["batch_index"] for e in enq.events]
    counts = {e.extra["batch_count"] for e in enq.events}
    assert indices == [0, 1, 2]
    assert counts == {3}
    # Multi-batch headers include the "batch X of Y" suffix.
    assert "batch 1 of 3" in enq.events[0].content
    assert "batch 2 of 3" in enq.events[1].content
    assert "batch 3 of 3" in enq.events[2].content


@pytest.mark.asyncio
async def test_run_poller_batch_render_indents_multiline_items(
    tmp_path: Path, home: Path,
) -> None:
    """Multi-line item prompts (URLs / detail blocks on their own
    lines) get visually grouped under their item-number marker via
    indented continuation. Without the indent, items 2+ look like
    they're starting where item 1's URL was — readability hit on
    bursty github-poller fires."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x",
    "prompt": "first event\\nhttps://example.com/1"}))
print(json.dumps({"poller": "x",
    "prompt": "second event"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=5,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    content = enq.events[0].content
    # Continuation lines indented under the numbered marker.
    assert "1. first event\n   https://example.com/1\n2. second event" in content


@pytest.mark.asyncio
async def test_run_poller_batched_complete_event_has_metadata(
    tmp_path: Path, home: Path,
) -> None:
    """``poller_complete`` carries ``items_collected`` + ``batches_emitted``
    so the operator can audit the coalesce ratio (a 12-item fire that
    coalesces to 3 batches reports both)."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
for i in range(7):
    print(json.dumps({"poller": "x", "prompt": f"item {i}"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=3,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    completes = [e for e in events if e["type"] == "poller_complete"]
    assert len(completes) == 1
    assert completes[0]["items_collected"] == 7
    # 7 items / batch_size 3 = ceil(7/3) = 3 batches.
    assert completes[0]["batches_emitted"] == 3
    assert completes[0]["events_emitted"] == 3


@pytest.mark.asyncio
async def test_run_poller_silent_run_reports_zero_metadata(
    tmp_path: Path, home: Path,
) -> None:
    """A poller that exits 0 with no stdout reports zero items + zero
    batches alongside the existing zero events_emitted/rejected."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", "pass\n")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0
    events = _read_events(home)
    completes = [e for e in events if e["type"] == "poller_complete"]
    assert len(completes) == 1
    assert completes[0]["items_collected"] == 0
    assert completes[0]["batches_emitted"] == 0
    assert completes[0]["events_emitted"] == 0
    assert completes[0]["events_rejected"] == 0


# ─── per-item starvation cap (PR #93 review nit) ─────────────────────


@pytest.mark.asyncio
async def test_run_poller_batch_size_above_one_caps_per_item(
    tmp_path: Path, home: Path,
) -> None:
    """When batch_size > 1, one giant item shouldn't starve the rest
    of the batch. Per-item soft cap = POLLER_PROMPT_CHARS//batch_size
    (minus a 50-char marker overhead, floored at 100). Pre-fix: a
    single 16K+ item in a 5-item batch consumed the whole prompt
    budget, items 2-5 got truncated away with no per-item indicator
    that anything was dropped."""
    from mimir.pollers import POLLER_PROMPT_CHARS
    skill_dir = tmp_path / "skill"
    # 5 items: one giant (>per-item cap), four small. With batch_size=5
    # the per-item cap is POLLER_PROMPT_CHARS//5 - 50 ≈ 3150 chars.
    # The giant item should be capped; small items pass through.
    expected_per_item_cap = POLLER_PROMPT_CHARS // 5 - 50
    huge = "GIANT" * (expected_per_item_cap + 200)  # well above cap
    _install_script(skill_dir, "poller.py", f"""
import json
print(json.dumps({{"poller": "x", "prompt": {huge!r}}}))
print(json.dumps({{"poller": "x", "prompt": "small 2"}}))
print(json.dumps({{"poller": "x", "prompt": "small 3"}}))
print(json.dumps({{"poller": "x", "prompt": "small 4"}}))
print(json.dumps({{"poller": "x", "prompt": "small 5"}}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=5,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 1  # all 5 items in one batch
    content = enq.events[0].content
    # Small items survive — proves per-item cap fired on the giant
    # rather than the batch-level cap dropping the trailing items.
    assert "1. GIANT" in content  # giant rendered (truncated)
    assert "small 2" in content
    assert "small 3" in content
    assert "small 4" in content
    assert "small 5" in content
    # Per-item truncation event fired with scope="per_item".
    events = _read_events(home)
    truncs = [e for e in events if e["type"] == "poller_prompt_truncated"]
    per_item = [t for t in truncs if t.get("scope") == "per_item"]
    assert len(per_item) == 1
    # Final batch render is under the global cap (per-item caps did
    # their job; batch-level cap doesn't fire).
    assert len(content) <= POLLER_PROMPT_CHARS + 100
    batch_truncs = [t for t in truncs if t.get("scope") == "batch"]
    assert batch_truncs == []


@pytest.mark.asyncio
async def test_run_poller_batch_size_one_does_not_apply_per_item_cap(
    tmp_path: Path, home: Path,
) -> None:
    """The per-item cap fires only at batch_size>1. At batch_size=1
    (default) a giant single item still goes through the batch-level
    cap, preserving the verbatim-pass-through shape that matches
    open-strix for the default path."""
    from mimir.pollers import POLLER_PROMPT_CHARS
    skill_dir = tmp_path / "skill"
    huge = "X" * (POLLER_PROMPT_CHARS + 5000)
    _install_script(skill_dir, "poller.py", f"""
import json
print(json.dumps({{"poller": "x", "prompt": {huge!r}}}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=1,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    truncs = [e for e in events if e["type"] == "poller_prompt_truncated"]
    # Only the batch-level cap fires; no per_item scope.
    scopes = [t.get("scope") for t in truncs]
    assert "batch" in scopes
    assert "per_item" not in scopes


@pytest.mark.asyncio
async def test_run_poller_source_id_carries_fire_timestamp(
    tmp_path: Path, home: Path,
) -> None:
    """source_id includes a per-fire timestamp so overlapping fires
    of the same poller don't produce colliding source_ids
    (manual-fire racing scheduled-fire, e.g.). Format:
    ``poller:<name>:<fire_ts_ms>:batch:<idx>``."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "prompt": "first"}))
print(json.dumps({"poller": "x", "prompt": "second"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        batch_size=1,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    sids = [e.source_id for e in enq.events]
    # Format: poller:<name>:<fire_ts_ms>:batch:<idx>
    parts0 = sids[0].split(":")
    assert parts0[0] == "poller"
    assert parts0[1] == "x"
    # fire_ts_ms is a digit string of reasonable length (13-14 chars
    # for current epoch ms).
    assert parts0[2].isdigit()
    assert len(parts0[2]) >= 13
    assert parts0[3] == "batch"
    assert parts0[4] == "0"
    # Both events from the same fire share the same fire_ts_ms.
    assert sids[0].split(":")[2] == sids[1].split(":")[2]
    # But have distinct batch_idx.
    assert sids[0].endswith(":0") and sids[1].endswith(":1")


def test_render_batch_pads_markers_for_double_digit_batches():
    """When a batch has 10+ items, marker padding aligns so
    ``"10. "`` and ``" 1. "`` line up. Continuation indent matches
    the marker width so multi-line items stay grouped under their
    parent regardless of digit count."""
    from mimir.pollers import _render_batch
    batch = [
        {"prompt": f"item {i}\nurl-{i}", "extras": {}}
        for i in range(12)
    ]
    out = _render_batch("p", batch, 0, 1)
    # 12 items → width 2. Single-digit markers padded with leading space.
    assert "\n 1. item 0\n" in out
    assert "\n 2. item 1\n" in out
    assert "\n10. item 9\n" in out
    assert "\n11. item 10\n" in out
    # Continuation indent: 4 chars (2 for digit + ". ").
    assert "    url-0" in out
    assert "    url-9" in out
