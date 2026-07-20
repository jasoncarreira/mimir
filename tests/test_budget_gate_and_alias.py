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

import asyncio
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
from mimir.models import AuthContext, InformationFlowLabels, SourceLabel, TurnContext
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
    args: dict[str, Any] | None = None,
) -> ToolCallRequest:
    """Build a request with the exact frozen carrier LangGraph supplies."""
    if auth_context is None:
        turn = get_current_turn()
        auth_context = getattr(turn, "auth_context", None) if turn is not None else None
    return ToolCallRequest(
        tool_call={
            "name": tool_name,
            "args": args or {},
            "id": tool_call_id,
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=Runtime(context=auth_context),
    )


def test_private_admin_can_approve_only_one_exact_file_sink_through_middleware(
    tmp_path: Path,
) -> None:
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"ch-1"}),
        sources=frozenset({SourceLabel(
            principal="user-1",
            domain="channel",
            resource_id="ch-1",
            bridge_instance="test",
            sensitivity="private",
            authorized_principals=frozenset({"user-1"}),
        )}),
    )
    auth = AuthContext(
        principal="test-U1",
        canonical_principal="user-1",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=labels,
        domain="channel",
        resource_id="ch-1",
        bridge_instance="test",
    )
    middleware = BudgetGateMiddleware()
    approved_path = str(tmp_path / "approved.txt")
    other_path = str(tmp_path / "other.txt")
    executions: list[str] = []

    def handler(request: ToolCallRequest) -> ToolMessage:
        executions.append(str(request.tool_call["args"]["file_path"]))
        return ToolMessage(
            content="written",
            tool_call_id=str(request.tool_call["id"]),
            name="write_file",
        )

    denied_before = middleware.wrap_tool_call(
        _make_request("write_file", auth_context=auth, args={"file_path": approved_path}),
        handler,
    )
    init_logger(tmp_path / "events.jsonl", session_id="ifc-middleware-test")
    try:
        approval = middleware.wrap_tool_call(
            _make_request(
                "approve_declassification",
                tool_call_id="approval-1",
                auth_context=auth,
                args={
                    "sink_category": "file",
                    "destination": approved_path,
                    "reason": "write this exact output",
                },
            ),
            lambda _request: pytest.fail("approval handler must not receive authority"),
        )
        written = middleware.wrap_tool_call(
            _make_request("write_file", auth_context=auth, args={"file_path": approved_path}),
            handler,
        )
        denied_other = middleware.wrap_tool_call(
            _make_request("write_file", auth_context=auth, args={"file_path": other_path}),
            handler,
        )
    finally:
        _reset_logger_for_tests()

    assert denied_before.status == "error"
    assert approval.status == "success"
    assert written.status != "error"
    assert denied_other.status == "error"
    assert executions == [approved_path]


