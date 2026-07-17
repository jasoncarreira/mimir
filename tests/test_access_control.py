from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    AccessStatus,
    DenialReason,
    HTTP_EVENT_INGRESS_EXTRA_KEY,
    OperationDecision,
    ToolRegistry,
    authorize_action,
    authorize_inbound,
    create_auth_context,
)
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, AuthContext, SourceLabel, TurnContext


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def _event(author: str | None) -> AgentEvent:
    return AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author=author,
        content="hello",
    )


def test_inbound_allows_allowlisted_user_when_enforced(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    decision = authorize_inbound(_event("slack-U1"), resolver, enforce=True)

    assert decision.allowed is True
    assert decision.status == AccessStatus.USER_ALLOWED
    assert decision.denial_reason is None
    assert decision.canonical_author == "alice"
    assert decision.roles == ("user",)


def test_inbound_distinguishes_known_non_allowlisted_from_unknown(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
        """,
    )

    known = authorize_inbound(_event("slack-U1"), resolver, enforce=True)
    unknown = authorize_inbound(_event("slack-U2"), resolver, enforce=True)

    assert known.allowed is False
    assert known.status == AccessStatus.DENIED
    assert known.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert known.canonical_author == "alice"
    assert unknown.allowed is False
    assert unknown.reason == DenialReason.UNKNOWN_AUTHOR
    assert unknown.canonical_author == "slack-U2"


def test_admin_action_requires_admin_role(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: root
            aliases: [slack-UADMIN]
            access: {roles: [user, admin]}
        """,
    )

    user = authorize_action(_event("slack-U1"), resolver, admin=True, enforce=True)
    admin = authorize_action(_event("slack-UADMIN"), resolver, admin=True, enforce=True)

    assert user.allowed is False
    assert user.reason == DenialReason.ADMIN_REQUIRED
    assert admin.allowed is True
    assert admin.status == AccessStatus.ADMIN_ALLOWED
    assert admin.reason is None


def test_admin_action_follows_canonical_aliases_across_slack_discord(
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

    slack = authorize_action("slack-UADMIN", resolver, admin=True, enforce=True)
    discord = authorize_action("discord-42", resolver, admin=True, enforce=True)

    assert slack.allowed is True
    assert discord.allowed is True
    assert slack.canonical_author == "root"
    assert discord.canonical_author == "root"
    assert slack.roles == ("user", "admin")
    assert discord.roles == ("user", "admin")


@pytest.mark.parametrize(
    "tool_name",
    [
        "memory_store",
        "memory_query",
        "memory_get",
        "saga_feedback",
        "saga_mark_contributions",
        "saga_end_session",
        "saga_record_skill_learning",
        "bash_jobs_list",
        "bash_job_output",
        "write_todos",
        "defer_injected_message",
        "commitment_complete",
        "commitment_snooze",
        "commitment_dismiss",
    ],
)
def test_admin_turn_can_use_routine_cataloged_tools_when_enforced(
    tool_name: str,
) -> None:
    auth = AuthContext(
        principal="slack-UADMIN",
        canonical_principal="root",
        roles=("user", "admin"),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
    )

    result = ToolRegistry().authorize_tool(tool_name, auth, enforce=True)

    assert result.allowed is True
    assert result.decision is not OperationDecision.UNKNOWN
    assert result.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "service_trigger", "service_principal"),
    [
        ("list_channels", "poller", "poller"),
        ("list_schedules", "upgrade", "system"),
        ("bash_jobs_list", "scheduled_tick", "scheduler"),
    ],
)
@pytest.mark.parametrize(
    ("caller", "should_render"),
    [
        ("regular", False),
        ("admin", True),
        ("service", True),
        ("missing", False),
        ("http", False),
    ],
)
async def test_protected_metadata_reads_authorize_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    service_trigger: str,
    service_principal: str,
    caller: str,
    should_render: bool,
) -> None:
    from langchain_core.messages import ToolMessage
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kwargs: None
    )
    if caller == "service":
        auth_context = create_auth_context(
            AgentEvent(
                trigger=service_trigger,
                channel_id=f"{service_trigger}:test",
                service_principal=service_principal,
            ),
            enforce=True,
        )
    elif caller == "missing":
        auth_context = None
    else:
        auth_context = AuthContext(
            principal=f"{caller}-principal",
            canonical_principal=caller,
            roles=("user", "admin") if caller in {"admin", "http"} else ("user",),
            event_ingress="http_event" if caller == "http" else None,
            trigger="user_message",
            channel_id="slack-C1",
            interactivity=None,
            enforcement_enabled=True,
        )

    protected_result = f"protected-metadata:{tool_name}"
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(
            content=protected_result,
            tool_call_id=request.tool_call["id"],
        )

    result = await BudgetGateMiddleware().awrap_tool_call(
        _tool_request(auth_context, tool_name=tool_name, args={}), handler
    )

    assert handler_calls == int(should_render)
    if should_render:
        assert result.status != "error"
        assert result.content == protected_result
    else:
        assert result.status == "error"
        assert protected_result not in str(result.content)


