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


def test_budget_middleware_publishes_final_runtime_inventory_per_model_call() -> None:
    registry = budget_gate.get_tool_registry()
    registry.register_runtime_tools(
        [SimpleNamespace(name="existing_tool", description="existing")]
    )
    middleware = budget_gate.BudgetGateMiddleware()
    request = SimpleNamespace(
        tools=[SimpleNamespace(name="narrow_surface_tool", description="narrow")]
    )

    result = middleware.wrap_model_call(request, lambda value: value)

    assert result is request
    assert registry.list_tools() == ["narrow_surface_tool"]
    assert registry.get_tool("narrow_surface_tool")["description"] == "narrow"


@pytest.mark.asyncio
async def test_budget_middleware_publishes_inventory_on_async_model_path() -> None:
    registry = budget_gate.get_tool_registry()
    middleware = budget_gate.BudgetGateMiddleware()
    request = SimpleNamespace(
        tools=[SimpleNamespace(name="async_surface_tool", description="async")]
    )

    async def handler(value: object) -> object:
        return value

    result = await middleware.awrap_model_call(request, handler)

    assert result is request
    assert registry.list_tools() == ["async_surface_tool"]


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


@pytest.mark.parametrize(
    ("trigger", "allowed_operation", "denied_operation"),
    [
        ("scheduled_tick", "shell_exec", "request_mimir_update"),
        ("scheduled_tick", "read_file", "request_mimir_update"),
        ("poller", "worklink_run", "remove_schedule"),
        ("poller", "write_file", "remove_schedule"),
        ("upgrade", "submit_proposal", "spawn_codex"),
        ("upgrade", "read_file", "spawn_codex"),
        ("saga_session_end", "saga_end_session", "spawn_codex"),
        ("saga_session_end", "read_file", "spawn_codex"),
        ("saga_session_end", "memory_get", "spawn_codex"),
    ],
)
def test_service_principals_allow_only_explicit_operations_with_full_inventory(
    trigger: str,
    allowed_operation: str,
    denied_operation: str,
) -> None:
    registry = ToolRegistry()
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )

    admitted = registry.authorize_tool(
        allowed_operation, _auth_context(trigger=trigger, enforce=True), enforce=True
    )
    denied = registry.authorize_tool(
        denied_operation, _auth_context(trigger=trigger, enforce=True), enforce=True
    )

    assert admitted.allowed is True
    assert admitted.is_shadow_decision is False
    assert admitted.service_principal is get_service_principal(trigger)
    assert denied.allowed is False
    assert denied.reason == "admin_required"
    assert denied.service_principal is get_service_principal(trigger)


def test_service_authorization_is_stable_under_inventory_mutation_and_surface_width() -> None:
    registry = ToolRegistry()
    scheduler = _auth_context(trigger="scheduled_tick", enforce=True)

    before = registry.authorize_tool("shell_exec", scheduler, enforce=True)
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )
    with_full_surface = registry.authorize_tool("shell_exec", scheduler, enforce=True)
    denied_with_full_surface = registry.authorize_tool(
        "request_mimir_update", scheduler, enforce=True
    )
    registry.clear()
    registry.register_runtime_tools([SimpleNamespace(name="send_message")])
    with_narrow_surface = registry.authorize_tool("shell_exec", scheduler, enforce=True)
    denied_with_narrow_surface = registry.authorize_tool(
        "request_mimir_update", scheduler, enforce=True
    )

    assert before.allowed is True
    assert with_full_surface.allowed is True
    assert with_narrow_surface.allowed is True
    assert denied_with_full_surface.allowed is False
    assert denied_with_narrow_surface.allowed is False


def test_unknown_and_http_triggers_cannot_inherit_service_capabilities() -> None:
    registry = ToolRegistry()
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )

    unknown_trigger = registry.authorize_tool(
        "shell_exec", _auth_context(trigger="unknown_synthetic", enforce=True), enforce=True
    )
    forged_http_trigger = registry.authorize_tool(
        "shell_exec",
        _auth_context(trigger="scheduled_tick", enforce=True, event_ingress="http-api"),
        enforce=True,
    )

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


def test_capability_matrix_preflight_check_passes_with_complete_matrix() -> None:
    from mimir.access_control import check_capability_matrix_complete

    is_complete, errors = check_capability_matrix_complete(fail_closed=True)
    assert is_complete is True
    assert errors == []


