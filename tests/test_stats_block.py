"""Shared assembly for the Resource usage stats block (mimir/stats_block.py).

The agent loop and the ``mimir stats`` CLI both feed through
``assemble_stats_block``. These tests pin:
- the happy path: aggregate + alert + plan + subagent → rendered body
- partial-failure: rate_limits raises → block still renders (plan
  lines empty)
- partial-failure: subagent_stats raises → block still renders
  (subagent body None)
- ``betas`` auto-defaults from ``cfg.context_1m``
- aggregate() exceptions BUBBLE (caller decides to skip)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir.stats_block import StatsBlockResult, assemble_stats_block


class _StubCfg:
    """Minimal Config-shaped stand-in for the helper. The real Config
    has dozens of fields we don't need here; we just stub the handful
    ``assemble_stats_block`` reads."""

    def __init__(self, tmp_path: Path, *, context_1m: bool = False):
        self.turns_log = tmp_path / "turns.jsonl"
        self.events_log = tmp_path / "events.jsonl"
        self.model = "claude-opus-4-7"
        self.cost_hourly_limit_usd = 0.0
        self.cost_rate_spike_ratio = 0.0
        self.cost_rate_spike_floor_usd = 0.0
        self.usage_5h_limit_usd = 0.0
        self.usage_weekly_limit_usd = 0.0
        self.context_1m = context_1m


def _ts(hours_ago: float = 0) -> str:
    return (
        datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat()


def _write_turn(path: Path, hours_ago: float, cost: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _ts(hours_ago),
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 100,
        },
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def test_assemble_returns_result_with_body_and_state(tmp_path: Path):
    """Happy path: turns recorded → block renders, state echoed."""
    cfg = _StubCfg(tmp_path)
    _write_turn(cfg.turns_log, hours_ago=1, cost=0.50)

    result = assemble_stats_block(cfg, rate_limit_current={})

    assert isinstance(result, StatsBlockResult)
    assert result.body is not None
    # Renderer emits a "Last turn" + "Last 5h" / "Last 7d" summary.
    assert "Last 5h" in result.body
    assert "Last 7d" in result.body
    assert result.alert is None  # thresholds at 0 → no alert
    assert result.off_pace == []
    assert result.rate_limit_current == {}


def test_assemble_returns_none_body_when_no_turns(tmp_path: Path):
    """No turns recorded yet → renderer returns None; helper passes it
    through. The CLI prints '(no turns recorded yet)' on this signal."""
    cfg = _StubCfg(tmp_path)

    result = assemble_stats_block(cfg, rate_limit_current={})
    assert result.body is None


def test_assemble_degrades_on_rate_limits_exception(tmp_path: Path):
    """``rate_limits.render_plan_quota_lines`` blowing up must NOT take
    out the whole block — pre-refactor the agent path caught this and
    rendered with empty plan lines. The shared helper preserves that."""
    cfg = _StubCfg(tmp_path)
    _write_turn(cfg.turns_log, hours_ago=1, cost=0.50)

    with patch(
        "mimir.stats_block.render_plan_quota_lines",
        side_effect=RuntimeError("boom"),
    ):
        result = assemble_stats_block(cfg, rate_limit_current={"plan": "bad"})

    # Body still rendered, just without plan lines / off_pace warning.
    assert result.body is not None
    assert result.off_pace == []


def test_assemble_degrades_on_subagent_stats_exception(tmp_path: Path):
    """``subagent_stats.aggregate`` blowing up must NOT take out the
    block — pre-refactor caught + logged, rendered with no subagent
    section. Shared helper preserves that."""
    cfg = _StubCfg(tmp_path)
    _write_turn(cfg.turns_log, hours_ago=1, cost=0.50)

    with patch(
        "mimir.stats_block.aggregate_subagents",
        side_effect=RuntimeError("boom"),
    ):
        result = assemble_stats_block(cfg, rate_limit_current={})

    # Body still rendered.
    assert result.body is not None


def test_assemble_aggregate_exception_bubbles(tmp_path: Path):
    """``aggregate()`` failure must propagate — the caller (agent or
    CLI) decides whether to skip the whole block. Pre-refactor the
    agent had a try/except around aggregate that returned (None, []);
    the shared helper bubbles so each caller picks its own behavior."""
    cfg = _StubCfg(tmp_path)

    with patch(
        "mimir.stats_block.aggregate",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            assemble_stats_block(cfg, rate_limit_current={})


def test_assemble_betas_default_from_context_1m(tmp_path: Path):
    """When ``cfg.context_1m`` is true, the helper passes
    ``[CONTEXT_1M_BETA]`` to the renderer so the % of context-window
    arithmetic uses the 1M cap. Pre-refactor the CLI didn't pass
    betas at all — auto-defaulting from cfg means the CLI output
    now matches what the agent renders."""
    from mimir.usage_stats import CONTEXT_1M_BETA

    cfg = _StubCfg(tmp_path, context_1m=True)
    _write_turn(cfg.turns_log, hours_ago=1, cost=0.50)

    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return "[rendered]"

    with patch("mimir.stats_block.render_usage_block", side_effect=_capture):
        assemble_stats_block(cfg, rate_limit_current={})

    assert captured.get("betas") == [CONTEXT_1M_BETA]


def test_assemble_betas_explicit_override(tmp_path: Path):
    """Caller can pass ``betas=[]`` to suppress the auto-default."""
    cfg = _StubCfg(tmp_path, context_1m=True)
    _write_turn(cfg.turns_log, hours_ago=1, cost=0.50)

    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return "[rendered]"

    with patch("mimir.stats_block.render_usage_block", side_effect=_capture):
        assemble_stats_block(cfg, rate_limit_current={}, betas=[])

    # Empty list → renderer gets ``betas=None`` (the ``betas or None``
    # short-circuit).
    assert captured.get("betas") is None
