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


# ─── chainlink #84: invalid-manifest reporting + back-reference ──────


def test_discover_pollers_sets_manifest_path_on_each_config(tmp_path: Path):
    """chainlink #84: each returned ``PollerConfig`` carries the
    absolute path of the ``pollers.json`` it was parsed from, so the
    scheduler can later identify which previously-installed pollers
    belong to a manifest that fails to parse on reload."""
    skills = tmp_path / "skills"
    _write_pollers_json(skills / "a", [
        {"name": "p1", "command": "x", "cron": "* * * * *"},
    ])
    _write_pollers_json(skills / "b", [
        {"name": "p2", "command": "y", "cron": "* * * * *"},
    ])
    out = discover_pollers(skills)
    by_name = {p.name: p for p in out}
    assert by_name["p1"].manifest_path == skills / "a" / "pollers.json"
    assert by_name["p2"].manifest_path == skills / "b" / "pollers.json"


def test_discover_pollers_reports_invalid_manifests_via_outlist(
    tmp_path: Path,
):
    """chainlink #84: when ``invalid_manifests`` is supplied, each
    ``pollers.json`` whose JSON parse failed gets appended as a
    ``(path, error)`` tuple. The valid manifest in the same skills
    tree still returns its config in the regular list. Format-level
    failures (e.g. missing 'pollers' key) are NOT reported — those
    are structural bugs, not transient typos."""
    skills = tmp_path / "skills"
    bad = skills / "broken-skill"
    bad.mkdir(parents=True)
    (bad / "pollers.json").write_text("not json {{{", encoding="utf-8")
    _write_pollers_json(skills / "good-skill", [
        {"name": "good", "command": "echo hi", "cron": "* * * * *"},
    ])
    # Format-level miss: missing 'pollers' key. Should NOT be reported
    # as an invalid manifest (it's a structural bug, not a typo).
    format_bad = skills / "format-bad"
    format_bad.mkdir(parents=True)
    (format_bad / "pollers.json").write_text("{}", encoding="utf-8")

    invalid: list[tuple[Path, str]] = []
    out = discover_pollers(skills, invalid_manifests=invalid)
    # Valid one returned.
    assert [p.name for p in out] == ["good"]
    # Parse-failure manifest reported.
    assert len(invalid) == 1
    failing_path, err = invalid[0]
    assert failing_path == bad / "pollers.json"
    # Error message carries enough info to debug (exception type name
    # + parser-emitted message).
    assert "JSONDecodeError" in err


