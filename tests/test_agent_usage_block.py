"""Regression tests for ``Agent._assemble_usage_block`` running off the
event loop (CR#5 → ``asyncio.to_thread``).

The block runs in a worker thread that has no running asyncio loop, so
the side-effect events (``cost_rate_advisory`` / ``cost_rate_alert`` /
``rate_limit_off_pace``) cannot be spawned via ``asyncio.create_task``
from inside the function — they are returned as a deferred list and
flushed by the caller on the dispatcher loop.

These tests pin:
- The function returns a ``(text, deferred_events)`` tuple shape.
- Running from a thread does not raise (the original bug:
  ``RuntimeError: no running event loop`` from ``_spawn_bg_task``).
- When alert conditions trigger, the deferred list contains the
  expected event tuples — confirming the side-effect intent is
  preserved across the refactor.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.channel_registry import ChannelRegistry

from .test_agent_outbound import _make_agent


def _write_turn(path: Path, *, ts: datetime, cost: float) -> None:
    """Minimal turns.jsonl line — only the fields ``aggregate_usage``
    inspects to derive the 1h cost rate."""
    rec = {
        "ts": ts.isoformat(),
        "turn_id": "t" + ts.isoformat()[:19],
        "session_id": "s",
        "saga_session_id": None,
        "trigger": "user_message",
        "channel_id": "chan",
        "input": "",
        "events": [],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": cost,
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


@pytest.mark.asyncio
async def test_assemble_usage_block_returns_tuple(tmp_path: Path):
    """Empty turns.jsonl → ``(None or rendered, [])`` — no deferred
    events, no exceptions."""
    reg = ChannelRegistry()
    agent = _make_agent(tmp_path, reg)

    text, deferred = agent._assemble_usage_block()
    assert isinstance(deferred, list)
    assert deferred == []  # no turns → no alert conditions
    # text may be None (no turns recorded) — that's fine
    assert text is None or isinstance(text, str)


@pytest.mark.asyncio
async def test_assemble_usage_block_safe_from_thread(tmp_path: Path):
    """Original CR#5 regression: calling from a worker thread used to
    raise ``RuntimeError: no running event loop`` because the function
    spawned bg-task log_event calls via ``asyncio.create_task``.

    Pin: even with conditions that *would* trigger an alert, calling
    from ``asyncio.to_thread`` returns cleanly and the alert event is
    in the deferred list."""
    reg = ChannelRegistry()
    agent = _make_agent(tmp_path, reg)
    # Force conditions that trip a cost-rate alert: tight hourly limit
    # + recent expensive turns. cost_hourly_limit_usd uses real wall
    # clock for the 1h aggregation window so write turns at real-now.
    agent._config.cost_hourly_limit_usd = 0.5  # very low limit
    real_now = datetime.now(tz=timezone.utc)
    turns = agent._config.turns_log
    turns.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        _write_turn(turns, ts=real_now - timedelta(minutes=5), cost=2.0)

    # The original bug: this call raised RuntimeError inside the
    # to_thread worker. Pin that it's clean now.
    text, deferred = await asyncio.to_thread(agent._assemble_usage_block)

    # The block was rendered (not None — turns were aggregated).
    assert text is not None
    # The deferred list carries the alert event tuple instead of
    # spawning it from inside the worker thread.
    kinds = [k for (k, _) in deferred]
    # Either cost_rate_advisory (quota mode) or cost_rate_alert (pay-go).
    assert any(
        k in ("cost_rate_advisory", "cost_rate_alert") for k in kinds
    ), f"expected cost-rate event in deferred, got {kinds}"
    # Each entry is (kind: str, kwargs: dict)
    for kind, kwargs in deferred:
        assert isinstance(kind, str)
        assert isinstance(kwargs, dict)
        # cost_rate_* events must carry the alert details so the
        # caller can pass them through to log_event.
        if kind in ("cost_rate_advisory", "cost_rate_alert"):
            assert "reason" in kwargs
            assert "rate_now_usd_per_hour" in kwargs
            assert "threshold_usd_per_hour" in kwargs
