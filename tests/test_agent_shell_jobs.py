"""Agent's shell-job completion bridge — _handle_shell_job_complete +
_on_shell_job_complete dispatching shell_job_complete AgentEvents."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mimir.models import AgentEvent
from mimir.shell_jobs import ShellJob, ShellJobRegistry


class _FakeDispatcher:
    """Minimal stand-in for mimir.dispatcher.Dispatcher — collects
    enqueued events for assertion. Async-compatible."""

    def __init__(self):
        self.events: list[AgentEvent] = []
        self.raise_on_enqueue: BaseException | None = None

    async def enqueue(self, event: AgentEvent) -> bool:
        if self.raise_on_enqueue is not None:
            raise self.raise_on_enqueue
        self.events.append(event)
        return True


def _make_job(channel_id: str | None = "discord-1", *, exit_code: int = 0) -> ShellJob:
    """Construct a ShellJob with the fields a completion handler reads.
    Bypasses spawn() to keep the test purely about the handler logic."""
    job = ShellJob(
        job_id="j_testjob01",
        command="echo done",
        pid=12345,
        started_at=time.time() - 5.0,
        stdout_path=Path("/tmp/nonexistent.out"),
        stderr_path=Path("/tmp/nonexistent.err"),
        last_live_signal=time.time(),
        exit_code=exit_code,
        finished_at=time.time(),
        channel_id=channel_id,
    )
    return job


# Simulating a full Agent setup is heavy; the bridge methods are
# self-contained, so we test them with a minimal Agent stub that
# carries just the fields they read.
class _AgentBridgeStub:
    """Minimal Agent stand-in exposing just the fields the bridge
    methods use. Avoids the heavy Agent constructor for unit-level
    testing of the completion path."""

    def __init__(self, dispatcher, shell_jobs, loop):
        self._dispatcher = dispatcher
        self._shell_jobs = shell_jobs
        self._loop = loop

    # Bind the real methods at class level via late attribute lookup.
    from mimir.agent import Agent
    _handle_shell_job_complete = Agent._handle_shell_job_complete
    _on_shell_job_complete = Agent._on_shell_job_complete


@pytest.mark.asyncio
async def test_on_complete_enqueues_event_with_summary(tmp_path: Path):
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    dispatcher = _FakeDispatcher()
    loop = asyncio.get_running_loop()
    bridge = _AgentBridgeStub(dispatcher, registry, loop)

    # Synthesize a finished job that read_output knows about. spawn() is
    # cleaner than fabricating a ShellJob — we then await its completion.
    job = registry.spawn("echo hi", argv=["bash", "-c", "echo hi"], channel_id="c1")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if job.exit_code is not None:
            break
        await asyncio.sleep(0.05)
    assert job.exit_code is not None

    await bridge._on_shell_job_complete(job)

    assert len(dispatcher.events) == 1
    ev = dispatcher.events[0]
    assert ev.trigger == "shell_job_complete"
    assert ev.channel_id == "c1"
    assert "Shell job j_" in ev.content
    assert "exit_code=0" in ev.content
    assert "echo hi" in ev.content
    assert ev.extra["job_id"] == job.job_id
    assert ev.extra["exit_code"] == 0
    assert ev.source == "system"


@pytest.mark.asyncio
async def test_on_complete_drops_event_when_no_channel(tmp_path: Path):
    """When channel_id is None on the spawned job, completion silently
    drops (logs a no-channel event but doesn't enqueue). Without a
    channel there's no sensible routing target."""
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    dispatcher = _FakeDispatcher()
    loop = asyncio.get_running_loop()
    bridge = _AgentBridgeStub(dispatcher, registry, loop)

    # Initialize logger so log_event doesn't NPE under the hood.
    from mimir.event_logger import init_logger
    init_logger(tmp_path / "events.jsonl", session_id="test")

    job = _make_job(channel_id=None)
    await bridge._on_shell_job_complete(job)

    assert dispatcher.events == []


@pytest.mark.asyncio
async def test_on_complete_swallows_dispatcher_failure(tmp_path: Path):
    """A dispatcher.enqueue() that raises must not propagate out of the
    handler — that would crash the daemon-thread bridge."""
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    dispatcher = _FakeDispatcher()
    dispatcher.raise_on_enqueue = RuntimeError("dispatcher down")
    loop = asyncio.get_running_loop()
    bridge = _AgentBridgeStub(dispatcher, registry, loop)

    from mimir.event_logger import init_logger
    init_logger(tmp_path / "events.jsonl", session_id="test")

    job = registry.spawn("true", argv=["bash", "-c", "true"], channel_id="c1")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if job.exit_code is not None:
            break
        await asyncio.sleep(0.05)

    # Must not raise.
    await bridge._on_shell_job_complete(job)
    # And no event got recorded (because enqueue raised).
    assert dispatcher.events == []


@pytest.mark.asyncio
async def test_on_complete_truncates_long_output(tmp_path: Path):
    """A runaway job's stdout shouldn't blow the prompt budget — the
    summary caps each stream at ~4000 chars."""
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    dispatcher = _FakeDispatcher()
    loop = asyncio.get_running_loop()
    bridge = _AgentBridgeStub(dispatcher, registry, loop)

    # Spawn a job that produces ~10000 chars of stdout.
    cmd = "for i in $(seq 1 1000); do echo 'long-stdout-line-padding'; done"
    job = registry.spawn(cmd, argv=["bash", "-c", cmd], channel_id="c1")
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if job.exit_code is not None:
            break
        await asyncio.sleep(0.05)

    await bridge._on_shell_job_complete(job)

    assert len(dispatcher.events) == 1
    body = dispatcher.events[0].content
    # The body has the rendered summary; the stdout block must be
    # bounded. The body has wrapper text, but the actual stdout chunk
    # shouldn't dominate by orders of magnitude.
    assert len(body) < 6000  # well under the unbounded ~30KB+


def test_handle_complete_no_loop_is_silent(tmp_path: Path):
    """Bridge invoked before the loop is captured (e.g. unit tests
    that don't run a turn) silently no-ops rather than crashing."""
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    dispatcher = _FakeDispatcher()
    bridge = _AgentBridgeStub(dispatcher, registry, loop=None)

    job = _make_job(channel_id="c1")
    # No loop captured — must not raise.
    bridge._handle_shell_job_complete(job)
    assert dispatcher.events == []


def test_handle_complete_no_dispatcher_is_silent(tmp_path: Path):
    """Bridge with no dispatcher (e.g. test fixtures) silently no-ops."""
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    # No real loop needed since the early-out triggers first.

    class _Stub:
        from mimir.agent import Agent
        _handle_shell_job_complete = Agent._handle_shell_job_complete
        _on_shell_job_complete = Agent._on_shell_job_complete

    bridge = _Stub()
    bridge._loop = asyncio.new_event_loop()
    bridge._dispatcher = None
    bridge._shell_jobs = registry

    try:
        job = _make_job(channel_id="c1")
        bridge._handle_shell_job_complete(job)
    finally:
        bridge._loop.close()
