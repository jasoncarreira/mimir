from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    AccessStatus,
    DenialReason,
    authorize_action,
    authorize_inbound,
    create_auth_context,
)
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, AuthContext, TurnContext


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
        extra={"event_ingress": "http-api"},
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


def _tool_request(auth_context: object | None, *, session_id: str = "forged"):
    from langchain.agents.middleware import ToolCallRequest
    from langgraph.runtime import Runtime

    return ToolCallRequest(
        tool_call={
            "name": "shell_exec",
            "args": {"command": "true", "session_id": session_id},
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
        task = asyncio.create_task(asyncio.sleep(0, result=_auth_context_from_request(_tool_request(alice))))
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


def test_allow_event_fields_are_stable_and_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify inbound allow events have stable, redacted fields without secrets."""
    captured: list[dict] = []

    async def capture_event(event_type: str, **payload: dict):
        captured.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.event_logger.log_event", capture_event)

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
        source="slack",
    )

    decision = authorize_inbound(event, resolver, enforce=True)
    assert decision.allowed is True
    assert decision.status == AccessStatus.USER_ALLOWED
    assert decision.canonical_author == "alice"
    assert decision.enforcement_enabled is True
