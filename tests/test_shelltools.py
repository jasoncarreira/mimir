"""bash_async / bash_jobs_list / bash_job_output MCP tool handlers."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mimir import _context
from mimir.event_logger import init_logger
from mimir.models import TurnContext
from mimir.shell_jobs import ShellJobRegistry
from mimir.shelltools import build_shell_tools, shell_tool_names


@pytest.fixture(autouse=True)
def _ensure_event_logger(tmp_path):
    """bash_async emits ``bash_async_ctx_resolution`` + ``bash_async_spawned``
    events; the event logger must be initialized for these tests."""
    init_logger(tmp_path / "test-events.jsonl", session_id="test-shelltools")


@pytest.fixture(autouse=True)
def _isolate_active_turns():
    snapshot = dict(_context._active_turns)
    yield
    _context._active_turns.clear()
    _context._active_turns.update(snapshot)


def _make_registry(tmp_path: Path) -> ShellJobRegistry:
    return ShellJobRegistry(jobs_dir=tmp_path / "shell-jobs")


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(name)


def _wait_until_done(registry: ShellJobRegistry, job_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = registry.get(job_id)
        if job is not None and job.exit_code is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not exit within {timeout}s")


def _ctx(channel_id: str = "c1", saga_session_id: str = "saga-c1-100") -> TurnContext:
    return TurnContext(
        turn_id=f"t-{channel_id}",
        session_id=channel_id,
        trigger="user_message",
        channel_id=channel_id,
        started_at=0.0,
        saga_session_id=saga_session_id,
    )


# ─── shell_tool_names ─────────────────────────────────────────────────


def test_shell_tool_names_lists_three_tools():
    names = shell_tool_names()
    assert "mcp__mimir__bash_async" in names
    assert "mcp__mimir__bash_jobs_list" in names
    assert "mcp__mimir__bash_job_output" in names
    assert len(names) == 3


# ─── bash_async ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_async_spawns_and_returns_job_id(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    bash_async = _by_name(tools, "bash_async")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await bash_async.handler({"command": "echo hello"})
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is not True
    text = out["content"][0]["text"]
    assert "Spawned job j_" in text
    # Registry got the job.
    jobs = registry.all_jobs()
    assert len(jobs) == 1
    assert jobs[0].command == "echo hello"
    assert jobs[0].channel_id == "c1"  # from the contextvar fallback


@pytest.mark.asyncio
async def test_bash_async_resolves_via_session_id_under_fork(tmp_path: Path):
    """Mirrors chainlink #23 — when ``session_id`` is passed in args, the
    handler resolves to the matching turn even without contextvar
    inheritance (which is what happens under SDK MCP dispatch)."""
    from tests._mcp_dispatch import dispatch_via_sdk_task_fork

    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    bash_async = _by_name(tools, "bash_async")

    ctx = _ctx(channel_id="c-fork", saga_session_id="saga-fork-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await dispatch_via_sdk_task_fork(
            bash_async.handler,
            {"command": "echo forked", "session_id": "saga-fork-1"},
        )
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is not True
    jobs = registry.all_jobs()
    assert len(jobs) == 1
    assert jobs[0].channel_id == "c-fork"


@pytest.mark.asyncio
async def test_bash_async_missing_command_is_error(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    bash_async = _by_name(tools, "bash_async")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await bash_async.handler({})  # no command
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_bash_async_passes_on_complete_callback(tmp_path: Path):
    """The on_complete callback supplied to build_shell_tools must
    fire when a job spawned via the tool exits."""
    registry = _make_registry(tmp_path)
    fired = threading.Event()

    def on_complete(job):
        fired.set()

    tools = build_shell_tools(registry, on_complete=on_complete)
    bash_async = _by_name(tools, "bash_async")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        await bash_async.handler({"command": "true"})
    finally:
        _context.reset_current_turn(token)

    assert fired.wait(timeout=5.0), "on_complete callback didn't fire"


# ─── bash_jobs_list ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_jobs_list_empty_returns_no_jobs_message(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    jobs_list = _by_name(tools, "bash_jobs_list")

    out = await jobs_list.handler({})
    assert out.get("is_error") is not True
    assert "No jobs in scope" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_bash_jobs_list_renders_running(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    jobs_list = _by_name(tools, "bash_jobs_list")

    job = registry.spawn("sleep", argv=["bash", "-c", "sleep 2"])
    out = await jobs_list.handler({"scope": "running"})
    assert out.get("is_error") is not True
    text = out["content"][0]["text"]
    assert job.job_id in text
    assert "running" in text


@pytest.mark.asyncio
async def test_bash_jobs_list_invalid_scope_is_error(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    jobs_list = _by_name(tools, "bash_jobs_list")

    out = await jobs_list.handler({"scope": "garbage"})
    assert out.get("is_error") is True


# ─── bash_job_output ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_job_output_returns_tails(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    job_output = _by_name(tools, "bash_job_output")

    cmd = "echo out; echo err 1>&2"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    out = await job_output.handler({"job_id": job.job_id})
    text = out["content"][0]["text"]
    assert "out" in text
    assert "err" in text
    assert "stdout tail" in text
    assert "stderr tail" in text
    assert job.job_id in text


@pytest.mark.asyncio
async def test_bash_job_output_unknown_job_id_is_error(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    job_output = _by_name(tools, "bash_job_output")

    out = await job_output.handler({"job_id": "j_nonsense"})
    assert out.get("is_error") is True
    assert "unknown job_id" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_bash_job_output_stream_filter(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    job_output = _by_name(tools, "bash_job_output")

    cmd = "echo only-out; echo only-err 1>&2"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd])
    _wait_until_done(registry, job.job_id)

    out = await job_output.handler({"job_id": job.job_id, "stream": "stderr"})
    text = out["content"][0]["text"]
    assert "stderr tail" in text
    assert "stdout tail" not in text  # stream=stderr suppresses stdout block
    assert "only-err" in text


@pytest.mark.asyncio
async def test_bash_job_output_invalid_stream_is_error(tmp_path: Path):
    registry = _make_registry(tmp_path)
    tools = build_shell_tools(registry)
    job_output = _by_name(tools, "bash_job_output")

    job = registry.spawn("true", argv=["bash", "-c", "true"])
    _wait_until_done(registry, job.job_id)

    out = await job_output.handler({"job_id": job.job_id, "stream": "garbage"})
    assert out.get("is_error") is True
