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

import contextvars
import time
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from mimir._context import get_current_turn, reset_current_turn, set_current_turn
from mimir.models import AuthContext, InformationFlowLabels, TurnContext
from mimir.identities import IdentityResolver
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


def _make_request(
    tool_name: str = "fake_tool",
    tool_call_id: str = "tc-1",
    auth_context: AuthContext | None = None,
) -> ToolCallRequest:
    """Build a request with the exact frozen carrier LangGraph supplies."""
    if auth_context is None:
        turn = get_current_turn()
        auth_context = getattr(turn, "auth_context", None) if turn is not None else None
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {}, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state=None,
        runtime=Runtime(context=auth_context),
    )


def _ifc_labels(
    channel: str = "ch-1",
    *,
    sources: frozenset[str] | None = None,
) -> InformationFlowLabels:
    return InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=sources if sources is not None else frozenset({channel}),
    )


def _attach_auth(ctx: TurnContext, resolver: IdentityResolver | None = None) -> None:
    roles = ()
    canonical = ctx.author
    if resolver is not None and ctx.author is not None:
        roles = resolver.access_metadata(ctx.author).roles
        canonical = resolver.resolve(ctx.author)
    ctx.auth_context = AuthContext(
        principal=ctx.author,
        canonical_principal=canonical,
        roles=roles,
        event_ingress=ctx.event_ingress,
        trigger=ctx.trigger,
        channel_id=ctx.channel_id,
        interactivity=None,
        enforcement_enabled=ctx.access_control_enforced,
    )


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


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


def test_small_budget_denial_marks_context():
    ctx = _make_ctx(budget=1)
    token = set_current_turn(ctx)
    try:
        assert _check_and_increment_or_deny("first_tool") is None  # 1
        first_denial = _check_and_increment_or_deny("second_tool")
        second_denial = _check_and_increment_or_deny("third_tool")
        # Exempt tools stay available after exhaustion and must not mutate
        # the hard-denial markers.
        assert _check_and_increment_or_deny("send_message") is None
        assert _check_and_increment_or_deny("react") is None
    finally:
        reset_current_turn(token)

    assert first_denial is not None
    assert second_denial is not None
    assert ctx.tool_call_count == 1
    assert ctx.tool_call_budget_exhausted is True
    assert ctx.tool_call_budget_denied_count == 2
    assert ctx.tool_call_budget_denied_tools == ["second_tool", "third_tool"]
    assert ctx.tool_call_budget_first_denied_at_count == 1


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
async def test_forked_task_uses_auth_context_ifc_labels_when_contextvar_is_unset():
    """The frozen request carrier keeps the sink gate active across task forks."""
    mw = BudgetGateMiddleware()
    auth = AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=(),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=_ifc_labels(),
    )
    handler_calls: list[ToolCallRequest] = []

    async def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    # No set_current_turn(): this mirrors a forked SDK/MCP task whose ContextVar
    # did not propagate. Authorization must recover labels from AuthContext.
    out = await mw.awrap_tool_call(
        _make_request("send_message", "ifc-carrier", auth), handler,
    )

    assert out.content == "ok"
    assert len(handler_calls) == 1


@pytest.mark.asyncio
async def test_forked_task_blocks_incompatible_egress_from_auth_context_labels():
    mw = BudgetGateMiddleware()
    auth = AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=(),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=_ifc_labels(sources=frozenset({"ch-private"})),
    )
    handler_calls: list[ToolCallRequest] = []

    async def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(
        _make_request("send_message", "ifc-block", auth), handler,
    )

    assert out.status == "error"
    assert "ifc_label_blocked:same_channel" in str(out.content)
    assert handler_calls == []


@pytest.mark.asyncio
async def test_real_turn_without_ifc_labels_fails_closed_under_enforcement():
    mw = BudgetGateMiddleware()
    auth = AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=(),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=None,
    )
    handler_calls: list[ToolCallRequest] = []

    async def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(
        _make_request("send_message", "ifc-missing", auth), handler,
    )

    assert out.status == "error"
    assert "missing_ifc_labels" in str(out.content)
    assert handler_calls == []


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


