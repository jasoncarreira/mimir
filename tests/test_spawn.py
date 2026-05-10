"""Tests for ``mimir.spawn`` — ``spawn_claude_code`` MCP tool (chainlink #60).

Coverage:
- pure helpers: _parse_spawn_result_json, _classify_terminal,
  _model_usage_to_record_usage, _build_spawn_record
- end-to-end tool invocation: argv shape, env_overlay (HOME override +
  CLAUDECODE strip), brief file lands at the right path
- profile defaults: code-implementer → $25, bench-runner → $10,
  doc-writer → $5
- completion path: happy / auth-fail (4xx + is_error) / work-fail
  (terminal_reason in {max-turns, max-budget-usd, errored}) / parse-fail
- spawn-failure: registry.spawn raises → tool returns is_error block
  before any job_id exists, no completion ever fires
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mimir.event_logger import init_logger
from mimir.models import TurnRecord
from mimir.shell_jobs import ShellJob
from mimir.spawn import (
    DEFAULT_AGENT,
    DEFAULT_TIMEOUT_SEC,
    PROFILE_DEFAULTS,
    _build_spawn_record,
    _classify_terminal,
    _model_usage_to_record_usage,
    _parse_spawn_result_json,
    build_spawn_tool,
)


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """MIMIR_HOME root with logger initialized so log_event won't crash."""
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-spawn")
    return tmp_path


def _read_events(home: Path) -> list[dict]:
    path = home / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


class _FakeRegistry:
    """Stand-in for ShellJobRegistry. Captures spawn args; lets tests
    fire on_complete with synthesized stdout. Spawn raises if
    ``raise_on_spawn`` is set, simulating spawn-failure."""

    def __init__(self) -> None:
        self.spawned: list[dict[str, Any]] = []
        self.last_on_complete = None
        self.raise_on_spawn: BaseException | None = None
        self._counter = 0

    def spawn(
        self,
        command: str,
        *,
        argv: list[str],
        channel_id: str | None = None,
        on_complete=None,
        env_overlay=None,
        cwd=None,
    ) -> ShellJob:
        if self.raise_on_spawn is not None:
            raise self.raise_on_spawn
        self._counter += 1
        job_id = f"j_test{self._counter:04d}"
        self.spawned.append({
            "command": command,
            "argv": argv,
            "channel_id": channel_id,
            "env_overlay": env_overlay,
            "cwd": cwd,
        })
        self.last_on_complete = on_complete
        return ShellJob(
            job_id=job_id,
            command=command,
            pid=999,
            started_at=time.time(),
            stdout_path=Path("/tmp/__nonexistent_spawn_stdout__"),
            stderr_path=Path("/tmp/__nonexistent_spawn_stderr__"),
            last_live_signal=time.time(),
            channel_id=channel_id,
        )


def _fire_completion(
    registry: _FakeRegistry,
    *,
    stdout_text: str,
    job_id: str = "j_test0001",
    channel_id: str | None = None,
    exit_code: int = 0,
    stdout_path: Path | None = None,
) -> None:
    """Synthesize a completed ShellJob with a real on-disk stdout file
    and invoke the captured on_complete callback as the registry would."""
    if stdout_path is None:
        raise ValueError("test must supply a writable stdout_path")
    stdout_path.write_text(stdout_text, encoding="utf-8")
    job = ShellJob(
        job_id=job_id,
        command="claude -p (test)",
        pid=999,
        started_at=time.time() - 1.0,
        stdout_path=stdout_path,
        stderr_path=stdout_path.with_suffix(".err"),
        last_live_signal=time.time(),
        exit_code=exit_code,
        finished_at=time.time(),
        channel_id=channel_id,
    )
    assert registry.last_on_complete is not None
    registry.last_on_complete(job)


# ─── Pure helpers ────────────────────────────────────────────────────


def test_parse_finds_trailing_json_after_noise():
    """The bundled CLI prints ``Shell cwd was reset to ...`` after the
    JSON. The parser must walk from the tail and pick up the JSON line."""
    text = '{"is_error": false, "terminal_reason": "completed"}\nShell cwd was reset to /mimir-home\n'
    assert _parse_spawn_result_json(text) == {
        "is_error": False,
        "terminal_reason": "completed",
    }


def test_parse_returns_none_for_unparseable():
    assert _parse_spawn_result_json("nothing here\nor here") is None
    assert _parse_spawn_result_json("") is None