def test_legacy_default_allows_but_reports_would_deny_reason(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
        """,
    )

    decision = authorize_inbound(_event("slack-U1"), resolver)

    assert decision.allowed is True
    assert decision.status == AccessStatus.LEGACY_ALLOWED
    assert decision.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert decision.enforcement_enabled is False


def test_missing_resolver_preserves_single_operator_legacy_behavior() -> None:
    decision = authorize_action(_event("slack-U1"), None, admin=True)

    assert decision.allowed is True
    assert decision.status == AccessStatus.LEGACY_ALLOWED
    assert decision.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert decision.canonical_author == "slack-U1"


def test_missing_author_has_stable_denial_reason_when_enforced() -> None:
    decision = authorize_inbound(_event(None), None, enforce=True)

    assert decision.allowed is False
    assert decision.status == AccessStatus.DENIED
    assert decision.denial_reason == "missing_author"


def test_log_fields_are_stable_string_values(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    fields = authorize_action(
        "slack-U1",
        resolver,
        admin=True,
        enforce=True,
    ).as_log_fields()

    assert fields == {
        "allowed": False,
        "status": "denied",
        "required_tier": "admin",
        "denial_reason": "admin_required",
        "author": "slack-U1",
        "canonical_author": "alice",
        "roles": ["user"],
        "enforcement_enabled": True,
    }


def test_auth_context_frozen_is_immutable(tmp_path: Path) -> None:
    """Verify AuthContext is frozen and cannot be mutated after creation."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user, admin]}
        """,
    )

    event = _event("slack-U1")
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.principal == "slack-U1"
    assert auth_ctx.canonical_principal == "alice"
    assert auth_ctx.roles == ("user", "admin")
    assert auth_ctx.is_service is False

    with pytest.raises(FrozenInstanceError):
        auth_ctx.roles = ("user", "admin", "service")
    with pytest.raises(FrozenInstanceError):
        auth_ctx.enforcement_enabled = True


def test_auth_context_carries_ingress_provenance(tmp_path: Path) -> None:
    """Verify AuthContext captures server-owned ingress metadata."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author="slack-U1",
        content="hello",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
    )
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.event_ingress == "http-api"
    assert auth_ctx.trigger == "user_message"
    assert auth_ctx.channel_id == "slack-C1"


def test_auth_context_service_identity(tmp_path: Path) -> None:
    """Verify AuthContext captures service identity from identity resolver."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: mcp-service
            aliases: [mcp-1]
            access: {roles: [service], is_service: true}
        """,
    )

    event = _event("mcp-1")
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.is_service is True
    assert "service" in auth_ctx.roles


def test_service_only_identity_does_not_get_user_inbound_access(tmp_path: Path) -> None:
    """Service classification alone must not widen USER-tier policy."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: external-service
            aliases: [service-external]
            access: {roles: [service], is_service: true}
          - canonical: trusted-service-user
            aliases: [service-trusted]
            access: {roles: [service, user], is_service: true}
        """,
    )

    external = authorize_inbound(_event("service-external"), resolver, enforce=True)
    trusted = authorize_inbound(_event("service-trusted"), resolver, enforce=True)

    assert external.allowed is False
    assert external.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert external.roles == ("service",)
    assert trusted.allowed is True
    assert trusted.status == AccessStatus.USER_ALLOWED
    assert trusted.roles == ("service", "user")


