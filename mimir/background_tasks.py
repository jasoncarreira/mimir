"""Utilities for fire-and-forget asyncio tasks.

``asyncio.create_task()`` returns a weakly referenced task. If the caller drops
the returned object, the task may be garbage-collected before completion. Use
``spawn_background`` for intentional fire-and-forget work so a strong reference
is retained until the task finishes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


def spawn_background(
    tasks: set[asyncio.Task[Any]],
    coro: Awaitable[Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Schedule *coro* and keep a strong ref in *tasks* until completion."""
    loop = asyncio.get_running_loop()
    task: asyncio.Task[Any] = loop.create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task