@pytest.mark.asyncio
async def test_mcp_resource_adapter_runs_before_remote_handler() -> None:
    from dataclasses import replace

    from mimir.mcp_client import (
        MCPAdapterConfig,
        MCPProvenance,
        MCPServerConfig,
        _bridge_mcp_tool,
        clear_mcp_adapter_registry,
        register_configured_mcp_adapters,
    )

    config = MCPServerConfig(
        name="github",
        command="x",
        args=[],
        server_config_id="github-production",
        policy_version="policy-v1",
        adapters=(MCPAdapterConfig(
            name="github-owner",
            version="adapter-v1",
            policy_version="policy-v1",
            resource_argument="repository",
            owner_argument="owner",
            source=True,
        ),),
    )
    provenance = replace(
        MCPProvenance.create(
            config,
            "get_repository",
            {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repository": {"type": "string"},
                },
                "required": ["owner", "repository"],
            },
            server_config_id=config.server_config_id,
        ),
        classification="resource_scoped",
        adapter_name="github-owner",
        adapter_version="adapter-v1",
        approval_version="approval-v1",
        policy_version="policy-v1",
    )
    tool = _bridge_mcp_tool(
        server_name="github",
        tool_name="get_repository",
        description="",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repository": {"type": "string"},
            },
            "required": ["owner", "repository"],
        },
        session=object(),
        provenance=provenance,
    )
    context = AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=("user",),
        event_ingress="bridge",
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
    )
    handler_calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    def request(call_id: str, arguments: dict[str, Any]) -> ToolCallRequest:
        return ToolCallRequest(
            tool_call={
                "name": tool.name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            },
            tool=tool,
            state=None,
            runtime=Runtime(context=context),
        )

    clear_mcp_adapter_registry()
    register_configured_mcp_adapters([config])
    middleware = BudgetGateMiddleware()
    turn = _make_ctx()
    turn.auth_context = context
    turn.ifc_labels = InformationFlowLabels()
    token = set_current_turn(turn)
    try:
        valid = await middleware.awrap_tool_call(
            request("valid", {"owner": "alice", "repository": "repo-1"}),
            handler,
        )
        wrong_owner = await middleware.awrap_tool_call(
            request("wrong", {"owner": "bob", "repository": "repo-1"}),
            handler,
        )
        malformed = await middleware.awrap_tool_call(
            request("malformed", {"owner": "alice", "repository": ["repo-1"]}),
            handler,
        )
    finally:
        reset_current_turn(token)
        clear_mcp_adapter_registry()

    assert valid.content == "ok"
    assert wrong_owner.status == "error"
    assert "mcp_wrong_owner" in str(wrong_owner.content)
    assert malformed.status == "error"
    assert "mcp_malformed_arguments" in str(malformed.content)
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_mcp_resource_adapter_still_enforces_external_sink_ifc() -> None:
    from dataclasses import replace

    from mimir.mcp_client import (
        MCPAdapterConfig,
        MCPProvenance,
        MCPServerConfig,
        _bridge_mcp_tool,
        clear_mcp_adapter_registry,
        register_configured_mcp_adapters,
    )

    config = MCPServerConfig(
        name="github",
        command="x",
        args=[],
        server_config_id="github-production",
        policy_version="policy-v1",
        adapters=(MCPAdapterConfig(
            name="github-owner",
            version="adapter-v1",
            policy_version="policy-v1",
            resource_argument="repository",
            owner_argument="owner",
            source=True,
            sink=True,
        ),),
    )
    input_schema = {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repository": {"type": "string"},
        },
        "required": ["owner", "repository"],
    }
    provenance = replace(
        MCPProvenance.create(
            config,
            "update_repository",
            input_schema,
            server_config_id=config.server_config_id,
        ),
        classification="resource_scoped",
        adapter_name="github-owner",
        adapter_version="adapter-v1",
        approval_version="approval-v1",
        policy_version="policy-v1",
    )
    tool = _bridge_mcp_tool(
        server_name="github",
        tool_name="update_repository",
        description="",
        input_schema=input_schema,
        session=object(),
        provenance=provenance,
    )
    context = AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=("user",),
        event_ingress="bridge",
        trigger="user_message",
        channel_id="untrusted-channel",
        interactivity=None,
        enforcement_enabled=True,
    )
    turn = _make_ctx()
    turn.auth_context = context
    turn.ifc_labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"untrusted-channel"}),
    )
    handler_calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    request = ToolCallRequest(
        tool_call={
            "name": tool.name,
            "args": {"owner": "alice", "repository": "repo-1"},
            "id": "tainted",
            "type": "tool_call",
        },
        tool=tool,
        state=None,
        runtime=Runtime(context=context),
    )

    clear_mcp_adapter_registry()
    register_configured_mcp_adapters([config])
    token = set_current_turn(turn)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(request, handler)
    finally:
        reset_current_turn(token)
        clear_mcp_adapter_registry()

    assert result.status == "error"
    assert "ifc_label_blocked:external_mcp" in str(result.content)
    assert handler_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument_egress", "expected_allowed"),
    [("allowed", True), ("taint_gated", False)],
)
async def test_mcp_argument_posture_controls_queries_after_active_ingest(
    argument_egress: str, expected_allowed: bool,
) -> None:
    from dataclasses import replace

    from mimir.access_control import OperationDecision
    from mimir.mcp_client import (
        MCPAuthorizationResult,
        MCPProvenance,
        MCPServerConfig,
        _bridge_mcp_tool,
        clear_mcp_adapter_registry,
        register_mcp_adapter,
    )

    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "argument_egress": {"type": "string"},
        },
        "required": ["query"],
    }
    config = MCPServerConfig(name="search", command="x", args=[])
    provenance = replace(
        MCPProvenance.create(config, "query", schema),
        classification="open",
        adapter_name="search-policy",
        adapter_version="adapter-v1",
        approval_version="approval-v1",
        policy_version="policy-v1",
        result_integrity="untrusted",
        argument_egress=argument_egress,
    )
    tool = _bridge_mcp_tool(
        server_name="search",
        tool_name="query",
        description="",
        input_schema=schema,
        session=object(),
        provenance=provenance,
    )
    context = AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=("user",),
        event_ingress="bridge",
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
    )
    untrusted = SourceLabel(
        principal="alice",
        domain="tool",
        resource_id="untrusted-page",
        bridge_instance="web",
        sensitivity="internal",
        authorized_principals=frozenset({"alice"}),
        source_kind="protected_tool",
        integrity="untrusted",
        integrity_effect="active_ingest",
    )
    tainted = InformationFlowLabels().with_source(untrusted)
    context.ifc_state.merge(tainted)
    turn = _make_ctx()
    turn.auth_context = context
    turn.ifc_labels = tainted
    calls = 0

    def classify(request):  # type: ignore[no-untyped-def]
        assert request.arguments["query"] == "model-composed arbitrary query"
        return MCPAuthorizationResult(
            decision=OperationDecision.OPEN,
            allowed=True,
            sink_resources=("configured-search-service",),
        )

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(content="results", tool_call_id=request.tool_call["id"])

    request = ToolCallRequest(
        tool_call={
            "name": tool.name,
            "args": {
                "query": "model-composed arbitrary query",
                # Model arguments cannot select the policy posture.
                "argument_egress": "allowed",
            },
            "id": "query",
            "type": "tool_call",
        },
        tool=tool,
        state=None,
        runtime=Runtime(context=context),
    )

    clear_mcp_adapter_registry()
    register_mcp_adapter(
        "search-policy", "adapter-v1", "policy-v1", classify,
        flow_direction="sink",
    )
    token = set_current_turn(turn)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(request, handler)
    finally:
        reset_current_turn(token)
        clear_mcp_adapter_registry()

    assert (result.status != "error") is expected_allowed
    assert calls == int(expected_allowed)
    if not expected_allowed:
        assert "ifc_label_blocked:external_mcp" in str(result.content)


