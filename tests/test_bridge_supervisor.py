"""Tests for mimir.bridges._supervisor (chainlink #246).

Both DiscordBridge and SlackBridge previously carried private copies of
should_emit_retry_algedonic and safe_log_event. A throttle / safe-log
fix on one bridge silently failed to propagate to the other — see the
2026-05 history where the Discord-side fix landed weeks before Slack.
The shared module unifies them; these tests pin the contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mimir.bridges._supervisor import (
    safe_log_event,
    should_emit_retry_algedonic,
)


class TestShouldEmitRetryAlgedonic:
    """The throttle: silent for attempts 1-2 (early noise), every attempt
    3-9 (the "is this real?" early-warning window), then every 10th from
    10 onward (sustained-outage throttle)."""

    @pytest.mark.parametrize("attempt", [1, 2])
    def test_silent_below_attempt_3(self, attempt: int) -> None:
        assert should_emit_retry_algedonic(attempt) is False

    @pytest.mark.parametrize("attempt", [3, 4, 5, 6, 7, 8, 9])
    def test_fires_every_attempt_3_through_9(self, attempt: int) -> None:
        assert should_emit_retry_algedonic(attempt) is True

    @pytest.mark.parametrize("attempt", [10, 20, 30, 100])
    def test_fires_on_multiples_of_ten(self, attempt: int) -> None:
        assert should_emit_retry_algedonic(attempt) is True

    @pytest.mark.parametrize("attempt", [11, 12, 15, 19, 21, 99])
    def test_silent_between_multiples_of_ten(self, attempt: int) -> None:
        assert should_emit_retry_algedonic(attempt) is False


class TestSafeLogEvent:
    """The wrapper: forwards to event_logger.log_event when it works;
    catches and logs any exception so a misbehaving sink can't crash the
    supervisor reconnect loop."""

    @pytest.mark.asyncio
    async def test_forwards_to_log_event(self) -> None:
        mock_log_event = AsyncMock()
        with patch("mimir.event_logger.log_event", mock_log_event):
            await safe_log_event("TestBridge", "test_event", error="boom")
        mock_log_event.assert_awaited_once_with("test_event", error="boom")

    @pytest.mark.asyncio
    async def test_swallows_log_event_exceptions(self) -> None:
        """If log_event raises, the supervisor reconnect loop must not
        crash. Exception is logged via log.exception (assertable via
        caplog), then swallowed."""
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("event sink down")

        with patch("mimir.event_logger.log_event", _boom):
            # Must not raise.
            await safe_log_event("TestBridge", "test_event")

    @pytest.mark.asyncio
    async def test_logs_with_bridge_label(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exception log line carries the bridge label so an operator
        scanning the log can tell which bridge's supervisor saw the
        sink-failure."""
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("event sink down")

        with caplog.at_level("ERROR", logger="mimir.bridges._supervisor"), \
             patch("mimir.event_logger.log_event", _boom):
            await safe_log_event("DiscordBridge", "discord_bridge_retry")
            await safe_log_event("SlackBridge", "slack_bridge_retry")

        messages = [r.getMessage() for r in caplog.records]
        assert any("DiscordBridge" in m and "discord_bridge_retry" in m
                   for m in messages)
        assert any("SlackBridge" in m and "slack_bridge_retry" in m
                   for m in messages)