def test_discover_pollers_invalid_manifests_default_none_no_crash(
    tmp_path: Path,
):
    """Back-compat: callers that don't pass ``invalid_manifests`` see
    the same behavior as before (warning logged, file skipped, no
    crash). Existing call sites in tests / niche code paths must
    continue to work unchanged."""
    skills = tmp_path / "skills"
    bad = skills / "broken-skill"
    bad.mkdir(parents=True)
    (bad / "pollers.json").write_text("not json", encoding="utf-8")
    # No ``invalid_manifests`` kwarg — must not raise.
    out = discover_pollers(skills)
    assert out == []


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
    the operator can grep for it. Doesn't affect event emission.

    The event includes ``exit_code`` so readers can distinguish progress
    noise (exit_code=0) from actual failures (exit_code != 0) — chainlink #93.
    """
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
    # exit_code=0: progress noise on a successful run (e.g. gh auth output)
    assert stderr[0].get("exit_code") == 0


@pytest.mark.asyncio
async def test_run_poller_stderr_exit_code_nonzero_on_failure(
    tmp_path: Path, home: Path,
) -> None:
    """When a poller writes to stderr AND exits non-zero, the
    ``poller_stderr`` event carries ``exit_code != 0`` so readers
    can treat it as an actual error, not progress noise — chainlink #93.
    """
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import sys
print("fatal: could not connect to service", file=sys.stderr)
sys.exit(1)
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0  # no events emitted (non-zero exit)
    events = _read_events(home)
    stderr = [e for e in events if e["type"] == "poller_stderr"]
    assert len(stderr) == 1
    assert "fatal: could not connect" in stderr[0]["stderr"]
    # exit_code != 0: real failure, not progress noise
    assert stderr[0].get("exit_code") == 1


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


def test_pollers_module_annotations_resolve():
    """Regression: ``pollers.py`` uses ``Any`` in type annotations
    (``_render_batch`` signature plus several local annotations) but
    historically did not import it. Under ``from __future__ import
    annotations`` the annotations are stringified, so the missing
    import is latent — it only fires when something resolves the
    hints (``typing.get_type_hints``, dataclass introspection,
    ``inspect.signature`` callers that pass ``eval_str=True``, etc.).
    Locks the import in place so future edits don't silently break
    introspection-time consumers.
    """
    from typing import get_type_hints

    from mimir.pollers import _render_batch

    hints = get_type_hints(_render_batch)
    # ``batch`` is annotated ``list[dict[str, Any]]`` — resolution
    # would NameError without the import.
    assert "batch" in hints


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


# ─── PR #111 review-fix: env scrub regression tests ───────────────────


@pytest.mark.asyncio
async def test_run_poller_env_strips_secrets(
    tmp_path: Path, home: Path, monkeypatch,
):
    """PR #111 review fix: secrets MUST NOT survive into the poller
    subprocess env. ANTHROPIC_API_KEY, MIMIR_API_KEY, GITHUB_TOKEN,
    *_SECRET, *_PASSWORD all hard-deny regardless of operator
    allowlist additions."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    monkeypatch.setenv("MIMIR_API_KEY", "mimir-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("DATABASE_PASSWORD", "pwd-secret")
    monkeypatch.setenv("API_SECRET", "another-secret")
    # Even an explicit allowlist add for a secret is denied.
    monkeypatch.setenv("MIMIR_POLLER_ENV_ALLOWLIST", "ANTHROPIC_API_KEY")

    skill_dir = tmp_path / "skill"
    # Poller dumps env to stderr; we inspect for secrets.
    _install_script(skill_dir, "poller.py", """
import os
import sys
print("|".join(f"{k}={v}" for k, v in sorted(os.environ.items())), file=sys.stderr)
print('{"poller": "x", "prompt": "ok"}')
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    stderr_payloads = [
        e.get("stderr", "")
        for e in events if e.get("type") == "poller_stderr"
    ]
    combined_stderr = "|".join(stderr_payloads)
    # Secrets MUST NOT appear anywhere in the subprocess env dump.
    assert "sk-ant-test-secret" not in combined_stderr
    assert "mimir-secret" not in combined_stderr
    assert "ghp_secret" not in combined_stderr
    assert "pwd-secret" not in combined_stderr
    assert "another-secret" not in combined_stderr


@pytest.mark.asyncio
async def test_run_poller_env_allowlist_override_works(
    tmp_path: Path, home: Path, monkeypatch,
):
    """PR #111 review fix: ``MIMIR_POLLER_ENV_ALLOWLIST`` lets the
    operator extend the built-in list with arbitrary non-secret keys."""
    monkeypatch.setenv("MY_CUSTOM_ENV", "operator-set-value")
    monkeypatch.setenv("MIMIR_POLLER_ENV_ALLOWLIST", "MY_CUSTOM_ENV,OTHER")

    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import os
import sys
print(f"GOT={os.environ.get('MY_CUSTOM_ENV', '')}", file=sys.stderr)
print('{"poller": "x", "prompt": "ok"}')
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    stderr_payloads = [
        e.get("stderr", "")
        for e in events if e.get("type") == "poller_stderr"
    ]
    combined_stderr = "|".join(stderr_payloads)
    assert "GOT=operator-set-value" in combined_stderr


@pytest.mark.asyncio
async def test_run_poller_env_xdg_paths_pass_through(
    tmp_path: Path, home: Path, monkeypatch,
):
    """PR #111 review fix: XDG_CONFIG_HOME etc. must reach pollers
    so ``gh`` and other XDG-respecting CLIs work."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/some/xdg/config")
    monkeypatch.setenv("SSL_CERT_FILE", "/some/ca/cert.pem")

    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import os