def test_parse_handles_single_blob_no_newline():
    assert _parse_spawn_result_json('{"x": 1}') == {"x": 1}


def test_classify_completed():
    ev, fields = _classify_terminal({
        "is_error": False, "terminal_reason": "completed",
        "total_cost_usd": 0.5, "duration_ms": 1234,
    })
    assert ev == "claude_code_spawn_completed"
    assert fields["cost_usd"] == 0.5
    assert fields["terminal_reason"] == "completed"


def test_classify_auth_failed_4xx():
    ev, fields = _classify_terminal({
        "is_error": True, "api_error_status": 401,
        "terminal_reason": "errored",
    })
    assert ev == "claude_code_spawn_auth_failed"
    assert fields["api_error_status"] == 401


def test_classify_5xx_is_work_not_auth():
    """A 5xx server error is_error=true falls through to work-failed —
    the auth bucket is specifically 4xx (token / quota / scope), 5xx is
    operational."""
    ev, _ = _classify_terminal({
        "is_error": True, "api_error_status": 503,
        "terminal_reason": "errored",
    })
    assert ev == "claude_code_spawn_work_failed"


def test_classify_max_budget_is_work_failed():
    ev, _ = _classify_terminal({
        "is_error": True, "terminal_reason": "max-budget-usd",
        "total_cost_usd": 9.99,
    })
    assert ev == "claude_code_spawn_work_failed"


def test_classify_max_turns_without_is_error_still_work_failed():
    """``terminal_reason`` is the canonical signal — even when the CLI
    happens to set ``is_error: false`` on a max-turns exit, that's still
    a work failure (the agent didn't reach success)."""
    ev, _ = _classify_terminal({
        "is_error": False, "terminal_reason": "max-turns",
    })
    assert ev == "claude_code_spawn_work_failed"


def test_model_usage_translation_sums_across_models():
    out = _model_usage_to_record_usage({
        "claude-sonnet-4-5": {
            "inputTokens": 100, "outputTokens": 50,
            "cacheCreationInputTokens": 10, "cacheReadInputTokens": 200,
        },
        "claude-haiku-4-5": {
            "inputTokens": 50, "outputTokens": 25,
            "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0,
        },
    })
    assert out == {
        "input_tokens": 150,
        "output_tokens": 75,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 200,
    }


def test_model_usage_empty_returns_none():
    """Distinguishes ``no usage data`` from ``zero usage`` so the
    synthetic record's ``usage`` field stays absent (matches convention
    for turns that produced no usage)."""
    assert _model_usage_to_record_usage({}) is None


