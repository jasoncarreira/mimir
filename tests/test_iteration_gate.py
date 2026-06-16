"""Per-turn model-iteration ceiling — 3-tier (chainlink #511)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mimir._context import reset_current_turn, set_current_turn
from mimir.event_logger import init_logger
from mimir.models import TurnContext
from mimir.tools.iteration_gate import IterationGateMiddleware


def _ctx(budget: int = 20) -> TurnContext:
    # budget 20 → tiers at 15 (75%), 18 (90%), 20 (100%) — cleanly distinct.
    return TurnContext(
        turn_id="t-iter",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        iteration_budget=budget,
    )


def _before(mw: IterationGateMiddleware):
    return mw.before_model(None, None)  # state/runtime unused by this hook


def _drive(mw: IterationGateMiddleware, n: int) -> list:
    """Call before_model n times; return the list of returns (index i = count i+1)."""
    return [_before(mw) for _ in range(n)]


def test_budget_zero_disables():
    ctx = _ctx(budget=0)
    token = set_current_turn(ctx)
    try:
        assert _before(IterationGateMiddleware()) is None
        assert ctx.iteration_count == 0  # not even counted when disabled
    finally:
        reset_current_turn(token)


def test_no_active_turn_returns_none():
    assert IterationGateMiddleware().before_model(None, None) is None


def test_below_75_just_counts():
    ctx = _ctx(budget=20)
    token = set_current_turn(ctx)
    try:
        rets = _drive(IterationGateMiddleware(), 14)  # counts 1..14, < 15
        assert all(r is None for r in rets)
        assert ctx.iteration_count == 14
    finally:
        reset_current_turn(token)


def test_three_tiers_fire_at_75_90_100_once_each():
    ctx = _ctx(budget=20)
    token = set_current_turn(ctx)
    try:
        rets = _drive(IterationGateMiddleware(), 20)  # counts 1..20
        # 75% nudge at count 15 — HumanMessage, NOT a jump.
        r75 = rets[14]
        assert isinstance(r75, dict) and "jump_to" not in r75
        assert isinstance(r75["messages"][0], HumanMessage)
        assert "75%" in r75["messages"][0].content
        # 90% nudge at count 18 — HumanMessage, NOT a jump.
        r90 = rets[17]
        assert isinstance(r90, dict) and "jump_to" not in r90
        assert isinstance(r90["messages"][0], HumanMessage)
        assert "Last warning" in r90["messages"][0].content
        # 100% hard stop at count 20 — jump_to end + AIMessage + flag.
        r100 = rets[19]
        assert r100["jump_to"] == "end"
        assert isinstance(r100["messages"][0], AIMessage)
        assert ctx.iteration_hard_stopped is True
        # All other calls (between/after tiers) are no-ops — each tier once.
        others = [r for i, r in enumerate(rets) if i not in (14, 17, 19)]
        assert all(r is None for r in others)
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
async def test_events_only_at_90_and_100(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="t-iter")
    ctx = _ctx(budget=20)
    token = set_current_turn(ctx)
    try:
        _drive(IterationGateMiddleware(), 20)
        await asyncio.sleep(0.1)  # let fire-and-forget log tasks flush
    finally:
        reset_current_turn(token)
    text = (tmp_path / "logs" / "events.jsonl").read_text()
    assert text.count("iteration_budget_warning") == 1   # the 90% tier
    assert text.count("iteration_budget_reached") == 1   # the 100% tier
    # exactly those two iteration_budget events — the 75% tier emits none.
    assert text.count("iteration_budget") == 2
