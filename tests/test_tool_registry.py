from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mimir.access_control import (
    OperationCatalog,
    OperationDecision,
    ResourceScope,
    ServicePrincipal,
    ToolRegistry,
    get_service_principal,
)
from mimir.models import AuthContext
from mimir.tools import budget_gate


_OLD_ADMIN_TOOLS = {
    "add_schedule",
    "set_schedule_priority",
    "remove_schedule",
    "reload_pollers",
    "open_proposal",
    "submit_proposal",
    "abandon_proposal",
    "request_mimir_update",
    "worklink_run",
    "shell_exec",
    "bash_async",
    "spawn_claude_code",
    "spawn_codex",
    "saga_forget",
    "write_file",
    "edit_file",
}


def _auth_context(
    *,
    trigger: str = "user_message",
    roles: tuple[str, ...] = ("user",),
    enforce: bool = False,
    event_ingress: str | None = None,
) -> AuthContext:
    return AuthContext(
        principal=None,
        canonical_principal=None,
        roles=roles,
        event_ingress=event_ingress,
        trigger=trigger,
        channel_id="scheduler:test",
        interactivity=None,
        enforcement_enabled=enforce,
    )


def test_admin_catalog_never_shrinks_and_preserves_mcp_suffixes() -> None:
    catalog = OperationCatalog()

    assert _OLD_ADMIN_TOOLS <= catalog._ADMIN_REQUIRED_OPERATIONS
    assert catalog.get_decision("mcp__mimir__shell_exec") == OperationDecision.ADMIN_REQUIRED
    assert catalog.get_decision("mcp_mimir_shell_exec") == OperationDecision.ADMIN_REQUIRED


def test_unknown_operation_fails_closed_only_when_enforced() -> None:
    registry = ToolRegistry()

    legacy = registry.authorize_tool("new_unclassified_tool", enforce=False)
    enforced = registry.authorize_tool("new_unclassified_tool", enforce=True)

    assert legacy.allowed is True
    assert legacy.is_shadow_decision is True
    assert enforced.allowed is False
    assert enforced.reason == "unknown_operation"


@pytest.mark.asyncio
async def test_shadow_decision_is_emitted_on_live_authorization_path() -> None:
    registry = ToolRegistry()
    registry.enable_shadow_logging()
    captured: list[tuple[str, dict[str, object]]] = []

    async def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    with patch("mimir.event_logger.log_event", new=capture):
        decision = registry.authorize_tool("new_unclassified_tool", enforce=False)
        await asyncio.sleep(0)

    assert decision.allowed is True
    assert captured == [
        (
            "shadow_tool_decision",
            decision.as_log_fields(),
        )
    ]


def test_resource_scoped_operation_is_gated_by_live_path() -> None:
    catalog = budget_gate.get_tool_registry()
    operation_catalog = __import__(
        "mimir.access_control", fromlist=["get_operation_catalog"]
    ).get_operation_catalog()
    operation_catalog.register_operation(
        "scoped_test_tool",
        OperationDecision.RESOURCE_SCOPED,
        [ResourceScope(domain="channel")],
    )

    try:
        assert budget_gate._is_admin_sensitive_tool(
            "scoped_test_tool", _auth_context(enforce=True)
        )
    finally:
        operation_catalog._custom_decisions.pop("scoped_test_tool", None)
        operation_catalog._resource_scoped_operations.pop("scoped_test_tool", None)
        catalog.disable_shadow_logging()


def test_runtime_inventory_replaced_from_final_model_surface() -> None:
    registry = ToolRegistry()
    registry.register_tool("stale")
    tools = [
        SimpleNamespace(name="native_tool", description="native"),
        SimpleNamespace(name="write_file", description="built-in"),
    ]

    registry.register_runtime_tools(tools)

    assert registry.list_tools() == ["native_tool", "write_file"]
    assert registry.get_tool("native_tool") == {
        "name": "native_tool",
        "description": "native",
        "category": "runtime",
        "is_native": False,
        "is_builtin": False,
        "is_dynamic": False,
        "is_external": False,
    }


def test_budget_middleware_inventories_model_bound_tools() -> None:
    registry = budget_gate.get_tool_registry()
    middleware = budget_gate.BudgetGateMiddleware()
    request = SimpleNamespace(tools=[SimpleNamespace(name="assembled_tool", description="x")])

    result = middleware.wrap_model_call(request, lambda value: value)

    assert result is request
    assert registry.list_tools() == ["assembled_tool"]


def test_explicit_service_principals_are_separate_and_frozen() -> None:
    scheduler = get_service_principal("scheduled_tick")
    poller = get_service_principal("poller")
    synthesis = get_service_principal("saga_session_end")
    system = get_service_principal("upgrade")

    assert scheduler is not None and scheduler.canonical == "scheduler"
    assert poller is not None and poller.canonical == "poller"
    assert synthesis is not None and synthesis.canonical == "synthesis"
    assert system is not None and system.canonical == "system"
    assert len({scheduler.canonical, poller.canonical, synthesis.canonical, system.canonical}) == 4
    with pytest.raises(FrozenInstanceError):
        scheduler.trigger = "forged"


def test_service_principal_can_use_only_inventoried_runtime_tools() -> None:
    registry = ToolRegistry()
    registry.register_tool("shell_exec")

    admitted = registry.authorize_tool(
        "shell_exec", _auth_context(trigger="scheduled_tick", enforce=True), enforce=True
    )
    unregistered = registry.authorize_tool(
        "spawn_codex", _auth_context(trigger="scheduled_tick", enforce=True), enforce=True
    )
    unknown_trigger = registry.authorize_tool(
        "shell_exec", _auth_context(trigger="unknown_synthetic", enforce=True), enforce=True
    )
    forged_http_trigger = registry.authorize_tool(
        "shell_exec",
        _auth_context(trigger="scheduled_tick", enforce=True, event_ingress="http-api"),
        enforce=True,
    )

    assert admitted.allowed is True
    assert admitted.is_shadow_decision is False
    assert admitted.service_principal is get_service_principal("scheduled_tick")
    assert unregistered.allowed is False
    assert unknown_trigger.allowed is False
    assert unknown_trigger.service_principal is None
    assert forged_http_trigger.allowed is False
    assert forged_http_trigger.service_principal is None


def test_service_principal_capability_helpers_are_exact() -> None:
    principal = ServicePrincipal(
        canonical="test",
        trigger="test",
        capabilities=("one",),
        readable_domains=("domain",),
        sink_destinations=("sink",),
    )

    assert principal.has_capability("one")
    assert not principal.has_capability("two")
    assert principal.can_read_domain("domain")
    assert not principal.can_read_domain("other")
    assert principal.can_write_sink("sink")
    assert not principal.can_write_sink("other")