def test_http_ingress_extra_key_blocks_service_grant() -> None:
    """Verify that HTTP ingress via extra[HTTP_EVENT_INGRESS_EXTRA_KEY] blocks service authority.

    This is a defense-in-depth check: even when an event matches a registered
    service principal (trigger + canonical), if it came via HTTP ingress
    (detected via the canonical extra key), service authority should NOT be granted.
    """
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
    )

    auth_ctx = create_auth_context(event, enforce=True)

    assert auth_ctx.event_ingress is not None, "HTTP ingress should be detected from extra"
    assert auth_ctx.is_service is False, "Service authority should NOT be granted for HTTP ingress"


def _turn(turn_id: str, saga_session_id: str, auth_context: AuthContext) -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        session_id=turn_id,
        saga_session_id=saga_session_id,
        trigger="user_message",
        channel_id=auth_context.channel_id,
        started_at=0.0,
        auth_context=auth_context,
        access_control_enforced=True,
    )


def _tool_request(
    auth_context: object | None,
    *,
    session_id: str = "forged",
    tool_name: str = "shell_exec",
    args: dict[str, object] | None = None,
):
    from langchain.agents.middleware import ToolCallRequest
    from langgraph.runtime import Runtime

    return ToolCallRequest(
        tool_call={
            "name": tool_name,
            "args": args or {"command": "true", "session_id": session_id},
            "id": "tc-auth",
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=Runtime(context=auth_context),
    )


@pytest.mark.parametrize(
    "malformed_carrier",
    [
        {},
        object(),
        SimpleNamespace(
            roles=("admin",),
            enforcement_enabled=False,
            event_ingress=None,
        ),
    ],
    ids=["empty-dict", "arbitrary-object", "auth-lookalike"],
)
def test_malformed_runtime_carrier_fails_closed_under_process_enforcement(
    malformed_carrier: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an actual AuthContext may carry authority for a tool request."""
    from langchain_core.messages import ToolMessage
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ran", tool_call_id="tc-auth")

    result = BudgetGateMiddleware().wrap_tool_call(
        _tool_request(malformed_carrier), handler
    )

    assert called is False
    assert result.status == "error"
    assert "missing_auth_context" in str(result.content)


def test_forged_session_id_cannot_select_concurrent_admin_turn(tmp_path: Path) -> None:
    """Both principals are live; the request carrier, not model args, wins."""
    import asyncio
    from langchain_core.messages import ToolMessage
    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools.budget_gate import BudgetGateMiddleware

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [user, admin]}
        """,
    )
    alice = create_auth_context(_event("slack-U1"), resolver, enforce=True)
    bob = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="slack-C2", author="slack-U2"),
        resolver,
        enforce=True,
    )
    alice_token = set_current_turn(_turn("turn-alice", "saga-alice", alice))
    bob_token = set_current_turn(_turn("turn-bob", "saga-bob", bob))
    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ran", tool_call_id="tc-auth")

    try:
        result = asyncio.run(
            BudgetGateMiddleware().awrap_tool_call(
                _tool_request(alice, session_id="saga-bob"), handler
            )
        )
    finally:
        reset_current_turn(bob_token)
        reset_current_turn(alice_token)

    assert called is False
    assert result.status == "error"
    assert "requires an admin identity" in str(result.content)