import sys
print(f"XDG={os.environ.get('XDG_CONFIG_HOME', '')}|SSL={os.environ.get('SSL_CERT_FILE', '')}", file=sys.stderr)
print('{"poller": "x", "prompt": "ok"}')
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    stderr_payloads = [
        e.get("stderr", "")
        for e in events if e.get("type") == "poller_stderr"
    ]
    combined_stderr = "|".join(stderr_payloads)
    assert "XDG=/some/xdg/config" in combined_stderr
    assert "SSL=/some/ca/cert.pem" in combined_stderr


# ─── chainlink #82 sub #83/#85: pass_env passthrough mechanism ─────────


def test_discover_pollers_parses_pass_env_list(tmp_path: Path) -> None:
    """``pass_env`` field in pollers.json is parsed into PollerConfig.pass_env."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        '{"pollers": [{"name": "x", "command": "true",'
        ' "cron": "* * * * *",'
        ' "pass_env": ["GITHUB_TOKEN", "MIMIR_GITHUB_SELF_LOGIN"]}]}'
    )
    configs = discover_pollers(tmp_path)
    assert len(configs) == 1
    assert configs[0].pass_env == ("GITHUB_TOKEN", "MIMIR_GITHUB_SELF_LOGIN")


def test_discover_pollers_pass_env_missing_defaults_empty(tmp_path: Path) -> None:
    """Absent ``pass_env`` field → empty tuple (back-compat)."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        '{"pollers": [{"name": "x", "command": "true",'
        ' "cron": "* * * * *"}]}'
    )
    configs = discover_pollers(tmp_path)
    assert configs[0].pass_env == ()


def test_discover_pollers_pass_env_non_list_is_ignored(tmp_path: Path) -> None:
    """Malformed ``pass_env`` (e.g. dict, scalar) is rejected with a
    log warning and ignored — the poller still registers."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        '{"pollers": [{"name": "x", "command": "true",'
        ' "cron": "* * * * *", "pass_env": "GITHUB_TOKEN"}]}'
    )
    configs = discover_pollers(tmp_path)
    assert len(configs) == 1
    assert configs[0].pass_env == ()


def test_discover_pollers_pass_env_non_string_items_are_dropped(tmp_path: Path) -> None:
    """PR #135 review nit: the OUTER-type check (whole field isn't a
    list) is covered above. This pins the PER-ITEM filter at
    ``pollers.py:_invalid_pass_env_item`` — items inside a list that
    aren't strings get dropped individually with a log warning, and
    empty-string-after-strip entries are silently dropped. Surviving
    entries preserve declaration order."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "pollers.json").write_text(
        '{"pollers": [{"name": "x", "command": "true",'
        ' "cron": "* * * * *",'
        ' "pass_env": ["GITHUB_TOKEN", 42, null, "", "  ", "OK", true]}]}'
    )
    configs = discover_pollers(tmp_path)
    assert len(configs) == 1
    # Only the two real strings survive; 42/null/true (non-string),
    # ""/"  " (empty-after-strip) all dropped. Order preserved.
    assert configs[0].pass_env == ("GITHUB_TOKEN", "OK")


