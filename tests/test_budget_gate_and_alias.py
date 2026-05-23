"""Tool-call budget gate (middleware) + ``get_turn`` alias.

Budget enforcement is now a langchain ``AgentMiddleware``
(``mimir.tools.budget_gate.BudgetGateMiddleware``) wired into
deepagents via ``create_deep_agent(middleware=...)``. The middleware
intercepts every ``wrap_tool_call`` / ``awrap_tool_call`` invocation —
BOTH mimir-registered tools and deepagents' built-ins (``shell_exec``,
``read_file``, etc.). Pre-2026-05-23 we wrapped each registered tool's
coroutine/func individually and missed the built-ins; production
heartbeats blew past a 120 budget with zero denial events.

These tests exercise the middleware via two surfaces:

1. The internal ``_check_and_increment_or_deny`` helper (lower-cost,
   directly mutates ``TurnContext.tool_call_count`` so we can verify
   the bookkeeping without standing up a langgraph agent).
2. The ``BudgetGateMiddleware.wrap_tool_call`` / ``awrap_tool_call``
   methods (the integration surface — verifies the ToolMessage
   return shape and that the handler is bypassed at the cap).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from mimir._context import reset_current_turn, set_current_turn
from mimir.models import TurnContext
from mimir.tools.budget_gate import (
    BudgetGateMiddleware,
    _check_and_increment_or_deny,
)


def _make_ctx(budget: int = 5) -> TurnContext:
    return TurnContext(
        turn_id="t-budget",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        tool_call_budget=budget,
    )


def _make_request(tool_name: str = "fake_tool",
                  tool_call_id: str = "tc-1") -> ToolCallRequest:
    """Minimal ToolCallRequest for middleware tests. ``state`` /
    ``runtime`` aren't read by the budget middleware so we pass
    ``None`` for both — keeps the test surface small."""
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {}, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


# ─── Bookkeeping helper ───────────────────────────────────────────


def test_below_budget_increments_and_returns_none():
    ctx = _make_ctx(budget=5)
    token = set_current_turn(ctx)
    try:
        for _ in range(3):
            assert _check_and_increment_or_deny("fake_tool") is None
    finally:
        reset_current_turn(token)
    assert ctx.tool_call_count == 3


def test_at_budget_returns_denial_message():
    ctx = _make_ctx(budget=2)
    token = set_current_turn(ctx)
    try:
        assert _check_and_increment_or_deny("fake_tool") is None  # 1
        assert _check_and_increment_or_deny("fake_tool") is None  # 2
        out = _check_and_increment_or_deny("fake_tool")  # 3 — refused
    finally:
        reset_current_turn(token)
    assert out is not None
    assert "Tool-call budget exhausted" in out
    assert "2/2 calls used" in out
    assert "fake_tool" in out
    # Count must NOT advance past the cap (refused calls don't bump).
    assert ctx.tool_call_count == 2


def test_budget_zero_disables_gating():
    ctx = _make_ctx(budget=0)
    token = set_current_turn(ctx)
    try:
        for _ in range(20):
            assert _check_and_increment_or_deny("fake_tool") is None
    finally:
        reset_current_turn(token)
    # No enforcement → count stays at 0 (helper exits early on
    # budget=0 before incrementing).
    assert ctx.tool_call_count == 0


def test_no_active_turn_disables_gating():
    """Tests + bench harnesses invoke tools without a TurnContext.
    The gate must be transparent in that case."""
    # No set_current_turn — _resolve_budget_state returns None.
    for _ in range(10):
        assert _check_and_increment_or_deny("fake_tool") is None


@pytest.mark.asyncio
async def test_soft_warning_fires_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At/above the soft threshold (75% of budget), one warning event
    fires per turn — subsequent crossings re-evaluate but don't re-emit."""
    captured: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)

    ctx = _make_ctx(budget=8)  # soft threshold = max(1, 6) = 6
    token = set_current_turn(ctx)
    try:
        # 5 calls — below soft.
        for _ in range(5):
            _check_and_increment_or_deny("fake_tool")
        # 6th call crosses soft → one warning. Subsequent 7th also
        # ≥ soft but should NOT re-emit (per-turn idempotent).
        _check_and_increment_or_deny("fake_tool")
        _check_and_increment_or_deny("fake_tool")
    finally:
        reset_current_turn(token)

    # Yield so the fire-and-forget log_event tasks land.
    import asyncio
    await asyncio.sleep(0)

    soft_warns = [kw for k, kw in captured if k == "tool_call_budget_soft_warning"]
    assert len(soft_warns) == 1
    assert soft_warns[0]["soft_threshold"] == 6


# ─── Middleware surface ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_middleware_awrap_passes_through_under_budget():
    """Below the cap, ``awrap_tool_call`` delegates to the handler
    unchanged."""
    mw = BudgetGateMiddleware()
    handler_calls: list[ToolCallRequest] = []

    async def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=5)
    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("t1", "id-1"), handler)
    finally:
        reset_current_turn(token)
    assert isinstance(out, ToolMessage)
    assert out.content == "ok"
    assert len(handler_calls) == 1
    assert ctx.tool_call_count == 1


