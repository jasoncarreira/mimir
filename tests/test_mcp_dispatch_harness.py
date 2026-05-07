"""Demonstration tests for the MCP-dispatch task-fork harness
(chainlink #23 subissue #28).

These tests pin two invariants the harness depends on:

1. **The bug exists.** A handler dispatched via
   ``dispatch_via_sdk_task_fork`` does NOT see contextvars set in
   the caller. Without this property, the harness wouldn't reproduce
   the production failure mode (chainlink #23 root cause).

2. **The fix path works.** Handlers dispatched the same way CAN
   see ``_active_turns`` registry entries via ``get_turn_by_session_id``
   (already on main, used by hooks). This is the foundation for the
   chainlink #23 fix in subissues #25-#27, where the saga MCP tools
   move from ``get_current_turn`` to a registry-based lookup.

These tests don't migrate any production code — that's subissues
#25-#27. The harness + these demonstrations are infrastructure only.
"""

from __future__ import annotations

import pytest

from mimir._context import (
    _active_turns,
    _current_turn,
    get_turn_by_session_id,
    reset_current_turn,
    set_current_turn,
)
from mimir.models import TurnContext

from ._mcp_dispatch import dispatch_via_sdk_task_fork


def _make_ctx(turn_id: str, saga_session_id: str | None = "saga-c1-100") -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        session_id="c1",
        trigger="user_message",
        channel_id="c1",
        started_at=0.0,
        saga_session_id=saga_session_id,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test runs against a clean ``_active_turns`` registry."""
    snapshot = dict(_active_turns)
    _active_turns.clear()
    try:
        yield
    finally:
        _active_turns.clear()
        _active_turns.update(snapshot)


@pytest.mark.asyncio
async def test_handler_dispatched_via_fork_does_not_see_contextvar():
    """The bug we're modeling: a contextvar set in the caller's task
    is invisible inside a handler dispatched via the fork. This is the
    production failure mode for sagatools MCP handlers — explains why
    saga_end_session's ``ctx.saga_end_session_called = True`` line
    silently no-ops, and why saga_query atom auto-credit appears
    silently broken (chainlink #23)."""
    ctx = _make_ctx("t-1")
    token = set_current_turn(ctx)
    try:
        # Sanity: contextvar IS set in the caller's task.
        assert _current_turn.get() is ctx

        # The "handler" reads the contextvar.
        async def fake_handler(args):
            return _current_turn.get()

        # Dispatch through the simulated SDK fork.
        result = await dispatch_via_sdk_task_fork(fake_handler, {})

        # The handler did NOT see the contextvar. This IS the bug —
        # it's what production sagatools handlers experience.
        assert result is None, (
            "harness no longer reproduces the SDK task-fork bug "
            "(contextvar leaked into the forked task — fix must "
            "have changed asyncio's create_task context semantics)"
        )
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
async def test_handler_dispatched_via_fork_sees_active_turns_registry():
    """The fix path: ``_active_turns`` is a module-global dict, not a
    contextvar — its contents survive the fork. A handler that
    looks up via ``get_turn_by_session_id`` (or, after subissue #24
    lands, ``get_turn_by_saga_session_id``) finds the live ctx even
    though contextvar inheritance is broken.

    This is the foundation chainlink #23 subissues #25-#27 build on:
    replace ``get_current_turn()`` (broken under fork) with a
    registry lookup (works under fork)."""
    ctx = _make_ctx("t-1")
    token = set_current_turn(ctx)
    try:
        async def fake_handler(args):
            # Lookup uses turn_id from args — in production this would
            # come from the SDK's session_id forwarding for hooks, or
            # from the model's saga_session_id arg for saga_end_session
            # (the subissue #24 lookup), or from the single-active-turn
            # heuristic for tools without any per-turn arg.
            return get_turn_by_session_id(args["turn_id"])

        result = await dispatch_via_sdk_task_fork(
            fake_handler, {"turn_id": "t-1"}
        )

        assert result is ctx, (
            "registry lookup failed across the fork — _active_turns "
            "should be module-global, not contextvar-bound"
        )
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
async def test_handler_exception_propagates_through_fork():
    """Sanity: if the handler raises, the exception surfaces in the
    caller's await. Without this property, tests that drive error
    paths through the harness wouldn't catch failures."""

    class _HandlerError(Exception):
        pass

    async def raising_handler(args):
        raise _HandlerError("boom")

    with pytest.raises(_HandlerError, match="boom"):
        await dispatch_via_sdk_task_fork(raising_handler, {})


@pytest.mark.asyncio
async def test_handler_return_value_propagates_through_fork():
    """Sanity: the handler's return value comes back to the caller
    intact. Tests that assert on tool-result content depend on this."""

    async def returning_handler(args):
        return {"echo": args.get("input", "")}

    result = await dispatch_via_sdk_task_fork(
        returning_handler, {"input": "hello"}
    )
    assert result == {"echo": "hello"}