def test_exact_request_carrier_resists_concurrent_principal_swap(tmp_path: Path) -> None:
    """An inherited/admin ContextVar cannot replace the user request carrier."""
    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools.budget_gate import _auth_context_from_request

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [admin]}
        """,
    )
    alice = create_auth_context(_event("slack-U1"), resolver, enforce=True)
    bob = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="slack-C2", author="slack-U2"),
        resolver,
        enforce=True,
    )
    token = set_current_turn(_turn("turn-bob", "saga-bob", bob))
    try:
        resolved = _auth_context_from_request(_tool_request(alice))
    finally:
        reset_current_turn(token)

    assert resolved is alice
    assert resolved.canonical_principal == "alice"
    assert "admin" not in resolved.roles


def test_auth_context_ignores_mutated_resolver_and_event(tmp_path: Path) -> None:
    """Roles/provenance remain the ingress snapshot after mutable inputs change."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    event = _event("slack-U1")
    auth_context = create_auth_context(event, resolver, enforce=True)

    event.author = "slack-UADMIN"
    event.trigger = "scheduled_tick"
    event.extra["event_ingress"] = "trusted-later"
    (tmp_path / "state" / "identities.yaml").write_text(
        "people:\n  - canonical: alice\n    aliases: [slack-U1]\n"
        "    access: {roles: [user, admin]}\n",
        encoding="utf-8",
    )
    resolver.reload()

    assert auth_context.principal == "slack-U1"
    assert auth_context.trigger == "user_message"
    assert auth_context.event_ingress is None
    assert auth_context.roles == ("user",)


def test_detached_request_uses_explicit_carrier_not_inherited_context(tmp_path: Path) -> None:
    """A detached task with an inherited admin turn still honors its user carrier."""
    import asyncio
    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools.budget_gate import _auth_context_from_request

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [admin]}
        """,
    )
    alice = create_auth_context(_event("slack-U1"), resolver, enforce=True)
    bob = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="slack-C2", author="slack-U2"),
        resolver,
        enforce=True,
    )
    token = set_current_turn(_turn("turn-bob", "saga-bob", bob))

    async def run_detached():
        task = asyncio.create_task(
            asyncio.sleep(0, result=_auth_context_from_request(_tool_request(alice)))
        )
        return await task

    try:
        resolved = asyncio.run(run_detached())
    finally:
        reset_current_turn(token)
    assert resolved is alice
    assert resolved.roles == ("user",)


def test_missing_request_carrier_denies_admin_tool_under_enforcement(monkeypatch) -> None:
    import asyncio
    from langchain_core.messages import ToolMessage
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ran", tool_call_id="tc-auth")

    result = asyncio.run(BudgetGateMiddleware().awrap_tool_call(_tool_request(None), handler))
    assert called is False
    assert result.status == "error"
    assert "requires an admin identity" in str(result.content)


def test_claude_sdk_hook_fails_closed_without_exact_carrier(monkeypatch) -> None:
    """SDK built-in/MCP hooks never treat session_id or inherited turns as authz."""
    from mimir import _langchain_claude_code_patches as patches

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    denial = patches._claude_code_pre_tool_enforcement(
        "Bash", {"command": "true"}, "sdk-tool-1", session_id="saga-admin"
    )

    assert denial["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "missing_auth_context" in denial["hookSpecificOutput"]["permissionDecisionReason"]


def test_http_event_ingress_denies_without_server_owned_principal_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic HTTP credentials authenticate transport only - no server-owned principal."""
    import asyncio
    from langchain_core.messages import ToolMessage
    from mimir.tools.budget_gate import BudgetGateMiddleware

    captured: list[tuple[str, dict]] = []

    def _capture(kind: str, **kw: dict):
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

    ctx = TurnContext(
        turn_id="turn-1",
        session_id="saga-1",
        trigger="user_message",
        channel_id="slack-C1",
        started_at=0.0,
        tool_call_budget=10,
    )
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    ctx.auth_context = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user",),
        event_ingress="http-api",
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
    )

    mw = BudgetGateMiddleware()
    token = set_current_turn(ctx)
    try:
        async def handler(req):
            return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

        result = asyncio.run(
            mw.awrap_tool_call(
                _tool_request(ctx.auth_context, session_id="saga-1"), handler
            )
        )
    finally:
        reset_current_turn(token)

    assert result.status == "error"
    kinds = [kind for kind, _kw in captured]
    assert "admin_tool_call_denied" in kinds
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["denial_reason"] is not None