def test_build_record_marks_kind_and_pulls_cost():
    rec = _build_spawn_record(
        job_id="j_x",
        channel_id="discord-1",
        agent_name="bench-runner",
        parsed={
            "is_error": False, "terminal_reason": "completed",
            "total_cost_usd": 0.42, "duration_ms": 1234,
            "modelUsage": {"m1": {"inputTokens": 10, "outputTokens": 20,
                                  "cacheCreationInputTokens": 0,
                                  "cacheReadInputTokens": 0}},
            "subtype": "success", "stop_reason": "end_turn",
        },
        spawn_started_at=time.time() - 5,
        spawn_finished_at=time.time(),
    )
    assert isinstance(rec, TurnRecord)
    assert rec.kind == "claude_code_spawn"
    assert rec.trigger == "claude_code_spawn"
    assert rec.total_cost_usd == 0.42
    assert rec.duration_ms == 1234  # taken from parsed, not wall-clock
    assert rec.usage == {
        "input_tokens": 10, "output_tokens": 20,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    assert rec.result_subtype == "success"
    assert rec.stop_reason == "end_turn"
    assert rec.channel_id == "discord-1"
    assert rec.input == "spawn:bench-runner:j_x"


def test_build_record_falls_back_to_wall_clock_duration_when_unparseable():
    rec = _build_spawn_record(
        job_id="j_x",
        channel_id=None,
        agent_name="code-implementer",
        parsed=None,
        spawn_started_at=time.time() - 2.5,
        spawn_finished_at=time.time(),
    )
    assert rec.kind == "claude_code_spawn"
    # 2.5s as ms, +/- jitter from timing.
    assert 2400 <= rec.duration_ms <= 2700
    assert rec.total_cost_usd is None
    assert rec.usage is None


# ─── End-to-end tool wiring ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_invocation_writes_brief_and_builds_argv(home: Path):
    registry = _FakeRegistry()

    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    handler = tool_def.handler

    out = await handler({
        "brief": "Refactor the foo module.",
        "working_dir": "/workspace/mimir",
        "agent": "bench-runner",
    })
    assert out.get("is_error") is not True

    # Brief landed in the spawns dir.
    spawns_dir = home / "state" / "spawns"
    files = list(spawns_dir.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text() == "Refactor the foo module."

    # argv shape — head + agent + budget + setting-sources.
    [spawn] = registry.spawned
    argv = spawn["argv"]
    assert argv[0] == "timeout"
    assert argv[1] == str(DEFAULT_TIMEOUT_SEC)
    assert argv[2:5] == ["claude", "-p", "Refactor the foo module."]
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == "bench-runner"
    assert "--max-budget-usd" in argv
    bench_budget = argv[argv.index("--max-budget-usd") + 1]
    assert float(bench_budget) == 10.0  # bench-runner profile default
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--add-dir" in argv and argv[argv.index("--add-dir") + 1] == "/workspace/mimir"
    assert "--setting-sources" in argv

    # max_turns and model NOT in argv when not overridden — frontmatter wins.
    assert "--max-turns" not in argv
    assert "--model" not in argv

    # env_overlay carries HOME override and CLAUDECODE strip-marker.
    overlay = spawn["env_overlay"]
    assert overlay["HOME"] == str(home)
    assert overlay["CLAUDECODE"] is None
    # cwd was passed through.
    assert spawn["cwd"] == "/workspace/mimir"


@pytest.mark.asyncio
async def test_branch_arg_prepended_to_brief(home: Path):
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    await tool_def.handler({
        "brief": "Do the thing.",
        "working_dir": "/workspace/mimir",
        "branch": "feature-x",
    })
    [spawn] = registry.spawned
    # Brief argv element is the branch-annotated body, not the raw brief.
    brief_in_argv = spawn["argv"][4]
    assert brief_in_argv.startswith("<!-- branch: feature-x -->")
    assert "Do the thing." in brief_in_argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_name", "expected_budget"),
    [("code-implementer", 25.0), ("bench-runner", 10.0), ("doc-writer", 5.0)],
)
async def test_profile_defaults_inject_correct_budget(
    home: Path, agent_name: str, expected_budget: float,
) -> None:
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    await tool_def.handler({
        "brief": "x",
        "working_dir": "/tmp",
        "agent": agent_name,
    })
    [spawn] = registry.spawned
    argv = spawn["argv"]
    budget = float(argv[argv.index("--max-budget-usd") + 1])
    assert budget == expected_budget


@pytest.mark.asyncio
async def test_explicit_budget_overrides_profile_default(home: Path):
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    await tool_def.handler({
        "brief": "x",
        "working_dir": "/tmp",
        "agent": "code-implementer",
        "max_budget_usd": 7.5,
    })
    [spawn] = registry.spawned
    argv = spawn["argv"]
    assert float(argv[argv.index("--max-budget-usd") + 1]) == 7.5


@pytest.mark.asyncio
async def test_default_agent_is_code_implementer(home: Path):
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    await tool_def.handler({"brief": "x", "working_dir": "/tmp"})
    [spawn] = registry.spawned
    argv = spawn["argv"]
    assert argv[argv.index("--agent") + 1] == DEFAULT_AGENT
    assert DEFAULT_AGENT == "code-implementer"


# ─── Spawn-failure (raises before job_id) ─────────────────────────────


@pytest.mark.asyncio
async def test_spawn_failure_returns_is_error_block(home: Path):
    registry = _FakeRegistry()
    registry.raise_on_spawn = FileNotFoundError("cwd does not exist: /nope")

    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": "x", "working_dir": "/nope", "agent": "code-implementer",
    })
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "spawn_claude_code failed to launch" in text
    assert "cwd does not exist" in text

    # No on_complete was registered — registry never accepted the spawn.
    assert registry.last_on_complete is None
    # And a spawn_failed event landed.
    events = _read_events(home)
    assert any(e["type"] == "claude_code_spawn_spawn_failed" for e in events)


