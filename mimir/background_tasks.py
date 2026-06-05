"""Utilities for fire-and-forget asyncio tasks.

``asyncio.create_task()`` returns a weakly referenced task. If the caller drops
the returned object, the task may be garbage-collected before completion. Use
``spawn_background`` for intentional fire-and-forget work so a strong reference
is retained until the task finishes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from .event_logger import log_event_sync
from .redaction import redact_text

log = logging.getLogger(__name__)
_MAX_ERROR_CHARS = 500


def _bounded_error(exc: BaseException) -> str:
    error = redact_text(f"{type(exc).__name__}: {exc}")
    if len(error) > _MAX_ERROR_CHARS:
        return f"{error[:_MAX_ERROR_CHARS]}…"
    return error


def _discard_and_log_failure(
    tasks: set[asyncio.Task[Any]],
    task: asyncio.Task[Any],
) -> None:
    try:
        tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        log_event_sync(
            "background_task_failed",
            name=task.get_name(),
            error=_bounded_error(exc),
        )
    except Exception as callback_exc:  # noqa: BLE001
        log.warning("background task completion callback failed: %s", callback_exc)


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
    task.add_done_callback(lambda done: _discard_and_log_failure(tasks, done))
    return task