@pytest.mark.asyncio
async def test_middleware_awrap_refuses_at_cap():
    """At the cap, the handler is NOT called and the middleware
    returns a denial ToolMessage with status='error'."""
    mw = BudgetGateMiddleware()
    handler_calls: list[ToolCallRequest] = []

    async def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=2)
    token = set_current_turn(ctx)
    try:
        await mw.awrap_tool_call(_make_request("t1", "id-1"), handler)  # 1
        await mw.awrap_tool_call(_make_request("t1", "id-2"), handler)  # 2
        out = await mw.awrap_tool_call(_make_request("t1", "id-3"), handler)  # refused
    finally:
        reset_current_turn(token)
    assert isinstance(out, ToolMessage)
    assert "Tool-call budget exhausted" in str(out.content)
    assert out.status == "error"
    assert out.tool_call_id == "id-3"
    assert len(handler_calls) == 2  # Third never ran.


def test_middleware_sync_wrap_refuses_at_cap():
    """The sync ``wrap_tool_call`` path mirrors the async one."""
    mw = BudgetGateMiddleware()
    handler_calls: list[ToolCallRequest] = []

    def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=1)
    token = set_current_turn(ctx)
    try:
        mw.wrap_tool_call(_make_request("t1", "id-1"), handler)  # passes
        out = mw.wrap_tool_call(_make_request("t1", "id-2"), handler)  # refused
    finally:
        reset_current_turn(token)
    assert isinstance(out, ToolMessage)
    assert "Tool-call budget exhausted" in str(out.content)
    assert len(handler_calls) == 1


@pytest.mark.asyncio
async def test_send_message_and_react_bypass_the_cap():
    """``send_message`` is the only delivery path for the agent's reply
    (final assistant text doesn't auto-deliver to channels). If the cap
    refuses send_message too, the agent hits the budget and has no way
    to tell the operator anything. Exempting it — AND skipping the
    count increment — keeps that channel open. ``react`` follows the
    same operator-facing-acknowledgement logic."""
    mw = BudgetGateMiddleware()
    handler_calls: list[str] = []

    async def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req.tool_call["name"])
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=2)
    token = set_current_turn(ctx)
    try:
        # Burn the budget with non-exempt calls.
        await mw.awrap_tool_call(_make_request("shell_exec", "id-1"), handler)
        await mw.awrap_tool_call(_make_request("shell_exec", "id-2"), handler)
        # Past the cap: a regular tool is refused...
        denied = await mw.awrap_tool_call(_make_request("shell_exec", "id-3"), handler)
        assert isinstance(denied, ToolMessage)
        assert "Tool-call budget exhausted" in str(denied.content)
        # ...but send_message and react MUST still pass through.
        sm = await mw.awrap_tool_call(_make_request("send_message", "id-4"), handler)
        rx = await mw.awrap_tool_call(_make_request("react", "id-5"), handler)
    finally:
        reset_current_turn(token)
    assert sm.content == "ok"
    assert rx.content == "ok"
    assert handler_calls == ["shell_exec", "shell_exec", "send_message", "react"]
    # Exempt tools must NOT bump the count (otherwise heavy send_message
    # use would still tick toward... nothing useful, but for clarity
    # the spec is "free passage").
    assert ctx.tool_call_count == 2


def test_denial_message_mentions_exempt_tools():
    """The model needs to know what it CAN still do when the cap hits.
    The denial text names ``send_message`` and ``react`` so it doesn't
    waste turns retrying gated tools."""
    ctx = _make_ctx(budget=1)
    token = set_current_turn(ctx)
    try:
        _check_and_increment_or_deny("shell_exec")  # 1, passes
        out = _check_and_increment_or_deny("shell_exec")  # refused
    finally:
        reset_current_turn(token)
    assert out is not None
    assert "send_message" in out
    assert "react" in out


@pytest.mark.asyncio
async def test_middleware_catches_unregistered_tools():
    """The deepagents built-ins (``shell_exec``, ``read_file``, etc.)
    arrive at the middleware as ToolCallRequests whose ``tool`` may
    be set OR None depending on registration. Either way the budget
    check fires on the ``tool_call.name`` — which is the gap that
    motivated this rewrite."""
    mw = BudgetGateMiddleware()
    handler_invocations = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_invocations
        handler_invocations += 1
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=1)
    token = set_current_turn(ctx)
    try:
        # First call: deepagents built-in shell_exec — ``tool`` would
        # be the deepagents-supplied tool. Passes the cap.
        req1 = _make_request("shell_exec", "id-a")
        await mw.awrap_tool_call(req1, handler)
        # Second call: at the cap. Same shape, refused.
        req2 = _make_request("shell_exec", "id-b")
        out = await mw.awrap_tool_call(req2, handler)
    finally:
        reset_current_turn(token)
    assert isinstance(out, ToolMessage)
    assert "shell_exec" in str(out.content)
    assert handler_invocations == 1


# ─── get_turn alias (unchanged from prior file) ───────────────────


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
    assert "input" not in parsed


def test_all_mimir_tools_includes_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
    names = {t.name for t in all_mimir_tools()}
    assert "mimir_get_turn" in names
    assert "get_turn" in names
