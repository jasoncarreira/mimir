"""Test harness for simulating the SDK's MCP-handler task-fork dispatch
pattern (chainlink #23 subissue #28).

## Why this exists

MCP tool handlers in production hit a contextvar-staleness bug: the
``_current_turn`` ContextVar set in ``run_turn`` is invisible inside
the handler. The cause is the SDK's task-fork model:

1. ``client.connect()`` runs early (e.g. when the ClientPool acquires
   a client), and at some point inside connect the SDK calls
   ``spawn_detached(self._read_messages())``. The read-loop task
   captures contextvars at that moment — ``_current_turn=None``.
2. The read-loop task lives forever, processing incoming messages.
3. When an MCP ``tools/call`` arrives, the read-loop calls
   ``_spawn_control_request_handler``, which calls
   ``spawn_detached(self._handle_control_request(...))``. The new
   handler task COPIES contextvars from the read-loop task — so it
   sees ``_current_turn=None``.
4. Inside the handler, ``get_current_turn()`` returns ``None`` despite
   the run_turn task having called ``set_current_turn(ctx)`` later.

## How the harness works

We use Python's contextvar inheritance directly. ``asyncio.create_task``
accepts a ``context`` argument; passing
``context=contextvars.Context()`` (a brand-new empty context) is the
simplest way to spawn a task that DOES NOT inherit from the caller —
exactly what the read-loop's clean fork-at-connect produces.

A two-hop simulation (caller → read-loop → handler) is unnecessary
because contextvar inheritance via ``loop.create_task(context=X)``
uses ``X`` directly; there's no transitive read of the parent task.
The single-hop fresh-context spawn captures the bug class
faithfully.

## Usage

Tests that exercise sagatools MCP handlers under the production
dispatch path import ``dispatch_via_sdk_task_fork`` and pass the
handler closure + args. Existing direct-handler-call tests
(``await handler({...})``) bypass the bug and continue to work
unchanged — they're checking handler logic, not ctx-resolution.

This module's leading underscore keeps pytest from auto-collecting
it as a test file.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, Awaitable, Callable

# Tool handlers in sagatools.py / channeltools.py have the signature:
#   async def handler(args: dict[str, Any]) -> dict[str, Any]
# We accept anything that returns an awaitable, to keep the harness
# usable for non-MCP coroutines that need the same "fresh context"
# spawn behavior.
_HandlerAwaitable = Callable[[dict[str, Any]], Awaitable[Any]]


async def dispatch_via_sdk_task_fork(
    handler: _HandlerAwaitable,
    args: dict[str, Any],
) -> Any:
    """Run ``handler(args)`` on a task whose context was forked clean
    of the caller's contextvar sets — simulating the SDK's
    MCP-handler dispatch path.

    The handler runs to completion and its return value is awaited
    back into the caller. Exceptions raised inside the handler
    propagate out unchanged.

    The fresh context is built via ``contextvars.Context()`` — a
    brand-new empty context that inherits NOTHING from the caller.
    This is stronger than the production case (where the read-loop
    captured contextvars at connect time, not literally empty), but
    for the purposes of testing ctx-resolution under task-fork the
    behavior is equivalent: any contextvar set in the caller is
    invisible inside the handler.

    Returns the handler's awaited result. Caller is responsible for
    setting up / tearing down ``_active_turns`` registry state via
    ``set_current_turn`` / ``reset_current_turn`` before/after this
    call — that registry is module-global and survives across the
    fork (which is what the chainlink #23 fix relies on).
    """
    fresh_context = contextvars.Context()

    async def _runner() -> Any:
        return await handler(args)

    loop = asyncio.get_running_loop()
    task = loop.create_task(_runner(), context=fresh_context)
    return await task