@pytest.mark.asyncio
async def test_run_poller_pass_env_bypasses_deny_filter(
    tmp_path: Path, home: Path, monkeypatch,
):
    """``pass_env`` declares per-poller env keys that bypass the
    deny-suffix/deny-prefix filter — this is the supported path for
    getting secrets and ``MIMIR_*``-prefixed knobs to a poller. Without
    this, github-poller's ``GITHUB_TOKEN`` and ``MIMIR_GITHUB_SELF_LOGIN``
    are stripped before subprocess invocation (chainlink #82 sub #83/#85)."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_pass_env_value")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import os, sys
print(f"TOKEN={os.environ.get('GITHUB_TOKEN', 'absent')}", file=sys.stderr)
print(f"LOGIN={os.environ.get('MIMIR_GITHUB_SELF_LOGIN', 'absent')}", file=sys.stderr)
print('{"poller": "x", "prompt": "ok"}')
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        pass_env=("GITHUB_TOKEN", "MIMIR_GITHUB_SELF_LOGIN"),
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    stderr_payloads = [
        e.get("stderr", "")
        for e in events if e.get("type") == "poller_stderr"
    ]
    combined_stderr = "|".join(stderr_payloads)
    assert "TOKEN=ghp_test_pass_env_value" in combined_stderr
    assert "LOGIN=mimir-bot" in combined_stderr


@pytest.mark.asyncio
async def test_run_poller_pass_env_unset_in_environ_is_skipped(
    tmp_path: Path, home: Path, monkeypatch,
):
    """``pass_env`` entries that aren't set in os.environ are silently
    skipped — that absence is itself the operator's signal that the
    env var wasn't provisioned. The poller still runs (it'll likely
    fall through to its own default-handling path)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import os, sys
print(f"TOKEN={os.environ.get('GITHUB_TOKEN', 'absent')}", file=sys.stderr)
print('{"poller": "x", "prompt": "ok"}')
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        pass_env=("GITHUB_TOKEN",),
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    stderr_payloads = [
        e.get("stderr", "")
        for e in events if e.get("type") == "poller_stderr"
    ]
    combined_stderr = "|".join(stderr_payloads)
    assert "TOKEN=absent" in combined_stderr


@pytest.mark.asyncio
async def test_run_poller_pass_env_secret_named_key_emits_event(
    tmp_path: Path, home: Path, monkeypatch,
):
    """When ``pass_env`` includes a key whose name matches a
    deny-list pattern (``*_TOKEN``, ``MIMIR_*``), the framework
    emits a ``poller_env_passthrough_named_secret`` event for
    visibility. The value itself is NOT logged."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_should_not_appear_in_event")
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print('{"poller": "x", "prompt": "ok"}')
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
        pass_env=("GITHUB_TOKEN",),
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)
    events = _read_events(home)
    passthrough_events = [
        e for e in events
        if e.get("type") == "poller_env_passthrough_named_secret"
    ]
    assert len(passthrough_events) == 1
    assert passthrough_events[0].get("key") == "GITHUB_TOKEN"
    # Value must NOT leak into the event payload.
    payload = json.dumps(passthrough_events[0])
    assert "ghp_secret_should_not_appear_in_event" not in payload


# ─── Algedonic signals from pollers ──────────────────────────────────


@pytest.mark.asyncio
async def test_signal_record_routed_to_log_event_not_enqueued(tmp_path, home):
    """A stdout line carrying ``"signal": "<event_type>"`` must NOT
    become an AgentEvent. It must land in events.jsonl with the
    declared event_type so the algedonic block picks it up next turn.
    """
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({
    "poller": "gmail-inbox",
    "signal": "poller_oauth_expired",
    "account": "muninn@muninnai.ai",
    "detail": "token refresh failed (invalid_grant)"
}))
""")
    cfg = PollerConfig(
        name="gmail-inbox", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)

    # Zero AgentEvents created — signal does not spawn a turn.
    assert n == 0
    assert enq.events == []

    # The signal landed in events.jsonl with its declared event_type +
    # payload + the framework's ``poller`` stamp.
    events = _read_events(home)
    by_type = {e["type"]: e for e in events}
    assert "poller_oauth_expired" in by_type, (
        f"expected poller_oauth_expired in {sorted(by_type)}"
    )
    sig = by_type["poller_oauth_expired"]
    assert sig["poller"] == "gmail-inbox"
    assert sig["account"] == "muninn@muninnai.ai"
    assert sig["detail"] == "token refresh failed (invalid_grant)"