def test_middleware_sync_wrap_passes_through_under_budget():
    """Symmetric to the async pass-through case — verifies the sync
    ``wrap_tool_call`` delegates to the handler when below the cap."""
    mw = BudgetGateMiddleware()
    handler_calls: list[ToolCallRequest] = []

    def handler(req: ToolCallRequest) -> ToolMessage:
        handler_calls.append(req)
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=5)
    token = set_current_turn(ctx)
    try:
        out = mw.wrap_tool_call(_make_request("t1", "id-1"), handler)
    finally:
        reset_current_turn(token)
    assert isinstance(out, ToolMessage)
    assert out.content == "ok"
    assert len(handler_calls) == 1
    assert ctx.tool_call_count == 1


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
async def test_admin_sensitive_tool_denied_for_non_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("worklink_run", "id-admin"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0
    kinds = [kind for kind, _kw in captured]
    assert "admin_tool_call_denied" in kinds
    assert "tool_call_denied" in kinds
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["tool"] == "worklink_run"
    assert admin_event["canonical_author"] == "alice"
    assert admin_event["denial_reason"] == "admin_required"


@pytest.mark.asyncio
async def test_http_event_ingress_denies_admin_tool_even_when_trigger_source_forged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: root
            aliases: [api-root]
            access: {roles: [user, admin]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.trigger = "scheduled_tick"
    ctx.channel_source = "api"
    ctx.event_ingress = "http_event"
    ctx.author = "api-root"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("shell_exec", "id-http-forged"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity (http_event_author_untrusted)" in str(out.content)
    assert handler_calls == 0
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["tool"] == "shell_exec"
    assert admin_event["canonical_author"] == "root"
    assert admin_event["denial_reason"] == "http_event_author_untrusted"
    assert admin_event["enforcement_enabled"] is True


@pytest.mark.asyncio
async def test_http_event_ingress_denies_admin_tool_when_access_control_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: root
            aliases: [api-root]
            access: {roles: [user, admin]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.trigger = "scheduled_tick"
    ctx.channel_source = "api"
    ctx.event_ingress = "http_event"
    ctx.author = "api-root"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = False
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("shell_exec", "id-http-open"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity (http_event_author_untrusted)" in str(out.content)
    assert handler_calls == 0
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["tool"] == "shell_exec"
    assert admin_event["canonical_author"] == "root"
    assert admin_event["denial_reason"] == "http_event_author_untrusted"
    assert admin_event["enforcement_enabled"] is False


@pytest.mark.asyncio
async def test_admin_gate_denies_unadmitted_operation_for_unstamped_scheduler_turn() -> None:
    ctx = _make_ctx(budget=0)
    ctx.trigger = "scheduled_tick"
    ctx.channel_source = "api"
    ctx.author = None
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(
            _make_request("request_mimir_update", "id-api-internal"), handler
        )
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_admin_gate_uses_request_carrier_when_contextvar_missing(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    turn_context = contextvars.Context()
    token = turn_context.run(set_current_turn, ctx)
    try:
        out = await mw.awrap_tool_call(
            _make_request("shell_exec", "id-active", ctx.auth_context), handler
        )
    finally:
        turn_context.run(reset_current_turn, token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_admin_sensitive_tool_allowed_via_canonical_discord_alias(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: root
            aliases: [slack-UADMIN, discord-42]
            access: {roles: [user, admin]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "discord-42"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("reload_pollers", "id-admin"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.content == "ok"
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_read_only_tool_remains_available_to_non_admin(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="listed", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("list_schedules", "id-read"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.content == "listed"
    assert handler_calls == 1


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


@pytest.mark.asyncio
async def test_admin_gate_denies_unadmitted_operation_for_authorless_scheduler_turn() -> None:
    ctx = _make_ctx(budget=0)
    ctx.trigger = "scheduled_tick"
    ctx.author = None
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(
            _make_request("request_mimir_update", "id-system"), handler
        )
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_admin_gate_denies_web_source_without_admin_role() -> None:
    ctx = _make_ctx(budget=0)
    ctx.channel_source = "web"
    ctx.author = None
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("shell_exec", "id-web"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_admin_gate_denies_write_file_for_non_admin(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, locals().get("resolver"))
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="wrote", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("write_file", "id-write"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_admin_gate_denies_sensitive_tool_when_enforced_context_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    captured: list[tuple[str, dict[str, Any]]] = []

    def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(_make_request("shell_exec", "id-missing-ctx"), handler)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity (missing_auth_context)" in str(out.content)
    assert handler_calls == 0
    assert ("admin_tool_call_denied", {
        "tool": "shell_exec",
        "allowed": False,
        "status": "denied",
        "required_tier": "admin",
        "denial_reason": "missing_auth_context",
        "author": None,
        "canonical_author": None,
        "roles": [],
        "enforcement_enabled": True,
    }) in captured


@pytest.mark.asyncio
async def test_admin_gate_missing_context_still_allows_non_admin_tools_when_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="listed", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(_make_request("list_schedules", "id-read"), handler)

    assert isinstance(out, ToolMessage)
    assert out.content == "listed"
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_admin_gate_missing_context_allows_sensitive_tool_when_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_ACCESS_CONTROL_ENFORCED", raising=False)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(_make_request("shell_exec", "id-no-enforce"), handler)

    assert isinstance(out, ToolMessage)
    assert out.content == "ran"
    assert handler_calls == 1


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
    # ``input`` is stripped per the get_turn contract — the alias
    # must preserve that.
    assert "input" not in parsed


def test_all_mimir_tools_includes_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
    names = {t.name for t in all_mimir_tools()}
    assert "mimir_get_turn" in names
    assert "get_turn" in names


# ─── chainlink #118: asyncio strong-ref for fire-and-forget tasks ────────────


@pytest.mark.asyncio
async def test_emit_event_sync_task_held_in_background_tasks() -> None:
    """_emit_event_sync holds the spawned log_event task in _background_tasks
    until completion (chainlink #118).  Regression: bare loop.create_task()
    without a retained reference can be GC'd before completion."""
    import asyncio
    from unittest.mock import patch

    from mimir.tools.budget_gate import _background_tasks, _emit_event_sync

    logged: list[str] = []
    unblocked = asyncio.Event()

    async def blocking_log_event(kind: str, **kwargs: Any) -> None:
        logged.append(kind)
        await unblocked.wait()

    # _emit_event_sync uses a lazy import, so patch the source module
    # (mimir.event_logger) so the lazy ``from ..event_logger import log_event``
    # picks up the replacement at call time.
    with patch("mimir.event_logger.log_event", new=blocking_log_event):
        _emit_event_sync("tool_call_budget_denied", tool="bash", count=5)
        await asyncio.sleep(0)

        # Task is in flight: strong ref must be held.
        assert len(_background_tasks) == 1, (
            f"Expected 1 in-flight task, got {len(_background_tasks)}"
        )

        unblocked.set()
        # Two yields: first lets the task run to completion; second lets
        # the loop.call_soon-scheduled done_callback (discard) execute.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # After completion the done-callback must have removed the entry.
    assert len(_background_tasks) == 0, (
        "_background_tasks should be empty after task completes"
    )


@pytest.mark.asyncio
async def test_middleware_emits_tool_call_events_for_success_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every middleware-observed tool call emits a per-tool event; error
    ToolMessages additionally emit tool_error so failure rate is computable."""
    captured: list[tuple[str, dict[str, Any]]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)

    mw = BudgetGateMiddleware()

    async def ok_handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="ok",
            tool_call_id=req.tool_call["id"],
            name=req.tool_call["name"],
        )

    async def err_handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="boom",
            tool_call_id=req.tool_call["id"],
            name=req.tool_call["name"],
            status="error",
        )

    ctx = _make_ctx(budget=5)
    token = set_current_turn(ctx)
    try:
        await mw.awrap_tool_call(_make_request("memory_query", "id-ok"), ok_handler)
        await mw.awrap_tool_call(_make_request("memory_query", "id-err"), err_handler)
    finally:
        reset_current_turn(token)

    import asyncio
    await asyncio.sleep(0)

    tool_calls = [kw for kind, kw in captured if kind == "tool_call"]
    tool_errors = [kw for kind, kw in captured if kind == "tool_error"]
    assert [kw["ok"] for kw in tool_calls] == [True, False]
    assert all(kw["tool"] == "memory_query" for kw in tool_calls)
    assert len(tool_errors) == 1
    assert tool_errors[0]["tool"] == "memory_query"
    assert tool_errors[0]["paired_tool_call"] is True
    assert "boom" in tool_errors[0]["error"]


@pytest.mark.asyncio
async def test_middleware_emits_tool_error_for_budget_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)

    mw = BudgetGateMiddleware()

    async def handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    ctx = _make_ctx(budget=1)
    token = set_current_turn(ctx)
    try:
        await mw.awrap_tool_call(_make_request("shell_exec", "id-1"), handler)
        await mw.awrap_tool_call(_make_request("shell_exec", "id-2"), handler)
    finally:
        reset_current_turn(token)

    import asyncio
    await asyncio.sleep(0)

    denied_errors = [kw for kind, kw in captured if kind == "tool_error" and kw.get("denied")]
    assert len(denied_errors) == 1
    assert denied_errors[0]["tool"] == "shell_exec"
    assert denied_errors[0]["paired_tool_call"] is True


def test_admin_sensitive_tool_matches_mcp_name_variants():
    from mimir.tools.budget_gate import _is_admin_sensitive_tool

    assert _is_admin_sensitive_tool("mcp__mimir__shell_exec")
    assert _is_admin_sensitive_tool("mcp_mimir_shell_exec")
    assert _is_admin_sensitive_tool("mcp__mimir__worklink_run")
    assert _is_admin_sensitive_tool("mcp_mimir_worklink_run")
    assert _is_admin_sensitive_tool("mcp_mimir_read_file")
    assert _is_admin_sensitive_tool("mcp__mimir__read_file")
    assert _is_admin_sensitive_tool("mcp_mimir_glob")
    assert _is_admin_sensitive_tool("mcp_mimir_grep")
    assert _is_admin_sensitive_tool("mcp_mimir_file_search")
