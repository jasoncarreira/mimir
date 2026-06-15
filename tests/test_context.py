"""Unit tests for ``mimir/_context.py`` lookup primitives.

The contextvar + ``_active_turns`` registry is the core mechanism for
finding the active TurnContext from places where contextvar inheritance
is broken — hooks (CR#18) and MCP tool handlers (chainlink #23). These
tests pin the registry semantics for the two MCP-side helpers added in
chainlink #23 subissue #24:

- ``get_turn_by_saga_session_id`` — used by ``saga_end_session``
- ``get_only_active_turn`` — used by ``saga_query`` / ``store`` /
  ``feedback`` (best-effort, single-active-turn heuristic)
"""

from __future__ import annotations

import pytest

from mimir._context import (
    _active_turns,
    active_turn_snapshots,
    get_only_active_turn,
    get_turn_by_saga_session_id,
    reset_current_turn,
    set_current_turn,
)
from mimir.models import TurnContext


def _make_ctx(turn_id: str, saga_session_id: str | None) -> TurnContext:
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
    """Each test runs against a clean ``_active_turns`` so cross-test
    leakage (a missed reset) doesn't mask multi-active heuristic
    behavior."""
    snapshot = dict(_active_turns)
    _active_turns.clear()
    try:
        yield
    finally:
        _active_turns.clear()
        _active_turns.update(snapshot)


def test_get_turn_by_saga_session_id_hits_registered_turn():
    ctx = _make_ctx("t-1", "saga-c1-100-aaa")
    token = set_current_turn(ctx)
    try:
        found = get_turn_by_saga_session_id("saga-c1-100-aaa")
        assert found is ctx
    finally:
        reset_current_turn(token)


def test_get_turn_by_saga_session_id_returns_none_on_miss():
    ctx = _make_ctx("t-1", "saga-c1-100-aaa")
    token = set_current_turn(ctx)
    try:
        # Wrong saga_session_id — no match.
        assert get_turn_by_saga_session_id("saga-c1-200-bbb") is None
        # Empty arg returns None without iterating.
        assert get_turn_by_saga_session_id("") is None
        assert get_turn_by_saga_session_id(None) is None
    finally:
        reset_current_turn(token)


def test_get_turn_by_saga_session_id_finds_among_multiple():
    """When two turns are concurrently active (multi-channel deployment),
    the saga_session_id-based lookup must find the right one — this is
    the production scenario the CR#23 fix targets."""
    ctx_a = _make_ctx("t-a", "saga-channel-a-100")
    ctx_b = _make_ctx("t-b", "saga-channel-b-200")
    token_a = set_current_turn(ctx_a)
    token_b = set_current_turn(ctx_b)
    try:
        assert get_turn_by_saga_session_id("saga-channel-a-100") is ctx_a
        assert get_turn_by_saga_session_id("saga-channel-b-200") is ctx_b
        assert get_turn_by_saga_session_id("saga-channel-c-300") is None
    finally:
        # Reset in reverse order so the contextvar token chain unwinds
        # cleanly (set_current_turn returns Tokens that must be reset
        # in stack order).
        reset_current_turn(token_b)
        reset_current_turn(token_a)


def test_get_turn_by_saga_session_id_skips_turns_with_none_saga_session():
    """A turn with ``saga_session_id=None`` (e.g. some scheduled-tick
    triggers) must not match an empty-string lookup or accidentally
    match other turns."""
    ctx_no_saga = _make_ctx("t-1", None)
    ctx_with_saga = _make_ctx("t-2", "saga-c-100")
    token_1 = set_current_turn(ctx_no_saga)
    token_2 = set_current_turn(ctx_with_saga)
    try:
        # Lookup with empty arg returns None (not the no-saga turn).
        assert get_turn_by_saga_session_id("") is None
        assert get_turn_by_saga_session_id(None) is None
        # Lookup for the real saga_session_id finds the right ctx.
        assert get_turn_by_saga_session_id("saga-c-100") is ctx_with_saga
    finally:
        reset_current_turn(token_2)
        reset_current_turn(token_1)


def test_get_only_active_turn_returns_when_exactly_one():
    ctx = _make_ctx("t-1", "saga-c1-100")
    token = set_current_turn(ctx)
    try:
        assert get_only_active_turn() is ctx
    finally:
        reset_current_turn(token)


def test_get_only_active_turn_returns_none_when_zero_active():
    """Empty registry — heuristic must punt, not blow up."""
    assert get_only_active_turn() is None


def test_get_only_active_turn_returns_none_when_multiple_active():
    """Multi-active = ambiguous; the heuristic returns None and lets the
    caller emit a ``resolution_path=multi_active`` observability event
    rather than silently picking the wrong turn."""
    ctx_a = _make_ctx("t-a", "saga-channel-a-100")
    ctx_b = _make_ctx("t-b", "saga-channel-b-200")
    token_a = set_current_turn(ctx_a)
    token_b = set_current_turn(ctx_b)
    try:
        assert get_only_active_turn() is None
    finally:
        reset_current_turn(token_b)
        reset_current_turn(token_a)


def test_active_turn_snapshots_returns_bounded_diagnostic_metadata():
    ctx_a = _make_ctx("t-a", "saga-channel-a-100")
    ctx_a.started_at = 10.0
    ctx_a.tool_call_count = 3
    ctx_a.agent_id = "mimir-test"
    ctx_b = _make_ctx("t-b", "saga-channel-b-200")
    ctx_b.started_at = 18.0
    ctx_b.trigger = "scheduled_tick"
    ctx_b.channel_id = "scheduler:heartbeat"
    token_a = set_current_turn(ctx_a)
    token_b = set_current_turn(ctx_b)
    try:
        assert active_turn_snapshots(now=20.0) == [
            {
                "turn_id": "t-a",
                "trigger": "user_message",
                "channel_id": "c1",
                "age_s": 10.0,
                "tool_call_count": 3,
                "agent_id": "mimir-test",
            },
            {
                "turn_id": "t-b",
                "trigger": "scheduled_tick",
                "channel_id": "scheduler:heartbeat",
                "age_s": 2.0,
                "tool_call_count": 0,
            },
        ]
    finally:
        reset_current_turn(token_b)
        reset_current_turn(token_a)