def test_signal_event_types_classified_algedonically():
    """The pre-registered signal event types
    (``poller_oauth_expired`` / ``poller_auth_failed`` /
    ``poller_service_outage`` / ``poller_rate_limited`` /
    ``poller_signal`` / ``poller_nonzero_exit``) must all match a
    rule in ``feedback._EVENT_RULES`` so they surface in the
    algedonic block.
    """
    from mimir.feedback import _EVENT_RULES

    expected = {
        "poller_oauth_expired",
        "poller_auth_failed",
        "poller_service_outage",
        "poller_rate_limited",
        "poller_signal",
        "poller_nonzero_exit",
    }
    missing = expected - set(_EVENT_RULES.keys())
    assert not missing, f"signal event types not classified: {missing}"
    # All must be 'negative' polarity — these are pain signals.
    for evtype in expected:
        polarity, _slug = _EVENT_RULES[evtype]
        assert polarity == "negative", (
            f"{evtype} should be negative, got {polarity}"
        )


@pytest.mark.asyncio
async def test_signal_and_event_records_can_mix(tmp_path, home):
    """A poller run can emit MIXED records: some signals + some
    events. Signals route to log_event, events become AgentEvents,
    both flow through the same stdout JSONL stream."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
# A rate-limit signal + an actionable event in the same run.
print(json.dumps({"poller": "x", "signal": "poller_rate_limited", "service": "gmail", "retry_after_s": 60}))
print(json.dumps({"poller": "x", "prompt": "new message"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)

    assert n == 1
    assert len(enq.events) == 1
    assert "new message" in enq.events[0].content

    events = _read_events(home)
    by_type = {e["type"]: e for e in events}
    assert "poller_rate_limited" in by_type
    assert by_type["poller_rate_limited"]["service"] == "gmail"
    assert by_type["poller_rate_limited"]["retry_after_s"] == 60


@pytest.mark.asyncio
async def test_signal_only_run_still_reports_in_poller_complete(tmp_path, home):
    """A poller that emits ONLY signals (no actionable events) must
    still produce a ``poller_complete`` event, with the signal count
    reflected in ``signals_emitted``. Without that, the operator
    audit ledger would say 'nothing happened' on a run that was
    actually meaningful."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({"poller": "x", "signal": "poller_service_outage", "service": "gmail", "http_status": 503}))