@pytest.mark.asyncio
async def test_brief_above_size_cap_rejected_before_spawn(home: Path):
    """A brief above MAX_BRIEF_BYTES gets a clear is_error block before
    registry.spawn — surfaces actionably instead of letting execve fail
    with the opaque ``argument list too long``."""
    from mimir.spawn import MAX_BRIEF_BYTES
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    huge_brief = "A" * (MAX_BRIEF_BYTES + 1)
    out = await tool_def.handler({
        "brief": huge_brief, "working_dir": "/tmp",
    })
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "spawn_claude_code failed to launch" in text
    assert "brief is" in text
    assert str(MAX_BRIEF_BYTES) in text
    # Registry never accepted the spawn — caller's caps protect execve.
    assert registry.spawned == []
    events = _read_events(home)
    spawn_failed = [
        e for e in events if e["type"] == "claude_code_spawn_spawn_failed"
    ]
    assert len(spawn_failed) == 1
    assert spawn_failed[0].get("reason") == "brief_too_large"


@pytest.mark.asyncio
async def test_brief_at_size_cap_accepted(home: Path):
    """Boundary case: brief exactly at MAX_BRIEF_BYTES should still
    spawn (the cap is `>`, not `>=`). Locks the inclusive/exclusive
    semantics so a future tightening doesn't silently break it."""
    from mimir.spawn import MAX_BRIEF_BYTES
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    at_cap_brief = "A" * MAX_BRIEF_BYTES
    out = await tool_def.handler({
        "brief": at_cap_brief, "working_dir": "/tmp",
    })
    assert out.get("is_error") is not True
    assert len(registry.spawned) == 1


# ─── claude_code_spawn_started event payload ─────────────────────────


@pytest.mark.asyncio
async def test_started_event_carries_redacted_argv_for_postmortem(home: Path):
    """The spawn_started event records the resolved cmd argv (with the
    brief redacted to a brief_path pointer) so post-mortem ``what
    flags did this spawn use?`` doesn't require greppin' the registry."""
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    await tool_def.handler({
        "brief": "secret-task-content-here",
        "working_dir": "/tmp",
        "agent": "code-implementer",
        "max_turns": 50,
        "model": "sonnet",
    })
    events = _read_events(home)
    started = [e for e in events if e["type"] == "claude_code_spawn_started"]
    assert len(started) == 1
    payload = started[0]
    assert "cmd_argv" in payload
    cmd_argv = payload["cmd_argv"]
    # All the resolved flags are present...
    assert "--agent" in cmd_argv
    assert cmd_argv[cmd_argv.index("--agent") + 1] == "code-implementer"
    assert "--max-turns" in cmd_argv
    assert cmd_argv[cmd_argv.index("--max-turns") + 1] == "50"
    assert "--model" in cmd_argv
    assert cmd_argv[cmd_argv.index("--model") + 1] == "sonnet"
    # ...but the brief content is NOT — only a pointer to brief_path.
    assert "secret-task-content-here" not in str(cmd_argv)
    assert any("<brief at " in s for s in cmd_argv if isinstance(s, str))
    # Resolved knobs surfaced as discrete fields too (lets log queries
    # filter by them without parsing argv).
    assert payload.get("resolved_max_turns") == 50
    assert payload.get("resolved_model") == "sonnet"


# ─── Completion path: 4 flavors ───────────────────────────────────────


def _drive_completion(
    home: Path,
    *,
    stdout_text: str,
    chain_called: list[bool] | None = None,
) -> tuple[_FakeRegistry, asyncio.AbstractEventLoop, list[asyncio.Task]]:
    """Helper: build the spawn tool against a running loop, invoke the
    handler, then synthesize an on-disk stdout file and fire on_complete
    from a different thread (matching the production waiter-thread
    dispatch). Returns the registry + loop + scheduled tasks for the
    caller to await."""
    raise NotImplementedError("driven inline below")