@pytest.mark.asyncio
async def test_admin_mcp_sink_cannot_bypass_external_sink_ifc() -> None:
    from dataclasses import replace

    from mimir.access_control import OperationDecision, ToolFlowDirection, ToolRegistry
    from mimir.mcp_client import (
        MCPAuthorizationResult,
        MCPProvenance,
        MCPServerConfig,
        _bridge_mcp_tool,
        clear_mcp_adapter_registry,
        register_mcp_adapter,
    )

    input_schema = {
        "type": "object",
        "properties": {"destination": {"type": "string"}},
        "required": ["destination"],
    }
    config = MCPServerConfig(name="publisher", command="x", args=[])
    provenance = replace(
        MCPProvenance.create(config, "publish", input_schema),
        classification="admin_required",
        adapter_name="publisher-policy",
        adapter_version="adapter-v1",
        approval_version="approval-v1",
        policy_version="policy-v1",
    )
    tool = _bridge_mcp_tool(
        server_name="publisher",
        tool_name="publish",
        description="",
        input_schema=input_schema,
        session=object(),
        provenance=provenance,
    )
    context = AuthContext(
        principal="alice",
        canonical_principal="alice",
        roles=("admin",),
        event_ingress="bridge",
        trigger="user_message",
        channel_id="private-channel",
        interactivity=None,
        enforcement_enabled=True,
    )
    arguments = {"destination": "public-destination"}

    def classify(request):  # type: ignore[no-untyped-def]
        assert request.arguments == arguments
        return MCPAuthorizationResult(
            decision=OperationDecision.ADMIN_REQUIRED,
            allowed=True,
            source_resources=("private-source",),
            sink_resources=("public-destination",),
        )

    clear_mcp_adapter_registry()
    register_mcp_adapter(
        "publisher-policy",
        "adapter-v1",
        "policy-v1",
        classify,
        flow_direction="both",
    )
    compatible = ToolRegistry().authorize_tool(
        tool.name,
        context,
        enforce=True,
        mcp_tool=tool,
        arguments=arguments,
        ifc_labels=InformationFlowLabels(),
    )
    assert compatible.allowed is True
    assert compatible.decision is OperationDecision.ADMIN_REQUIRED
    assert compatible.required_tier.value == "admin"
    assert compatible.flow_direction is ToolFlowDirection.BOTH
    assert compatible.protected_source_resources == ("private-source",)
    assert compatible.protected_sink_resources == ("public-destination",)

    handler_calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    def request(call_id: str) -> ToolCallRequest:
        return ToolCallRequest(
            tool_call={
                "name": tool.name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            },
            tool=tool,
            state=None,
            runtime=Runtime(context=context),
        )

    turn = _make_ctx()
    turn.auth_context = context
    turn.ifc_labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"private-channel"}),
    )
    token = set_current_turn(turn)
    try:
        denied = await BudgetGateMiddleware().awrap_tool_call(
            request("tainted"), handler,
        )
        turn.ifc_labels = InformationFlowLabels()
        allowed = await BudgetGateMiddleware().awrap_tool_call(
            request("untainted"), handler,
        )
    finally:
        reset_current_turn(token)
        clear_mcp_adapter_registry()

    assert denied.status == "error"
    assert "ifc_label_blocked:external_mcp" in str(denied.content)
    assert allowed.content == "ok"
    assert handler_calls == 1


