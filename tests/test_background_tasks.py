"""Strong-ref helper for intentional fire-and-forget asyncio tasks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mimir.background_tasks import spawn_background
from mimir.event_logger import _reset_logger_for_tests, init_logger


@pytest.mark.asyncio
async def test_spawn_background_holds_ref_until_task_finishes():
    tasks: set[asyncio.Task[Any]] = set()
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> str:
        started.set()
        await release.wait()
        return "done"

    task = spawn_background(tasks, work(), name="test-bg-task")

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert task in tasks
    assert not task.done()

    release.set()
    assert await asyncio.wait_for(task, timeout=1.0) == "done"
    await asyncio.sleep(0)
    assert task not in tasks


@pytest.fixture(autouse=True)
def reset_event_logger():
    _reset_logger_for_tests()
    yield
    _reset_logger_for_tests()


async def _drain_task_callback() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_spawn_background_logs_task_failure(tmp_path):
    events = tmp_path / "events.jsonl"
    init_logger(events, session_id="test-session")
    tasks: set[asyncio.Task[Any]] = set()

    async def fail() -> None:
        raise RuntimeError("token=github_pat_secret")

    task = spawn_background(tasks, fail(), name="boom-task")

    with pytest.raises(RuntimeError):
        await task
    await _drain_task_callback()

    assert task not in tasks
    text = events.read_text()
    assert '"type": "background_task_failed"' in text
    assert '"name": "boom-task"' in text
    assert "RuntimeError: token=[REDACTED]" in text
    assert "github_pat_secret" not in text


@pytest.mark.asyncio
async def test_spawn_background_cancel_is_not_failure(tmp_path):
    events = tmp_path / "events.jsonl"
    init_logger(events, session_id="test-session")
    tasks: set[asyncio.Task[Any]] = set()
    started = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = spawn_background(tasks, wait_forever(), name="cancel-task")

    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _drain_task_callback()

    assert task not in tasks
    assert not events.exists() or "background_task_failed" not in events.read_text()


@pytest.mark.asyncio
async def test_spawn_background_done_callback_swallows_logging_failure(monkeypatch):
    tasks: set[asyncio.Task[Any]] = set()

    def broken_log_event_sync(*args: Any, **kwargs: Any) -> None:
        raise OSError("logger broken")

    monkeypatch.setattr("mimir.background_tasks.log_event_sync", broken_log_event_sync)

    async def fail() -> None:
        raise RuntimeError("boom")

    task = spawn_background(tasks, fail(), name="callback-safe-task")

    with pytest.raises(RuntimeError):
        await task
    await _drain_task_callback()

    assert task not in tasks