async def _run_completion_scenario(
    home: Path,
    *,
    stdout_text: str,
    expected_event_type: str,
    expect_chain_called: bool = True,
) -> dict[str, Any]:
    registry = _FakeRegistry()
    chain_calls: list[ShellJob] = []

    def chain(job: ShellJob) -> None:
        chain_calls.append(job)

    loop = asyncio.get_running_loop()

    def schedule(coro):
        # Production runs schedule from the waiter thread via
        # run_coroutine_threadsafe; in-test we await the coro directly
        # because the test harness runs the on_complete callback inline
        # on the same loop thread (see _fire_completion below).
        # Returning the task lets the caller await it.
        scheduled.append(loop.create_task(coro))

    scheduled: list[asyncio.Task] = []

    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,  # synthetic record write tested separately
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=schedule,
        chain_on_complete=chain,
    )
    await tool_def.handler({
        "brief": "x", "working_dir": "/tmp", "agent": "code-implementer",
    })

    stdout_path = home / "logs" / "j_test0001.out"
    stdout_path.parent.mkdir(exist_ok=True)
    _fire_completion(
        registry,
        stdout_text=stdout_text,
        stdout_path=stdout_path,
    )

    # Drain any scheduled coroutines so events land before assertion.
    if scheduled:
        await asyncio.gather(*scheduled)

    events = _read_events(home)
    matching = [e for e in events if e["type"] == expected_event_type]
    assert matching, f"expected {expected_event_type} in {[e['type'] for e in events]}"

    if expect_chain_called:
        assert len(chain_calls) == 1
        assert chain_calls[0].job_id == "j_test0001"
    else:
        assert chain_calls == []

    return matching[0]


@pytest.mark.asyncio
async def test_completion_clean_emits_completed(home: Path):
    stdout = (
        '{"type": "result", "subtype": "success", "is_error": false, '
        '"terminal_reason": "completed", "total_cost_usd": 0.42, '
        '"duration_ms": 1234, "modelUsage": {"m1": {"inputTokens": 100, '
        '"outputTokens": 50, "cacheCreationInputTokens": 0, '
        '"cacheReadInputTokens": 0}}}\nShell cwd was reset to /mimir-home\n'
    )
    ev = await _run_completion_scenario(
        home,
        stdout_text=stdout,
        expected_event_type="claude_code_spawn_completed",
    )
    assert ev["agent"] == "code-implementer"
    assert ev["cost_usd"] == 0.42
    assert ev["terminal_reason"] == "completed"


@pytest.mark.asyncio
async def test_completion_4xx_is_error_emits_auth_failed(home: Path):
    stdout = (
        '{"type": "result", "is_error": true, "api_error_status": 401, '
        '"terminal_reason": "errored", "total_cost_usd": 0.0}\n'
    )
    ev = await _run_completion_scenario(
        home,
        stdout_text=stdout,
        expected_event_type="claude_code_spawn_auth_failed",
    )
    assert ev["api_error_status"] == 401


@pytest.mark.asyncio
async def test_completion_max_budget_emits_work_failed(home: Path):
    stdout = (
        '{"type": "result", "is_error": true, '
        '"terminal_reason": "max-budget-usd", "total_cost_usd": 9.99}\n'
    )
    ev = await _run_completion_scenario(
        home,
        stdout_text=stdout,
        expected_event_type="claude_code_spawn_work_failed",
    )
    assert ev["terminal_reason"] == "max-budget-usd"


@pytest.mark.asyncio
async def test_completion_unparseable_stdout_emits_work_failed(home: Path):
    """JSON parse failure → work_failed with parse_failed=true; lets the
    operator distinguish ``the spawn died before emitting JSON`` from
    other work-failure shapes."""
    ev = await _run_completion_scenario(
        home,
        stdout_text="garbage\nno json here at all",
        expected_event_type="claude_code_spawn_work_failed",
    )
    assert ev.get("parse_failed") is True
    assert ev.get("terminal_reason") == "parse_failed"


# ─── Synthetic turn-record write ──────────────────────────────────────