def _ifc_labels(
    channel: str = "ch-1",
    *,
    sources: frozenset[str] | None = None,
) -> InformationFlowLabels:
    channels = sources if sources is not None else frozenset({channel})
    return InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=channels,
        sources=frozenset(SourceLabel(
            principal="user-1", domain="channel", resource_id=source,
            bridge_instance="test", sensitivity="private",
            authorized_principals=frozenset({"user-1"}),
        ) for source in channels),
    )


def _ifc_auth(*, roles: tuple[str, ...] = ("admin",)) -> AuthContext:
    labels = _ifc_labels()
    return AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=roles,
        event_ingress=None,
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=labels,
        domain="channel",
        resource_id="ch-1",
        bridge_instance="test",
    )


def _ifc_turn(auth: AuthContext) -> TurnContext:
    ctx = _make_ctx()
    ctx.auth_context = auth
    ctx.ifc_labels = auth.ifc_labels
    return ctx


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
        domain="channel",
        resource_id=ctx.channel_id,
        bridge_instance="test",
        ifc_labels=InformationFlowLabels(),
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
        domain="channel",
        resource_id="ch-1",
        bridge_instance="test",
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


@pytest.mark.asyncio
async def test_budget_denied_delegation_does_not_merge_propagated_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    ctx.tool_call_budget = 1
    ctx.tool_call_count = 1
    tainted = auth.ifc_labels.with_source(SourceLabel(
        principal="service:task", domain="service", resource_id="task",
        bridge_instance="task", sensitivity="private", integrity="untrusted",
        integrity_effect="active_ingest",
    ))
    monkeypatch.setattr(
        "mimir.agent._propagate_ifc_labels", lambda *args, **kwargs: tainted,
    )

    async def handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _make_request("task", "denied-task", auth), handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status == "error"
    assert "Tool-call budget exhausted" in str(result.content)
    assert auth.ifc_state.current(auth.ifc_labels) == auth.ifc_labels
    assert ctx.ifc_labels == auth.ifc_labels


