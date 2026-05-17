"""181-N regressions: tool-call budget gate + ``get_turn`` alias.

The SDK build gated tool calls via a ``PreToolUse`` HookMatcher that
checked TurnContext.tool_call_count against the per-turn budget. The
deepagents cutover dropped that — ``Config.tool_call_budget`` existed
but didn't gate anything. Restored as a langchain Tool wrapper that
mutates each registered tool's coroutine/func to add a budget check.

Also restored: ``get_turn`` as a back-compat alias for the renamed
``mimir_get_turn`` so skill docs referencing the pre-rename name
don't silently 404.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.models import TurnContext
from mimir.tools.budget_gate import apply_budget_gate


def _make_ctx(budget: int = 5) -> TurnContext:
    return TurnContext(
        turn_id="t-budget",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        tool_call_budget=budget,
    )


def _make_fake_tool(name: str = "fake_tool"):
    """Build a minimal langchain BaseTool. We avoid pulling in the
    full ``@tool`` decorator so the wrapper logic is exercised on a
    plain coroutine + func surface."""
    from langchain_core.tools import StructuredTool

    call_count = {"sync": 0, "async": 0}

    def _sync(**kwargs: Any) -> str:
        call_count["sync"] += 1
        return f"sync {call_count['sync']}"

    async def _async(**kwargs: Any) -> str:
        call_count["async"] += 1
        return f"async {call_count['async']}"

    tool = StructuredTool.from_function(
        func=_sync, coroutine=_async, name=name, description="fake tool",
    )
    return tool, call_count


# ─── Budget gate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_below_budget_calls_pass_through() -> None:
    tool, counts = _make_fake_tool()
    apply_budget_gate(tool)
    ctx = _make_ctx(budget=5)
    token = set_current_turn(ctx)
    try:
        for _ in range(3):
            await tool.ainvoke({})
    finally:
        reset_current_turn(token)
    assert counts["async"] == 3
    assert ctx.tool_call_count == 3


@pytest.mark.asyncio
async def test_at_budget_refuses_with_denial_message() -> None:
    tool, counts = _make_fake_tool()
    apply_budget_gate(tool)
    ctx = _make_ctx(budget=2)
    token = set_current_turn(ctx)
    try:
        await tool.ainvoke({})  # 1
        await tool.ainvoke({})  # 2 — at budget
        out = await tool.ainvoke({})  # 3 — refused
    finally:
        reset_current_turn(token)
    assert "Tool-call budget exhausted" in out
    assert "2/2 calls used" in out
    assert counts["async"] == 2  # The 3rd call never reached the body.


@pytest.mark.asyncio
async def test_budget_zero_disables_gating() -> None:
    tool, counts = _make_fake_tool()
    apply_budget_gate(tool)
    ctx = _make_ctx(budget=0)
    token = set_current_turn(ctx)
    try:
        for _ in range(20):
            await tool.ainvoke({})
    finally:
        reset_current_turn(token)
    assert counts["async"] == 20


@pytest.mark.asyncio
async def test_no_active_turn_disables_gating() -> None:
    """Tests + bench harnesses invoke tools without a TurnContext.
    The gate must be transparent in that case."""
    tool, counts = _make_fake_tool()
    apply_budget_gate(tool)
    # No set_current_turn — _resolve_budget_state returns None.
    for _ in range(10):
        await tool.ainvoke({})
    assert counts["async"] == 10


@pytest.mark.asyncio
async def test_soft_warning_fires_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At/above the soft threshold (75% of budget), one warning
    event fires per turn — subsequent crossings re-evaluate but
    don't re-emit."""
    captured: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)

    tool, _ = _make_fake_tool()
    apply_budget_gate(tool)
    ctx = _make_ctx(budget=8)  # soft threshold = max(1, 6) = 6
    token = set_current_turn(ctx)
    try:
        # 5 calls — below soft.
        for _ in range(5):
            await tool.ainvoke({})
        # 6th call crosses soft → one warning. Subsequent 7th also
        # ≥ soft but should NOT re-emit (per-turn idempotent).
        await tool.ainvoke({})
        await tool.ainvoke({})
    finally:
        reset_current_turn(token)

    # Give the scheduled tasks a chance to run.
    import asyncio
    await asyncio.sleep(0)

    soft_warns = [kw for k, kw in captured if k == "tool_call_budget_soft_warning"]
    assert len(soft_warns) == 1
    assert soft_warns[0]["soft_threshold"] == 6


def test_apply_budget_gate_is_idempotent() -> None:
    tool, _ = _make_fake_tool()
    apply_budget_gate(tool)
    first_coro = tool.coroutine
    apply_budget_gate(tool)  # second wrap should be a no-op
    assert tool.coroutine is first_coro
    assert getattr(tool, "_mimir_budget_wrapped", False) is True


@pytest.mark.asyncio
async def test_sync_tool_path_also_gates() -> None:
    """A sync tool (no coroutine) still gets the gate on its func.
    Important because mimir_get_turn is sync."""
    from langchain_core.tools import StructuredTool

    counts = {"n": 0}

    def _sync(**kwargs: Any) -> str:
        counts["n"] += 1
        return "ran"

    tool = StructuredTool.from_function(
        func=_sync, name="sync_only", description="",
    )
    apply_budget_gate(tool)
    ctx = _make_ctx(budget=1)
    token = set_current_turn(ctx)
    try:
        await tool.ainvoke({})  # 1 — passes
        out = await tool.ainvoke({})  # 2 — refused
    finally:
        reset_current_turn(token)
    assert "Tool-call budget exhausted" in out
    assert counts["n"] == 1


# ─── get_turn alias ───────────────────────────────────────────────


def test_get_turn_alias_is_a_distinct_tool() -> None:
    """The deepagents agent surface must expose both names so skill
    prompts referencing the pre-rename ``get_turn`` keep working."""
    from mimir.tools.extra import get_turn, mimir_get_turn

    assert get_turn.name == "get_turn"
    assert mimir_get_turn.name == "mimir_get_turn"


def test_get_turn_alias_returns_same_record(tmp_path) -> None:
    """The alias is wired to the same underlying turns.jsonl reader,
    so identical turn_id queries produce identical responses."""
    from mimir.tools.extra import get_turn, mimir_get_turn, set_turns_log_path
    import json

    log_path = tmp_path / "turns.jsonl"
    log_path.write_text(json.dumps({
        "turn_id": "abc123",
        "session_id": "ch-1",
        "trigger": "user_message",
        "output": "hello",
        "input": "stripped",
    }) + "\n")
    set_turns_log_path(log_path)

    out_canonical = mimir_get_turn.invoke({"turn_id": "abc123"})
    out_alias = get_turn.invoke({"turn_id": "abc123"})
    assert out_canonical == out_alias
    parsed = json.loads(out_canonical)
    assert parsed["turn_id"] == "abc123"
    # ``input`` was stripped per the contract.
    assert "input" not in parsed


def test_all_mimir_tools_includes_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
    names = {t.name for t in all_mimir_tools()}
    assert "mimir_get_turn" in names
    assert "get_turn" in names