class _FakeTurnLogger:
    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def write(self, record: TurnRecord) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_synthetic_turn_record_appended_on_clean_completion(home: Path):
    registry = _FakeRegistry()
    turn_logger = _FakeTurnLogger()
    loop = asyncio.get_running_loop()
    scheduled: list[asyncio.Task] = []

    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=turn_logger,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: scheduled.append(loop.create_task(coro)),
        chain_on_complete=None,
    )
    await tool_def.handler({
        "brief": "x", "working_dir": "/tmp", "agent": "doc-writer",
    })
    stdout = (
        '{"type": "result", "subtype": "success", "is_error": false, '
        '"terminal_reason": "completed", "total_cost_usd": 0.07, '
        '"duration_ms": 800, "modelUsage": {"claude-sonnet-4-5": {'
        '"inputTokens": 200, "outputTokens": 75, '
        '"cacheCreationInputTokens": 0, "cacheReadInputTokens": 0}}}\n'
    )
    stdout_path = home / "logs" / "j_test0001.out"
    stdout_path.parent.mkdir(exist_ok=True)
    _fire_completion(
        registry, stdout_text=stdout, stdout_path=stdout_path,
    )
    if scheduled:
        await asyncio.gather(*scheduled)

    assert len(turn_logger.records) == 1
    rec = turn_logger.records[0]
    assert rec.kind == "claude_code_spawn"
    assert rec.total_cost_usd == 0.07
    assert rec.usage == {
        "input_tokens": 200, "output_tokens": 75,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    assert rec.input == "spawn:doc-writer:j_test0001"


# ─── Profile-defaults dict invariants ─────────────────────────────────


def test_profile_defaults_contract_matches_spec():
    """Locks the chainlink-50 §5 operator decisions: code-implementer
    $25 / bench-runner $10 / doc-writer $5. Profile additions should
    update this test deliberately."""
    assert PROFILE_DEFAULTS == {
        "code-implementer": {"max_budget_usd": 25.0},
        "bench-runner":     {"max_budget_usd": 10.0},
        "doc-writer":       {"max_budget_usd": 5.0},
    }


# ─── Brief shell-substitution literal rejection (Shape 1 fix) ─────────


@pytest.mark.parametrize("bogus_brief", [
    "$(cat /tmp/sub_a_brief.md)",
    "  $(cat /tmp/foo.md)  ",                          # whitespace tolerated
    "`cat /tmp/foo.md`",
    "${BRIEF_VAR}",
    "$(generate-brief --target ntfy)",
])
@pytest.mark.asyncio
async def test_brief_pure_shell_substitution_literal_rejected(
    home: Path, bogus_brief: str,
):
    """A brief whose entire content is a literal shell-substitution
    token (``$(...)``, backticks, or ``${...}``) is the canonical
    caller anti-pattern: tool args are passed verbatim to ``claude
    -p`` (no shell), the spawn receives ~30 bytes of unparseable
    text, and parse-fails after ~15s. Detect pre-spawn and reject
    with an actionable error pointing at the actual fix (inline the
    file content into the brief argument). Lock down with multiple
    canonical shapes so future contributors can't accidentally
    narrow the regex."""
    from mimir.spawn import MAX_BRIEF_BYTES  # noqa: F401  (sanity-check exported)
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": bogus_brief, "working_dir": "/tmp",
    })
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "shell-substitution" in text
    assert "verbatim" in text or "no shell" in text.lower()
    # No spawn happened — caller-side caps protect spawn slot.
    assert registry.spawned == []
    events = _read_events(home)
    spawn_failed = [
        e for e in events if e["type"] == "claude_code_spawn_spawn_failed"
    ]
    assert len(spawn_failed) == 1
    assert spawn_failed[0].get("reason") == "brief_shell_substitution_literal"


@pytest.mark.asyncio
async def test_brief_containing_shell_sub_mid_text_accepted(home: Path):
    """A real brief that mentions ``$(foo)`` somewhere in its body
    (e.g. as a code sample or shell snippet for the spawn to run) is
    NOT rejected — only the pure-token shape (the entire stripped
    brief is a single substitution) is the anti-pattern. Locks the
    regex against future broadening that would false-positive on
    legitimate briefs."""
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    legit_brief = (
        "Implement a helper that uses $(date -u +%FT%TZ) to stamp the "
        "log header, and verify with `git log --oneline -3`.\n\n"
        "Acceptance criteria:\n- helper writes to mimir/_ntfy.py\n"
        "- tests cover the dedup branch\n"
    )
    out = await tool_def.handler({
        "brief": legit_brief, "working_dir": "/tmp",
    })
    # Did not error on the brief shape; spawn was attempted.
    assert out.get("is_error") is not True
    assert len(registry.spawned) == 1


@pytest.mark.asyncio
async def test_brief_pure_shell_sub_above_detect_cap_passes_through(
    home: Path,
):
    """The shell-sub detector is length-bounded (4 KB) so a huge
    brief that *coincidentally* starts with ``$(`` and ends with
    ``)`` doesn't hit the rejection path. Real briefs are larger
    than the size at which the bug occurs (the bug is "I tried
    inline a file"; the substitution token is by definition tiny).
    The bound matters because at large sizes the regex would scan
    the whole content unnecessarily."""
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    # 5 KB body, framed by parens — past the 4 KB detect cap.
    big_brief = "$(" + ("X" * 5000) + ")"
    out = await tool_def.handler({
        "brief": big_brief, "working_dir": "/tmp",
    })
    assert out.get("is_error") is not True
    assert len(registry.spawned) == 1