def test_prohibited_delegation_does_not_merge_propagated_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    tainted = auth.ifc_labels.with_source(SourceLabel(
        principal="service:bash_async", domain="service", resource_id="bash_async",
        bridge_instance="bash_async", sensitivity="private", integrity="untrusted",
        integrity_effect="active_ingest",
    ))
    monkeypatch.setattr(
        "mimir.agent._propagate_ifc_labels", lambda *args, **kwargs: tainted,
    )

    def handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = BudgetGateMiddleware().wrap_tool_call(
            _make_request(
                "bash_async", "prohibited-bash", auth,
                {"command": "git push --force origin main"},
            ),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status == "error"
    assert auth.ifc_state.current(auth.ifc_labels) == auth.ifc_labels
    assert ctx.ifc_labels == auth.ifc_labels


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


def test_sync_protected_read_allows_compatible_harness_egress():
    from mimir.agent import Agent
    from mimir.access_control import protected_result_source, publish_protected_result

    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()

    def handler(req: ToolCallRequest) -> ToolMessage:
        publish_protected_result((protected_result_source(
            auth,
            principal="filesystem",
            domain="filesystem",
            resource_id="/private/data",
            bridge_instance="filesystem",
        ),))
        return ToolMessage(content="protected file", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = middleware.wrap_tool_call(
            _make_request("read_file", "read-sync", auth, {"path": "/private/data"}),
            handler,
        )
        allowed = Agent._harness_sink_allowed(ctx, "ch-1", "harness_auto_deliver")
    finally:
        reset_current_turn(token)

    assert result.content == "protected file"
    assert any(source.domain == "filesystem" for source in ctx.ifc_labels.sources)
    assert allowed is True


@pytest.mark.asyncio
async def test_real_commitment_list_with_ownerless_record_allows_same_channel_reply(
    tmp_path: Path,
):
    from mimir.agent import Agent
    from mimir.commitments.models import CommitmentRecord, make_commitment_id
    from mimir.commitments.store import CommitmentsStore
    from mimir.tools.registry import commitment_list, set_commitments_store

    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()
    store = CommitmentsStore(path=tmp_path / "commitments.jsonl")
    await store.add(CommitmentRecord(
        id=make_commitment_id(),
        channel_id="ch-1",
        text="legacy ownerless commitment",
        owner_principal=None,
    ))
    set_commitments_store(store)

    async def handler(req: ToolCallRequest) -> ToolMessage:
        result = await commitment_list.coroutine(  # type: ignore[misc]
            due_within_days=0,
            runtime=req.runtime,
        )
        return ToolMessage(content=result, tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await middleware.awrap_tool_call(
            _make_request("commitment_list", "commitments-ownerless", auth),
            handler,
        )
        allowed = Agent._harness_sink_allowed(ctx, "ch-1", "harness_auto_deliver")
    finally:
        reset_current_turn(token)
        set_commitments_store(None)

    assert "legacy ownerless commitment" in str(result.content)
    commitment_sources = [
        source for source in ctx.ifc_labels.sources
        if source.domain == "commitments"
    ]
    assert len(commitment_sources) == 1
    assert commitment_sources[0].is_complete
    assert "user-1" in commitment_sources[0].authorized_principals
    assert allowed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "domain", "args"),
    [
        ("memory_query", "saga", {"query": "project"}),
        ("memory_get", "saga", {"atom_ids": ["a-1"]}),
        ("file_search", "filesystem", {"query": "project"}),
        ("get_turn", "turn_history", {"turn_id": "t-1"}),
        ("commitment_list", "commitments", {}),
        ("bash_jobs_list", "shell_jobs", {}),
        ("list_schedules", "schedule_metadata", {}),
        ("list_channels", "channel_metadata", {}),
    ],
)
async def test_exact_protected_native_result_can_reply_to_triggering_channel(
    tool_name: str,
    domain: str,
    args: dict[str, Any],
):
    from mimir.access_control import protected_result_source, publish_protected_result

    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()

    async def protected(req: ToolCallRequest) -> ToolMessage:
        publish_protected_result((protected_result_source(
            auth,
            principal=f"owner:{domain}",
            domain=domain,
            resource_id=f"{domain}:resource-1",
            bridge_instance="mimir",
        ),))
        return ToolMessage(content="protected", tool_call_id=req.tool_call["id"])

    async def send(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="sent", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        await middleware.awrap_tool_call(
            _make_request(tool_name, "read", auth, args), protected,
        )
        reply = await middleware.awrap_tool_call(
            _make_request("send_message", "reply", auth, {"channel_id": "ch-1"}),
            send,
        )
    finally:
        reset_current_turn(token)

    assert reply.status != "error"
    assert any(source.domain == domain for source in ctx.ifc_labels.sources)


@pytest.mark.asyncio
async def test_mixed_protected_result_requires_requester_in_every_resource_acl():
    from mimir.access_control import publish_protected_result

    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()
    compatible = SourceLabel(
        principal="owner-a", domain="saga", resource_id="atom:a",
        bridge_instance="saga", sensitivity="private",
        authorized_principals=frozenset({"user-1"}), source_kind="protected_tool",
    )
    incompatible = SourceLabel(
        principal="owner-b", domain="saga", resource_id="atom:b",
        bridge_instance="saga", sensitivity="private",
        authorized_principals=frozenset({"owner-b"}), source_kind="protected_tool",
    )

    async def protected(req: ToolCallRequest) -> ToolMessage:
        publish_protected_result((compatible, incompatible))
        return ToolMessage(content="mixed", tool_call_id=req.tool_call["id"])

    send_calls = 0

    async def send(req: ToolCallRequest) -> ToolMessage:
        nonlocal send_calls
        send_calls += 1
        return ToolMessage(content="sent", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        await middleware.awrap_tool_call(
            _make_request("memory_query", "read", auth, {"query": "mixed"}), protected,
        )
        denied = await middleware.awrap_tool_call(
            _make_request("send_message", "reply", auth, {"channel_id": "ch-1"}), send,
        )
    finally:
        reset_current_turn(token)

    assert denied.status == "error"
    assert "ifc_label_blocked:same_channel" in str(denied.content)
    assert send_calls == 0


@pytest.mark.asyncio
async def test_authoritative_empty_protected_result_does_not_add_taint():
    from mimir.access_control import publish_protected_result

    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()

    async def empty(req: ToolCallRequest) -> ToolMessage:
        publish_protected_result(())
        return ToolMessage(content="(no atoms)", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        before = ctx.ifc_labels
        await middleware.awrap_tool_call(
            _make_request("memory_query", "empty", auth, {"query": "none"}), empty,
        )
    finally:
        reset_current_turn(token)

    assert ctx.ifc_labels == before


@pytest.mark.asyncio
async def test_success_without_protected_result_provenance_remains_fail_closed():
    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()

    async def unknown(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="unproven memory", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        await middleware.awrap_tool_call(
            _make_request("memory_get", "unknown", auth, {"atom_ids": ["a-1"]}), unknown,
        )
    finally:
        reset_current_turn(token)

    source = next(source for source in ctx.ifc_labels.sources if source.domain == "saga")
    assert source.is_complete is False


@pytest.mark.asyncio
async def test_async_partial_error_taints_and_blocks_next_same_channel_send():
    auth = _ifc_auth()
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()

    async def partial(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="partial protected output before failure",
            tool_call_id=req.tool_call["id"],
            status="error",
        )

    send_calls = 0

    async def send(req: ToolCallRequest) -> ToolMessage:
        nonlocal send_calls
        send_calls += 1
        return ToolMessage(content="sent", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        partial_result = await middleware.awrap_tool_call(
            _make_request("memory_get", "partial", auth, {"atom_id": "other-user"}),
            partial,
        )
        denied = await middleware.awrap_tool_call(
            _make_request("send_message", "send", auth, {"channel_id": "ch-1"}),
            send,
        )
    finally:
        reset_current_turn(token)

    assert partial_result.status == "error"
    assert any(source.domain == "saga" for source in ctx.ifc_labels.sources)
    assert denied.status == "error"
    assert "ifc_label_blocked:same_channel" in str(denied.content)
    assert send_calls == 0


@pytest.mark.asyncio
async def test_state_only_fork_taint_blocks_first_send_with_stale_active_turn_labels():
    auth = _ifc_auth(roles=())
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()
    send_calls = 0

    async def send(req: ToolCallRequest) -> ToolMessage:
        nonlocal send_calls
        send_calls += 1
        return ToolMessage(content="sent", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        allowed = await middleware.awrap_tool_call(
            _make_request("send_message", "before-fork", auth, {"channel_id": "ch-1"}),
            send,
        )
        # Mirrors a detached result merge with no _current_turn to rebind ctx.
        auth.ifc_state.merge(_ifc_labels(sources=frozenset({"ch-private"})))
        denied = await middleware.awrap_tool_call(
            _make_request("send_message", "after-fork", auth, {"channel_id": "ch-1"}),
            send,
        )
    finally:
        reset_current_turn(token)

    assert allowed.status != "error"
    assert denied.status == "error"
    assert "ifc_label_blocked:same_channel" in str(denied.content)
    assert send_calls == 1
    assert ctx.ifc_labels is auth.ifc_labels


@pytest.mark.asyncio
async def test_same_channel_history_reply_succeeds_and_public_tool_does_not_overtaint():
    auth = _ifc_auth(roles=())
    ctx = _ifc_turn(auth)
    middleware = BudgetGateMiddleware()

    async def handler(req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        await middleware.awrap_tool_call(
            _make_request("write_todos", "public", auth), handler,
        )
        before = ctx.ifc_labels
        await middleware.awrap_tool_call(
            _make_request(
                "fetch_channel_history", "history", auth, {"channel_id": "ch-1"},
            ),
            handler,
        )
        reply = await middleware.awrap_tool_call(
            _make_request("send_message", "reply", auth, {"channel_id": "ch-1"}),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert before == auth.ifc_labels
    assert reply.status != "error"
    assert all(source.domain == "channel" for source in ctx.ifc_labels.sources)


@pytest.mark.asyncio
async def test_result_taint_is_fork_visible_and_isolated_between_concurrent_turns():
    middleware = BudgetGateMiddleware()
    protected_auth = _ifc_auth()
    public_auth = _ifc_auth(roles=())
    protected_ctx = _ifc_turn(protected_auth)
    public_ctx = _ifc_turn(public_auth)

    async def handler(req: ToolCallRequest) -> ToolMessage:
        await asyncio.sleep(0)
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    async def invoke(ctx: TurnContext, request: ToolCallRequest) -> None:
        token = set_current_turn(ctx)
        try:
            await middleware.awrap_tool_call(request, handler)
        finally:
            reset_current_turn(token)

    await asyncio.gather(
        invoke(
            protected_ctx,
            _make_request("get_turn", "protected-turn", protected_auth, {"turn_id": "t0"}),
        ),
        invoke(public_ctx, _make_request("write_todos", "public-turn", public_auth)),
    )

    assert any(source.domain == "turn_history" for source in protected_ctx.ifc_labels.sources)
    assert not any(source.domain == "turn_history" for source in public_ctx.ifc_labels.sources)

    # No ContextVar: the original frozen request carrier still sees post-read taint.
    async def should_not_send(req: ToolCallRequest) -> ToolMessage:
        raise AssertionError("forked incompatible sink executed")

    denied = await middleware.awrap_tool_call(
        _make_request("send_message", "forked-send", protected_auth, {"channel_id": "ch-1"}),
        should_not_send,
    )
    assert denied.status == "error"
    assert "ifc_label_blocked:same_channel" in str(denied.content)


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
async def test_protected_metadata_tool_is_denied_to_non_admin(tmp_path: Path) -> None:
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
        return ToolMessage(content="protected-schedule", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("list_schedules", "id-read"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "protected-schedule" not in str(out.content)
    assert handler_calls == 0


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


def test_enforced_missing_context_mcp_call_denies_without_startup_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")

    def fail_if_called(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("startup completeness assertion reached the hot path")

    monkeypatch.setattr(
        "mimir.access_control.resolve_access_control_enforcement",
        fail_if_called,
    )
    handler_calls = 0

    def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="should not run", tool_call_id=req.tool_call["id"])

    out = BudgetGateMiddleware().wrap_tool_call(
        _make_request("mcp_synthetic_uncataloged", "id-mcp-missing-ctx"),
        handler,
    )

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "missing_auth_context" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_admin_gate_missing_context_denies_protected_metadata_when_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="protected-schedule", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(_make_request("list_schedules", "id-read"), handler)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "missing_auth_context" in str(out.content)
    assert "protected-schedule" not in str(out.content)
    assert handler_calls == 0


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
    assert "approve_declassification" in names
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
