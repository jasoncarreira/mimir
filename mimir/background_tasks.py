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
BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS = 5.0


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


def _consume_late_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except BaseException:
        pass


async def cancel_background_tasks(
    tasks: set[asyncio.Task[Any]],
    *,
    label: str,
) -> list[BaseException]:
    snapshot = tuple(tasks)
    tasks.clear()
    if not snapshot:
        return []

    for task in snapshot:
        if not task.done():
            task.cancel()

    done, pending = await asyncio.wait(
        snapshot,
        timeout=BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS,
    )

    task_errors: list[tuple[str, BaseException]] = []
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            task_errors.append((task.get_name(), exc))

    task_errors.sort(key=lambda item: (item[0], type(item[1]).__name__))
    errors = [exc for _, exc in task_errors]
    if pending:
        for task in pending:
            task.add_done_callback(_consume_late_task_result)
        task_names = ", ".join(sorted(task.get_name() for task in pending))
        errors.append(
            TimeoutError(
                f"{label}: {len(pending)} background task(s) did not stop within "
                f"{BACKGROUND_TASK_CANCEL_TIMEOUT_SECONDS} seconds: {task_names}"
            )
        )
    return errors


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