def test_capability_matrix_preflight_fails_with_incomplete_matrix() -> None:
    from mimir.access_control import (
        ServicePrincipal,
        _TRUSTED_SERVICE_PRINCIPALS,
        check_capability_matrix_complete,
    )

    original_principals = _TRUSTED_SERVICE_PRINCIPALS.copy()
    try:
        _TRUSTED_SERVICE_PRINCIPALS["scheduled_tick"] = ServicePrincipal(
            canonical="scheduler",
            trigger="scheduled_tick",
            capabilities=(),
            readable_domains=(),
            sink_destinations=(),
        )

        is_complete, errors = check_capability_matrix_complete(fail_closed=True)
        assert is_complete is False
        assert len(errors) > 0
        assert any("no capabilities" in e for e in errors)
    finally:
        _TRUSTED_SERVICE_PRINCIPALS.clear()
        _TRUSTED_SERVICE_PRINCIPALS.update(original_principals)


def test_enforcement_enablement_fails_closed_with_incomplete_matrix() -> None:
    from mimir.access_control import (
        CapabilityMatrixError,
        ServicePrincipal,
        _TRUSTED_SERVICE_PRINCIPALS,
        resolve_access_control_enforcement,
    )

    original_principals = _TRUSTED_SERVICE_PRINCIPALS.copy()
    try:
        scheduler = original_principals["scheduled_tick"]
        _TRUSTED_SERVICE_PRINCIPALS["scheduled_tick"] = ServicePrincipal(
            canonical=scheduler.canonical,
            trigger=scheduler.trigger,
            capabilities=scheduler.capabilities,
            readable_domains=tuple(
                domain for domain in scheduler.readable_domains
                if domain != "filesystem"
            ),
            sink_destinations=scheduler.sink_destinations,
            creation_path=scheduler.creation_path,
        )

        with pytest.raises(CapabilityMatrixError, match="read_file.*filesystem"):
            resolve_access_control_enforcement(True)
        assert resolve_access_control_enforcement(False) is False
    finally:
        _TRUSTED_SERVICE_PRINCIPALS.clear()
        _TRUSTED_SERVICE_PRINCIPALS.update(original_principals)


def test_capability_matrix_report_generates_complete_report() -> None:
    from mimir.access_control import get_capability_matrix_report

    report = get_capability_matrix_report()

    assert "scheduled_tick" in report
    assert "poller" in report
    assert "saga_session_end" in report
    assert "upgrade" in report

    for trigger, principal in report.items():
        assert "canonical" in principal
        assert "capabilities" in principal
        assert "readable_domains" in principal
        assert "sink_destinations" in principal
        assert "creation_path" in principal
        assert len(principal["capabilities"]) > 0


def test_scheduler_principal_has_required_capabilities_for_heartbeat() -> None:
    """Verify scheduler principal has all capabilities needed for heartbeat workflow.

    Based on heartbeat.md production prompt:
    - Reads memory/core files (read_file, ls, glob)
    - Checks drift by reading files
    - Reads backlog files
    - Uses shell for jq analysis (shell_exec, bash_async)
    - Writes/edits files (write_file, edit_file)
    """
    scheduler = get_service_principal("scheduled_tick")
    assert scheduler is not None

    read_ops = {"read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep", "file_search", "get_turn", "mimir_get_turn"}
    write_ops = {"write_file", "edit_file"}
    shell_ops = {"shell_exec", "bash_async"}
    spawn_ops = {"spawn_claude_code", "spawn_codex"}
    proposal_ops = {"open_proposal", "submit_proposal", "abandon_proposal"}
    saga_ops = {"saga_forget"}
    worklink_ops = {"worklink_run"}

    all_expected = read_ops | write_ops | shell_ops | spawn_ops | proposal_ops | saga_ops | worklink_ops
    for cap in all_expected:
        assert scheduler.has_capability(cap), f"scheduler missing {cap}"

    forbidden = {"request_mimir_update", "remove_schedule", "reload_pollers"}
    for cap in forbidden:
        assert not scheduler.has_capability(cap), f"scheduler should NOT have {cap}"


def test_poller_principal_has_required_capabilities() -> None:
    """Verify poller principal has all capabilities needed for poller workflow.

    Based on poller production:
    - Reads poller payloads (read_file, ls, glob)
    - Uses shell for analysis (shell_exec, bash_async)
    - Writes results (write_file, edit_file)
    - Sends messages to channels (send_message)
    """
    poller = get_service_principal("poller")
    assert poller is not None

    read_ops = {"read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep", "file_search", "get_turn", "mimir_get_turn"}
    write_ops = {"write_file", "edit_file"}
    shell_ops = {"shell_exec", "bash_async"}
    spawn_ops = {"spawn_claude_code", "spawn_codex"}
    proposal_ops = {"open_proposal", "submit_proposal", "abandon_proposal"}
    message_ops = {"send_message"}
    worklink_ops = {"worklink_run"}

    all_expected = read_ops | write_ops | shell_ops | spawn_ops | proposal_ops | message_ops | worklink_ops
    for cap in all_expected:
        assert poller.has_capability(cap), f"poller missing {cap}"

    forbidden = {"remove_schedule", "reload_pollers", "add_schedule", "set_schedule_priority"}
    for cap in forbidden:
        assert not poller.has_capability(cap), f"poller should NOT have {cap}"


