"""Strong-ref helper for intentional fire-and-forget asyncio tasks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mimir.background_tasks import spawn_background


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