# ─── permission_mode arg + argv flag (Shape 2 fix) ────────────────────


@pytest.mark.asyncio
async def test_permission_mode_default_is_bypass(home: Path):
    """Default ``permission_mode`` is ``bypassPermissions`` — without
    it, ``claude -p`` blocks on interactive prompts that no human can
    answer (the live failure shape on 2026-05-09: spawn ran 7m,
    burned $1.95, exited 0 with zero files written because every
    Write/git-checkout asked for permission and got nothing).

    Lock the default + lock the flag landing in argv — a future
    contributor flipping the default to a stricter mode would
    silently regress the autonomous-spawn use case. Operators can
    still override per-spawn via the ``permission_mode`` arg."""
    from mimir.spawn import DEFAULT_PERMISSION_MODE
    assert DEFAULT_PERMISSION_MODE == "bypassPermissions"
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": "implement a small helper", "working_dir": "/tmp",
    })
    assert out.get("is_error") is not True
    assert len(registry.spawned) == 1
    argv = registry.spawned[0]["argv"]
    # Flag + value land contiguously in argv.
    assert "--permission-mode" in argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "bypassPermissions"


@pytest.mark.asyncio
async def test_permission_mode_explicit_override(home: Path):
    """Operator-supplied ``permission_mode`` overrides the default —
    e.g. ``acceptEdits`` for a less-trusted brief where Bash should
    still ask. Lock the override path so the default never silently
    overrides an explicit caller choice."""
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": "implement a small helper",
        "working_dir": "/tmp",
        "permission_mode": "acceptEdits",
    })
    assert out.get("is_error") is not True
    argv = registry.spawned[0]["argv"]
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "acceptEdits"


@pytest.mark.asyncio
async def test_permission_mode_invalid_rejected(home: Path):
    """An invalid permission_mode (typo, made-up value) is rejected
    pre-spawn with an actionable error listing the allowed set —
    not silently passed through to ``claude -p`` which would error
    less helpfully ~15s in."""
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": "ok", "working_dir": "/tmp",
        "permission_mode": "yolo-mode",
    })
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "permission_mode" in text
    assert "yolo-mode" in text
    assert "bypassPermissions" in text  # the default surfaces in the error
    assert registry.spawned == []
    events = _read_events(home)
    assert any(
        e.get("reason") == "invalid_permission_mode"
        for e in events
    )


@pytest.mark.asyncio
async def test_permission_mode_lands_in_started_event(home: Path):
    """The resolved permission_mode is recorded on the
    claude_code_spawn_started event so post-mortem ``which mode did
    this spawn run under?`` is one grep away — same shape as
    resolved_model and resolved_max_turns."""
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: coro.close(),
        chain_on_complete=None,
    )
    await tool_def.handler({
        "brief": "ok", "working_dir": "/tmp",
        "permission_mode": "acceptEdits",
    })
    events = _read_events(home)
    started = [e for e in events if e["type"] == "claude_code_spawn_started"]
    assert len(started) == 1
    assert started[0]["resolved_permission_mode"] == "acceptEdits"


# ─── CR2-#5: spawn completion accounting fallback path ───────────────