print(json.dumps({"poller": "x", "signal": "poller_auth_failed", "account": "y@z.com"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)

    events = _read_events(home)
    complete = [e for e in events if e.get("type") == "poller_complete"]
    assert len(complete) == 1
    assert complete[0]["signals_emitted"] == 2
    assert complete[0]["events_emitted"] == 0


@pytest.mark.asyncio
async def test_signal_with_empty_string_is_dropped(tmp_path, home):
    """Empty / whitespace ``signal`` value is not a valid event type —
    drop the record rather than emit a malformed events.jsonl entry."""
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
# Empty + whitespace-only signals — both must be dropped.
print(json.dumps({"poller": "x", "signal": ""}))
print(json.dumps({"poller": "x", "signal": "   "}))
print(json.dumps({"poller": "x", "prompt": "actionable"}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)

    # Only the actionable event survived.
    assert n == 1
    events = _read_events(home)
    # poller_complete still fires; signals_emitted should be 0.
    complete = [e for e in events if e.get("type") == "poller_complete"]
    assert complete[0]["signals_emitted"] == 0


@pytest.mark.asyncio
async def test_signal_record_with_both_signal_and_prompt_routes_as_signal(
    tmp_path, home,
):
    """A defensive contract: if a single JSONL line has BOTH
    ``signal`` and ``prompt``, route as signal (the prompt is
    ignored). Documents the precedence so skill authors don't depend
    on dual-emit behavior — they should emit ONE record per shape.
    """
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({
    "poller": "x",
    "signal": "poller_signal",
    "prompt": "this should NOT spawn an AgentEvent",
    "note": "operator's choice but signal wins"
}))
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
    by_type = {e["type"]: e for e in events}
    assert "poller_signal" in by_type
    # The ``prompt`` field is stripped from the signal payload (the
    # framework drops ``signal``, ``poller``, ``prompt``, and
    # ``event_type`` to avoid noise and the log_event kwarg collision).
    assert "prompt" not in by_type["poller_signal"]
    assert by_type["poller_signal"]["note"] == "operator's choice but signal wins"


@pytest.mark.asyncio
async def test_signal_payload_event_type_key_does_not_collide(tmp_path, home):
    """Regression for Mimir PR #235 nit: a payload key named
    ``event_type`` would collide with ``log_event(event_type, **payload)``
    on the kwarg expansion (``TypeError: got multiple values for
    keyword argument 'event_type'``) and the signal would drop silently
    via the catch-all warning.

    Post-fix: ``event_type`` is on the strip list, so the signal
    surfaces normally and the original collision-key is dropped.
    """
    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", """
import json
print(json.dumps({
    "poller": "gmail-inbox",
    "signal": "poller_oauth_expired",
    "event_type": "invalid_grant",
    "account": "muninn@muninnai.ai"
}))
""")
    cfg = PollerConfig(
        name="gmail-inbox", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    n = await run_poller(cfg, enqueue=enq)
    assert n == 0

    events = _read_events(home)
    by_type = {e["type"]: e for e in events}
    # The signal landed (didn't get silently dropped by the
    # TypeError-via-catchall).
    assert "poller_oauth_expired" in by_type
    sig = by_type["poller_oauth_expired"]
    # The payload-side ``event_type`` is stripped — only the
    # framework's outer ``type`` matters. Useful unique fields survive.
    assert sig["account"] == "muninn@muninnai.ai"
    # Confirm there's no payload-side ``event_type`` key sneaking
    # through (would imply a future regression).
    payload_keys = {k for k in sig.keys() if k != "type"}
    assert "event_type" not in payload_keys
    # The complete-event signal counter caught it.
    complete = [e for e in events if e.get("type") == "poller_complete"]
    assert complete[0]["signals_emitted"] == 1


@pytest.mark.asyncio
async def test_unknown_signal_type_lands_in_events_jsonl(tmp_path, home):
    """Regression for Mimir PR #235 nit: an unrecognized signal
    event_type (not in ``feedback._EVENT_RULES``) must still land
    in events.jsonl so the operator can grep for it during
    debugging — it just won't surface in the algedonic block.

    Pins the docstring contract: "Unknown signal types still land
    in events.jsonl (grep-able for operator debugging) but don't
    enter the algedonic block."
    """
    from mimir.feedback import _EVENT_RULES

    unknown_signal = "some_skill_specific_signal_not_in_rules"
    # Sanity: confirm the test's chosen name is genuinely unknown so
    # adding a future rule with this name doesn't quietly weaken the
    # test.
    assert unknown_signal not in _EVENT_RULES, (
        "test name collision: this signal type is now classified — "
        "pick a different unknown name"
    )

    skill_dir = tmp_path / "skill"
    _install_script(skill_dir, "poller.py", f"""
import json
print(json.dumps({{
    "poller": "x",
    "signal": "{unknown_signal}",
    "detail": "an unclassified pain signal"
}}))
""")
    cfg = PollerConfig(
        name="x", command=f"{sys.executable} poller.py",
        cron="* * * * *", env={}, skill_dir=skill_dir,
    )
    enq = _CapturingEnqueue()
    await run_poller(cfg, enqueue=enq)

    events = _read_events(home)
    by_type = {e["type"]: e for e in events}
    # The unknown signal IS in events.jsonl with its declared type.
    assert unknown_signal in by_type
    assert by_type[unknown_signal]["detail"] == "an unclassified pain signal"
    assert by_type[unknown_signal]["poller"] == "x"
    # poller_complete tracks it in signals_emitted just like a
    # recognized signal — the framework doesn't gate on classification.
    complete = [e for e in events if e.get("type") == "poller_complete"]
    assert complete[0]["signals_emitted"] == 1