def test_enforcement_on_missing_context_denies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcement-on with missing auth context denies all non-open operations."""
    import asyncio
    from langchain_core.messages import ToolMessage
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    captured: list[tuple[str, dict]] = []

    def _capture(kind: str, **kw: dict):
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)

    mw = BudgetGateMiddleware()

    async def handler(req):
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    result = asyncio.run(mw.awrap_tool_call(_tool_request(None), handler))

    assert result.status == "error"
    assert "missing_auth_context" in str(result.content).lower()
    kinds = [kind for kind, _kw in captured]
    assert "admin_tool_call_denied" in kinds
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["denial_reason"] == "missing_auth_context"


def test_enforcement_on_unknown_context_denies(tmp_path: Path) -> None:
    """Enforcement-on with unknown author denies at inbound."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    event = _event("unknown-user-123")
    decision = authorize_inbound(event, resolver, enforce=True)

    assert decision.allowed is False
    assert decision.status == AccessStatus.DENIED
    assert decision.reason in (DenialReason.UNKNOWN_AUTHOR, DenialReason.USER_NOT_ALLOWLISTED)


def test_unknown_mcp_tool_denies_under_enforcement(tmp_path: Path) -> None:
    """Unknown MCP tools are denied under enforcement."""
    from mimir.access_control import MCPResourceAdapter

    auth_ctx = AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
    )

    result = MCPResourceAdapter.authorize_mcp_tool(
        "mcp__unknown_tool",
        auth_ctx,
        enforce=True,
    )

    assert result.allowed is False
    assert result.decision.value == "admin_required"
    assert result.reason is not None


@pytest.mark.parametrize("enforce", [False, True])
def test_non_mcp_name_never_falls_through_mcp_adapter(enforce: bool) -> None:
    from mimir.access_control import MCPResourceAdapter, OperationDecision

    result = MCPResourceAdapter.authorize_mcp_tool(
        "shell_exec",
        None,
        enforce=enforce,
    )

    assert result.allowed is False
    assert result.decision == OperationDecision.ADMIN_REQUIRED
    assert result.reason == "non_mcp_tool_name"


def _dispatcher_config(tmp_path: Path, *, enforce: bool):
    from mimir.config import Config

    return replace(
        Config.from_env(),
        home=tmp_path,
        access_control_enforced=enforce,
        worker_idle_timeout_s=0.01,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author", "expected_type", "expected_status", "expected_reason"),
    [
        ("slack-U1", "inbound_event_allowed", "user_allowed", None),
        ("slack-unknown", "inbound_event_denied", "denied", "unknown_author"),
    ],
)
async def test_inbound_audit_events_are_structured_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    author: str,
    expected_type: str,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    """The live dispatcher emits stable decisions without message bodies/secrets."""
    from mimir.dispatcher import Dispatcher

    captured: list[dict[str, object]] = []

    async def capture_event(event_type: str, **payload: object) -> None:
        captured.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.dispatcher.log_event", capture_event)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    dispatcher = Dispatcher(_dispatcher_config(tmp_path, enforce=True), resolver=resolver)
    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author=author,
        author_id="U1" if author == "slack-U1" else "U-unknown",
        source="slack",
        content="secret-message-body",
        extra={"api_key": "secret-api-key"},
    )

    accepted = await dispatcher._authorize_bridge_event(event)

    assert accepted is (expected_type == "inbound_event_allowed")
    decision_event = next(row for row in captured if row["type"] == expected_type)
    assert decision_event == {
        "type": expected_type,
        "source": "slack",
        "channel_id": "slack-C1",
        "author": author,
        "raw_author_handle": author,
        "author_id": "U1" if author == "slack-U1" else "U-unknown",
        "canonical_author": "alice" if author == "slack-U1" else "slack-unknown",
        "status": expected_status,
        "trigger": "user_message",
        "enforcement_enabled": True,
        **({"reason": expected_reason} if expected_reason is not None else {}),
    }
    rendered = repr(captured)
    assert "secret-message-body" not in rendered
    assert "secret-api-key" not in rendered
    assert "api_key" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enforce", "tool_name", "args"),
    [
        (False, "send_message", {"channel_id": "api-C1", "text": "forged"}),
        (False, "future_dynamic_tool", {"scope": "forged"}),
        (True, "send_message", {"channel_id": "api-C1", "text": "forged"}),
    ],
    ids=[
        "compat-resource-scoped",
        "compat-unknown-operation",
        "enforced-resource-scoped",
    ],
)
async def test_http_transport_principal_mapping_absence_denies_every_non_open_call(
    monkeypatch: pytest.MonkeyPatch,
    enforce: bool,
    tool_name: str,
    args: dict[str, object],
) -> None:
    """A forged HTTP author/trigger cannot turn transport auth into authority."""
    from langchain_core.messages import ToolMessage
    from mimir.tools.budget_gate import BudgetGateMiddleware

    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda kind, **fields: captured.append((kind, fields)),
    )
    auth_context = AuthContext(
        principal="api-root",
        canonical_principal="root",
        roles=("user", "admin"),
        event_ingress="http_event",
        trigger="scheduled_tick",
        channel_id="api-C1",
        interactivity=None,
        enforcement_enabled=enforce,
    )
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    result = await BudgetGateMiddleware().awrap_tool_call(
        _tool_request(auth_context, tool_name=tool_name, args=args), handler
    )

    assert result.status == "error"
    assert "http_event_author_untrusted" in str(result.content)
    assert handler_calls == 0
    denial = next(fields for kind, fields in captured if kind == "admin_tool_call_denied")
    assert denial["tool"] == tool_name
    assert denial["canonical_author"] == "root"
    assert denial["denial_reason"] == "http_event_author_untrusted"
    assert denial["enforcement_enabled"] is enforce


