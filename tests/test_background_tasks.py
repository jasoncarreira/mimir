"""Strong-ref helper for intentional fire-and-forget asyncio tasks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mimir import background_tasks
from mimir.background_tasks import cancel_background_tasks, spawn_background
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


@pytest.mark.asyncio
async def test_cancel_background_tasks_cancels_clears_and_returns_errors():
    tasks: set[asyncio.Task[Any]] = set()
    cancelled = asyncio.Event()
    failure = RuntimeError("background failure")

    async def wait_until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def succeed() -> str:
        return "done"

    async def fail() -> None:
        raise failure

    waiting_task = asyncio.create_task(wait_until_cancelled(), name="waiting")
    successful_task = asyncio.create_task(succeed(), name="successful")
    failed_task = asyncio.create_task(fail(), name="failed")
    tasks.update((waiting_task, successful_task, failed_task))
    await asyncio.sleep(0)

    errors = await cancel_background_tasks(tasks, label="test cleanup")

    assert not tasks
    assert waiting_task.cancelled()
    assert cancelled.is_set()
    assert successful_task.result() == "done"
    assert errors == [failure]


@pytest.mark.asyncio
async def test_cancel_background_tasks_times_out_cancellation_resistant_task_after_five_seconds(
    monkeypatch,
):
    tasks: set[asyncio.Task[Any]] = set()
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    late_results_consumed = asyncio.Event()
    consumed_names: set[str] = set()
    requested_timeouts: list[float | None] = []
    real_wait = asyncio.wait
    real_consumer = background_tasks._consume_late_task_result

    async def resistant(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError(f"late failure {index}")

    async def immediate_wait(awaitables, *, timeout=None):
        requested_timeouts.append(timeout)
        return await real_wait(awaitables, timeout=0)

    def consume_late_result(task: asyncio.Task[Any]) -> None:
        real_consumer(task)
        consumed_names.add(task.get_name())
        if len(consumed_names) == 2:
            late_results_consumed.set()

    monkeypatch.setattr(background_tasks.asyncio, "wait", immediate_wait)
    monkeypatch.setattr(
        background_tasks,
        "_consume_late_task_result",
        consume_late_result,
    )

    first = asyncio.create_task(resistant(0), name="zeta-task")
    second = asyncio.create_task(resistant(1), name="alpha-task")
    tasks.update((first, second))
    await asyncio.gather(*(event.wait() for event in started))

    errors = await cancel_background_tasks(tasks, label="runtime cleanup")

    assert background_tasks.BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS == 5.0
    assert requested_timeouts == [5.0]
    assert not tasks
    assert not first.done()
    assert not second.done()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert str(errors[0]) == (
        "runtime cleanup: 2 background task(s) did not stop within 5.0 seconds: "
        "alpha-task, zeta-task"
    )

    release.set()
    await asyncio.wait_for(late_results_consumed.wait(), timeout=1.0)
    assert consumed_names == {"alpha-task", "zeta-task"}