@pytest.mark.asyncio
async def test_spawn_completion_writes_orphan_sidecar_when_loop_closed(
    home: Path, tmp_path: Path,
):
    """CR2-#5 regression: when ``schedule_from_thread`` returns False
    (loop unavailable / closed / pre-first-turn), the spawn's
    accounting must NOT silently disappear. ``_on_complete`` writes a
    sidecar entry to ``<mimir_home>/spawn-orphans.jsonl`` so the
    operator can replay it on next startup."""
    registry = _FakeRegistry()
    # schedule_from_thread that always reports "dropped" — simulates
    # the shutdown / pre-first-turn case. Closes the coro to suppress
    # the "never awaited" warning, then returns False to mirror what
    # ``Agent._schedule_from_thread`` would have done.
    def _dropping_schedule(coro):
        coro.close()
        return False
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=_dropping_schedule,
        chain_on_complete=None,
    )
    await tool_def.handler({"brief": "ok", "working_dir": "/tmp"})
    # Synthesize completion with parseable JSON.
    stdout_path = tmp_path / "spawn_stdout.json"
    _fire_completion(
        registry,
        stdout_text='{"is_error": false, "terminal_reason": "completed"}',
        exit_code=0,
        stdout_path=stdout_path,
    )

    # The orphan sidecar must exist with one entry.
    sidecar = home / "spawn-orphans.jsonl"
    assert sidecar.is_file(), (
        "spawn-orphans.jsonl must be written when loop is unavailable"
    )
    lines = [
        json.loads(line)
        for line in sidecar.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["event_type"] == "claude_code_spawn_completed"
    assert "event_payload" in entry
    assert entry["event_payload"]["agent"]
    assert "turn_record" in entry
    assert entry["turn_record"]["kind"] == "claude_code_spawn"


@pytest.mark.asyncio
async def test_spawn_completion_no_orphan_when_loop_available(
    home: Path, tmp_path: Path,
):
    """When schedule_from_thread succeeds (returns True), no sidecar
    is written — the regular log_event + turn_logger path handles
    accounting."""
    registry = _FakeRegistry()
    captured: list = []

    def schedule(coro):
        # Don't actually run the coroutine — pytest-asyncio's loop is
        # busy. Close it to suppress the "never awaited" warning and
        # report success, which is what the agent's
        # ``_schedule_from_thread`` would have done if the dispatch
        # to the live loop succeeded.
        captured.append(True)
        coro.close()
        return True

    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=schedule,
        chain_on_complete=None,
    )
    await tool_def.handler({"brief": "ok", "working_dir": "/tmp"})
    stdout_path = tmp_path / "spawn_stdout.json"
    _fire_completion(
        registry,
        stdout_text='{"is_error": false, "terminal_reason": "completed"}',
        exit_code=0,
        stdout_path=stdout_path,
    )
    assert captured == [True]
    sidecar = home / "spawn-orphans.jsonl"
    assert not sidecar.exists(), (
        f"sidecar must NOT be written when schedule succeeded, got: {sidecar}"
    )


# ─── PR #110 review-followup: missing test pins ────────────────────────


@pytest.mark.asyncio
async def test_spawn_rejects_unknown_agent_name(home: Path):
    """Reject a spawn before launch when agent_name doesn't match any
    profile in ``<home>/.claude/agents/*.md``. Pre-fix the unknown name
    flowed through to ``claude -p --agent <name>``; the spawn launched,
    hit profile-not-found error from the bundled CLI ~15s in, burned
    the spawn budget + timeout to discover a typo."""
    # Plant two known profiles.
    agents_dir = home / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "code-implementer.md").write_text("---\nname: code-implementer\n---\n")
    (agents_dir / "doc-writer.md").write_text("---\nname: doc-writer\n---\n")

    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: (coro.close(), False)[1],
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": "ok", "working_dir": "/tmp",
        "agent": "mighty-implementer",  # not installed
    })
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "mighty-implementer" in text
    assert "code-implementer" in text  # available list surfaced
    assert "doc-writer" in text
    # Spawn never launched.
    assert registry.spawned == []
    events = _read_events(home)
    assert any(
        e.get("reason") == "agent_profile_not_found"
        for e in events
    )


@pytest.mark.asyncio
async def test_spawn_passes_when_agents_dir_missing(home: Path):
    """If <home>/.claude/agents/ doesn't exist, validation falls through
    silently (bench / non-spawn-using deployments shouldn't be blocked
    by a missing dir). The spawn proceeds and any failure surfaces from
    the bundled CLI itself."""
    # Don't create agents_dir.
    registry = _FakeRegistry()
    [tool_def] = build_spawn_tool(
        registry=registry,
        turn_logger=None,
        mimir_home=home,
        spawns_dir=home / "state" / "spawns",
        schedule_from_thread=lambda coro: (coro.close(), False)[1],
        chain_on_complete=None,
    )
    out = await tool_def.handler({
        "brief": "ok", "working_dir": "/tmp",
        "agent": "anything-goes",
    })
    # Spawn launched (no validation when agents_dir is missing).
    assert out.get("is_error") is not True
    assert len(registry.spawned) == 1