@pytest.mark.asyncio
async def test_concurrent_turns_keep_authority_and_ifc_scope_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent requests cannot borrow admin authority or another turn's labels."""
    from langchain_core.messages import ToolMessage
    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kw: None)

    user_auth = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C-private",
        interactivity=None,
        enforcement_enabled=True,
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
    )
    admin_auth = AuthContext(
        principal="slack-U2",
        canonical_principal="bob",
        roles=("user", "admin"),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C-admin",
        interactivity=None,
        enforcement_enabled=True,
    )
    barrier = asyncio.Barrier(2)
    handler_calls: list[str] = []

    async def run_request(
        auth_context: AuthContext,
        *,
        tool_name: str,
        args: dict[str, object],
        ifc_source: str,
    ):
        ctx = _turn(
            f"turn-{auth_context.canonical_principal}",
            f"saga-{auth_context.canonical_principal}",
            auth_context,
        )
        ctx.ifc_labels = (
            InformationFlowLabels(
                labels=frozenset({"private"}),
                source_channels=frozenset({ifc_source}),
            )
            if auth_context.canonical_principal == "alice"
            else InformationFlowLabels()
        )
        token = set_current_turn(ctx)
        try:
            await barrier.wait()

            async def handler(request):
                handler_calls.append(auth_context.canonical_principal or "unknown")
                return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

            return await BudgetGateMiddleware().awrap_tool_call(
                _tool_request(auth_context, tool_name=tool_name, args=args), handler
            )
        finally:
            reset_current_turn(token)

    user_result, admin_result = await asyncio.gather(
        run_request(
            user_auth,
            tool_name="send_message",
            args={"channel_id": "slack-C-private", "text": "same scope"},
            ifc_source="slack-C-admin",
        ),
        run_request(
            admin_auth,
            tool_name="shell_exec",
            args={"command": "true"},
            ifc_source="slack-C-admin",
        ),
    )

    assert user_result.status == "error"
    assert "ifc_label_blocked:same_channel" in str(user_result.content)
    assert admin_result.status != "error"
    assert handler_calls == ["bob"]


@pytest.mark.asyncio
async def test_same_scope_private_egress_succeeds_through_live_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integrated middleware permits private data back to its source channel."""
    from langchain_core.messages import ToolMessage
    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kw: None)

    auth_context = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
    )
    ctx = _turn("turn-alice", "saga-alice", auth_context)
    ctx.ifc_labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"slack-C1"}),
        sources=frozenset({SourceLabel(
            principal="alice",
            domain="channel",
            resource_id="slack-C1",
            bridge_instance="slack",
            sensitivity="private",
            authorized_principals=frozenset({"alice"}),
        )}),
    )
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="sent", tool_call_id=request.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _tool_request(
                auth_context,
                tool_name="send_message",
                args={"channel_id": "slack-C1", "text": "same scope"},
            ),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status != "error"
    assert result.content == "sent"
    assert handler_calls == 1
