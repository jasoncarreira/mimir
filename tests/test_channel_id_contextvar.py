"""Tests for the channel_id ContextVar isolation (Change 3, S2-1 fix).

Verifies that:
- Two concurrent asyncio Tasks see their own isolated channel_id value.
- reset_current_channel_id(token) restores the prior value.
- _channel_from_config_or_state(None, None) falls through to the ContextVar.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from mimir.tools.registry import (
    _channel_from_config_or_state,
    _current_channel_id_var,
    reset_current_channel_id,
    set_current_channel_id,
)


class TestContextVarIsolation:
    def test_set_returns_token(self) -> None:
        """set_current_channel_id returns a contextvars.Token."""
        token = set_current_channel_id("chan-test")
        assert isinstance(token, contextvars.Token)
        reset_current_channel_id(token)

    def test_reset_restores_prior(self) -> None:
        """reset_current_channel_id(token) restores the value that was set before."""
        # Set an initial value.
        tok1 = set_current_channel_id("first-channel")
        # Overwrite it.
        tok2 = set_current_channel_id("second-channel")
        assert _current_channel_id_var.get() == "second-channel"
        # Reset to prior (first-channel).
        reset_current_channel_id(tok2)
        assert _current_channel_id_var.get() == "first-channel"
        # Reset to before first set (default None).
        reset_current_channel_id(tok1)
        assert _current_channel_id_var.get() is None

    def test_fallback_to_contextvar_when_no_config(self) -> None:
        """_channel_from_config_or_state(None, None) returns contextvar value."""
        token = set_current_channel_id("contextvar-channel")
        try:
            result = _channel_from_config_or_state(None, None)
            assert result == "contextvar-channel"
        finally:
            reset_current_channel_id(token)

    def test_contextvar_cleared_after_reset(self) -> None:
        """After reset to default None, _channel_from_config_or_state returns ''."""
        token = set_current_channel_id("some-channel")
        reset_current_channel_id(token)
        result = _channel_from_config_or_state(None, None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_concurrent_turns_isolated(self) -> None:
        """Two concurrent Tasks see their own isolated channel_id values."""
        barrier = asyncio.Event()
        results: dict[str, str] = {}

        async def turn(name: str, channel: str) -> None:
            token = set_current_channel_id(channel)
            try:
                # Wait at the barrier so both tasks are alive simultaneously
                # with different channels set.
                await asyncio.sleep(0)  # yield once so both tasks start
                barrier.set()
                await asyncio.sleep(0)  # yield again after barrier
                # Each task should see its own channel_id.
                results[name] = _channel_from_config_or_state(None, None)
            finally:
                reset_current_channel_id(token)

        async def runner() -> None:
            t1 = asyncio.create_task(turn("t1", "channel-alpha"))
            t2 = asyncio.create_task(turn("t2", "channel-beta"))
            await asyncio.gather(t1, t2)

        await runner()

        assert results["t1"] == "channel-alpha", (
            f"Task t1 expected 'channel-alpha', got {results['t1']!r}"
        )
        assert results["t2"] == "channel-beta", (
            f"Task t2 expected 'channel-beta', got {results['t2']!r}"
        )

    @pytest.mark.asyncio
    async def test_parent_context_not_leaked_to_child_task(self) -> None:
        """A child Task created after set_current_channel_id inherits the
        parent's value at creation time (copy semantics), but mutations in
        the child don't affect the parent."""
        parent_token = set_current_channel_id("parent-channel")
        child_saw: list[str] = []

        async def child_task() -> None:
            # Child inherits parent's value at task creation.
            child_saw.append(_channel_from_config_or_state(None, None))
            # Overwrite inside the child.
            child_token = set_current_channel_id("child-channel")
            child_saw.append(_channel_from_config_or_state(None, None))
            reset_current_channel_id(child_token)

        task = asyncio.create_task(child_task())
        await task

        # Parent still sees its own value.
        parent_val = _channel_from_config_or_state(None, None)
        reset_current_channel_id(parent_token)

        assert child_saw[0] == "parent-channel"
        assert child_saw[1] == "child-channel"
        assert parent_val == "parent-channel"