def test_synthesis_principal_has_required_capabilities_for_session_end() -> None:
    """Verify synthesis principal has all capabilities needed for saga_session_end workflow.

    Based on saga_session_end.md production prompt:
    - Gets turn content (mimir_get_turn, get_turn)
    - Reads files for memory capture (read_file, ls, glob)
    - Writes/edits memory files (write_file, edit_file)
    - Uses shell for file ops (shell_exec)
    - Stores atoms (memory_store)
    - Gets atoms by ID (memory_get)
    - Records feedback (saga_feedback)
    - Records skill learnings (saga_record_skill_learning)
    - Ends session (saga_end_session)
    - Marks contributions (saga_mark_contributions)
    """
    synthesis = get_service_principal("saga_session_end")
    assert synthesis is not None

    turn_ops = {"mimir_get_turn", "get_turn"}
    read_ops = {"read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep"}
    write_ops = {"write_file", "edit_file"}
    memory_ops = {"memory_get", "memory_store"}
    saga_ops = {"saga_feedback", "saga_end_session", "saga_mark_contributions", "saga_record_skill_learning"}

    all_expected = turn_ops | read_ops | write_ops | memory_ops | saga_ops
    for cap in all_expected:
        assert synthesis.has_capability(cap), f"synthesis missing {cap}"

    forbidden = {
        "shell_exec",
        "bash_async",
        "spawn_claude_code",
        "spawn_codex",
        "add_schedule",
        "remove_schedule",
        "send_message",
    }
    for cap in forbidden:
        assert not synthesis.has_capability(cap), f"synthesis should NOT have {cap}"


def test_system_principal_has_required_capabilities_for_upgrade() -> None:
    """Verify system principal has all capabilities needed for upgrade workflow.

    Based on upgrade.md production prompt:
    - Reads changed files (read_file, ls, glob)
    - Uses shell for git commands (shell_exec, bash_async)
    - Writes/edits files (write_file, edit_file)
    - Submits proposals (submit_proposal)
    - Sends operator notifications (send_message)
    - Manages schedules (add_schedule, set_schedule_priority)
    """
    system = get_service_principal("upgrade")
    assert system is not None

    read_ops = {"read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep"}
    write_ops = {"write_file", "edit_file"}
    shell_ops = {"shell_exec", "bash_async"}
    proposal_ops = {"open_proposal", "submit_proposal", "abandon_proposal"}
    schedule_ops = {"add_schedule", "set_schedule_priority"}
    message_ops = {"send_message"}

    all_expected = read_ops | write_ops | shell_ops | proposal_ops | schedule_ops | message_ops
    for cap in all_expected:
        assert system.has_capability(cap), f"system missing {cap}"

    forbidden = {"spawn_claude_code", "spawn_codex", "worklink_run", "saga_forget"}
    for cap in forbidden:
        assert not system.has_capability(cap), f"system should NOT have {cap}"


def test_adjacent_unauthorized_operations_deny_for_each_principal() -> None:
    """Verify that unauthorized operations are denied for each service principal.

    Tests boundary: each principal should only be able to use its defined
    capabilities, and adjacent unauthorized operations should be denied.
    """
    registry = ToolRegistry()

    test_cases = [
        ("scheduled_tick", "shell_exec", True),
        ("scheduled_tick", "remove_schedule", False),
        ("scheduled_tick", "reload_pollers", False),
        ("poller", "send_message", True),
        ("poller", "add_schedule", False),
        ("saga_session_end", "saga_end_session", True),
        ("saga_session_end", "spawn_claude_code", False),
        ("saga_session_end", "add_schedule", False),
        ("upgrade", "submit_proposal", True),
        ("upgrade", "spawn_codex", False),
        ("upgrade", "worklink_run", False),
    ]

    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    for trigger, operation, should_allow in test_cases:
        event = AgentEvent(trigger=trigger, channel_id=f"{trigger}:test")
        ctx = create_auth_context(event, enforce=True)
        result = registry.authorize_tool(operation, ctx, enforce=True)

        if should_allow:
            assert result.allowed is True, f"{trigger} should allow {operation}"
        else:
            assert result.allowed is False, f"{trigger} should deny {operation}"
            assert result.reason in ("admin_required", "unknown_operation"), f"{trigger} {operation} denied for wrong reason: {result.reason}"
