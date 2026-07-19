from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mimir.access_control import (
    HTTP_EVENT_INGRESS_EXTRA_KEY,
    OperationCatalog,
    OperationDecision,
    ResourceScope,
    ServicePrincipal,
    ToolRegistry,
    get_service_principal,
)
from mimir.models import AuthContext, InformationFlowLabels, SourceLabel
from mimir.tools import budget_gate
from mimir.tools.extra import shell_exec
from mimir.tools.shell_async import bash_async


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
    "spawn_open_code",
    "task",
    "saga_forget",
    "write_file",
    "edit_file",
}

_NEWLY_CATALOGED_TOOLS = {
    "memory_store": OperationDecision.ADMIN_REQUIRED,
    "memory_query": OperationDecision.OPEN,
    "memory_get": OperationDecision.OPEN,
    "web_search": OperationDecision.OPEN,
    "fetch_url": OperationDecision.OPEN,
    "saga_feedback": OperationDecision.ADMIN_REQUIRED,
    "saga_mark_contributions": OperationDecision.ADMIN_REQUIRED,
    "saga_end_session": OperationDecision.ADMIN_REQUIRED,
    "saga_record_skill_learning": OperationDecision.ADMIN_REQUIRED,
    "bash_jobs_list": OperationDecision.ADMIN_REQUIRED,
    "bash_job_output": OperationDecision.ADMIN_REQUIRED,
    "write_todos": OperationDecision.OPEN,
    "defer_injected_message": OperationDecision.OPEN,
    "commitment_complete": OperationDecision.OPEN,
    "commitment_snooze": OperationDecision.OPEN,
    "commitment_dismiss": OperationDecision.OPEN,
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


def _service_labels(event) -> InformationFlowLabels:
    principal = f"service:{event.service_principal}" if event.service_principal else None
    return InformationFlowLabels(
        labels=frozenset({"internal"}),
        source_channels=frozenset({event.channel_id}) if event.channel_id else frozenset(),
        sources=frozenset({SourceLabel(
            principal=principal,
            domain="channel",
            resource_id=event.channel_id,
            bridge_instance=event.source,
            sensitivity="internal",
            authorized_principals=frozenset({principal}) if principal else frozenset(),
        )}),
    )


@pytest.mark.parametrize("shell_tool", [shell_exec, bash_async])
def test_direct_argv_is_hidden_from_model_tool_schema(shell_tool) -> None:
    properties = shell_tool.tool_call_schema.model_json_schema()["properties"]

    assert "mimir_direct_argv" not in properties


def test_admin_catalog_never_shrinks_and_preserves_mcp_suffixes() -> None:
    catalog = OperationCatalog()

    assert _OLD_ADMIN_TOOLS <= catalog._ADMIN_REQUIRED_OPERATIONS
    assert catalog.get_decision("mcp__mimir__shell_exec") == OperationDecision.ADMIN_REQUIRED
    assert catalog.get_decision("mcp_mimir_shell_exec") == OperationDecision.ADMIN_REQUIRED


def test_open_catalog_excludes_protected_global_metadata() -> None:
    catalog = OperationCatalog()

    assert not (
        catalog._OPEN_OPERATIONS & catalog._PROTECTED_METADATA_OPERATIONS
    )
    assert catalog._PROTECTED_METADATA_OPERATIONS <= catalog._ADMIN_REQUIRED_OPERATIONS
    assert {
        catalog.get_decision("bash_jobs_list"),
        catalog.get_decision("bash_job_output"),
    } == {OperationDecision.ADMIN_REQUIRED}


@pytest.mark.parametrize(
    ("operation", "expected"),
    _NEWLY_CATALOGED_TOOLS.items(),
)
def test_routine_tools_have_explicit_catalog_decisions(
    operation: str,
    expected: OperationDecision,
) -> None:
    assert OperationCatalog().get_decision(operation) == expected


@pytest.mark.parametrize("tool_name", ["web_search", "fetch_url"])
@pytest.mark.parametrize("roles", [("user",), ("user", "admin")])
def test_web_tools_are_open_but_remain_network_sinks_when_enforced(
    tool_name: str,
    roles: tuple[str, ...],
) -> None:
    from mimir.access_control import SinkCategory, get_sink_category

    result = ToolRegistry().authorize_tool(
        tool_name,
        _auth_context(roles=roles, enforce=True),
        enforce=True,
        target_channel="https://external.example",
        ifc_labels=InformationFlowLabels(),
    )

    assert result.allowed is True
    assert result.decision == OperationDecision.OPEN
    assert get_sink_category(tool_name) == SinkCategory.NETWORK


def test_web_search_ifc_target_is_the_configured_search_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools.budget_gate import _extract_sink_target

    monkeypatch.setenv("TAVILY_SEARCH_URL", "https://search.example/api")
    request = SimpleNamespace(tool_call={"name": "web_search", "args": {"query": "mimir"}})

    assert _extract_sink_target(request) == "https://search.example/api"


@pytest.mark.parametrize("operation", ["spawn_open_code", "task"])
def test_factory_operations_are_cataloged_as_admin_required(operation: str) -> None:
    catalog = OperationCatalog()

    assert catalog.get_decision(operation) == OperationDecision.ADMIN_REQUIRED


def test_spawn_open_code_declares_spawn_sink_destination() -> None:
    from mimir.access_control import _OPERATION_SINK_DESTINATION

    assert _OPERATION_SINK_DESTINATION["spawn_open_code"] == "spawn_process"


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("spawn_open_code", False), ("task", True)],
)
def test_scheduler_service_principal_can_invoke_factory_operations(
    operation: str,
    expected: bool,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    auth = create_auth_context(
        event, enforce=True, ifc_labels=_service_labels(event),
    )

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    decision = ToolRegistry().authorize_tool(
        operation,
        auth,
        enforce=True,
        target_channel=str(tmp_path) if operation == "spawn_open_code" else None,
    )

    assert decision.allowed is expected
    assert decision.decision == OperationDecision.ADMIN_REQUIRED
    assert decision.reason == (None if expected else "ifc_label_blocked:spawn")
    assert decision.service_principal is get_service_principal("scheduled_tick")


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


@pytest.mark.parametrize("operation", ["memory_store", "shell_exec"])
def test_register_operation_refuses_protected_downgrade(operation: str) -> None:
    catalog = OperationCatalog()

    with pytest.raises(ValueError, match="cannot downgrade protected operation"):
        catalog.register_operation(operation, OperationDecision.OPEN)

    assert catalog.get_decision(operation) == OperationDecision.ADMIN_REQUIRED


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
    assert poller is None  # pollers exist only as per-instance carried grants
    assert synthesis is not None and synthesis.canonical == "synthesis"
    assert system is not None and system.canonical == "system"
    assert len({scheduler.canonical, synthesis.canonical, system.canonical}) == 3
    with pytest.raises(FrozenInstanceError):
        scheduler.trigger = "forged"


@pytest.mark.parametrize(
    ("trigger", "allowed_operation", "denied_operation", "ifc_allows"),
    [
        ("scheduled_tick", "shell_exec", "request_mimir_update", True),
        ("scheduled_tick", "read_file", "request_mimir_update", True),
        ("upgrade", "submit_proposal", "spawn_codex", True),
        ("upgrade", "read_file", "spawn_codex", True),
        ("saga_session_end", "saga_end_session", "spawn_codex", True),
        ("saga_session_end", "read_file", "spawn_codex", True),
        ("saga_session_end", "memory_get", "spawn_codex", True),
    ],
)
def test_service_principals_allow_only_explicit_operations_and_compatible_flows(
    trigger: str,
    allowed_operation: str,
    denied_operation: str,
    ifc_allows: bool,
) -> None:
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    registry = ToolRegistry()
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )

    service_principals = {
        "scheduled_tick": "scheduler",
        "saga_session_end": "synthesis",
        "upgrade": "system",
    }

    event = AgentEvent(
        trigger=trigger,
        channel_id=f"{trigger}:test",
        service_principal=service_principals.get(trigger),
    )
    labels = InformationFlowLabels(
        labels=frozenset({"internal"}),
        source_channels=frozenset({event.channel_id}),
    )
    ctx = create_auth_context(event, enforce=True, ifc_labels=labels)

    admitted = registry.authorize_tool(
        allowed_operation,
        ctx,
        enforce=True,
        target_channel=(
            "git status" if allowed_operation == "shell_exec" else f"{trigger}:target"
        ),
    )
    denied = registry.authorize_tool(denied_operation, ctx, enforce=True)

    assert admitted.allowed is ifc_allows
    assert admitted.is_shadow_decision is False
    if ifc_allows:
        assert admitted.service_principal is get_service_principal(trigger)
    else:
        blocked_category = "spawn" if allowed_operation == "worklink_run" else "file"
        assert admitted.reason == f"ifc_label_blocked:{blocked_category}"
    assert denied.allowed is False
    assert denied.reason == "admin_required"
    assert denied.service_principal is get_service_principal(trigger)


def test_service_authorization_is_stable_under_inventory_mutation_and_surface_width() -> None:
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    registry = ToolRegistry()
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    scheduler = create_auth_context(event, enforce=True, ifc_labels=_service_labels(event))

    before = registry.authorize_tool(
        "shell_exec", scheduler, enforce=True, target_channel="git status"
    )
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )
    with_full_surface = registry.authorize_tool(
        "shell_exec", scheduler, enforce=True, target_channel="git status"
    )
    denied_with_full_surface = registry.authorize_tool(
        "request_mimir_update", scheduler, enforce=True
    )
    registry.clear()
    registry.register_runtime_tools([SimpleNamespace(name="send_message")])
    with_narrow_surface = registry.authorize_tool(
        "shell_exec", scheduler, enforce=True, target_channel="git status"
    )
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


def test_capability_matrix_rejects_saga_mutation_without_sink_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    sinks = access_control._OPERATION_SINK_DESTINATION.copy()
    sinks.pop("memory_store")
    monkeypatch.setattr(access_control, "_OPERATION_SINK_DESTINATION", sinks)

    is_complete, errors = access_control.check_capability_matrix_complete()

    assert is_complete is False
    assert "SAGA mutation 'memory_store' has no sink destination mapping" in errors


def test_capability_matrix_rejects_declared_sink_without_ifc_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    categories = access_control._SINK_CATEGORY_MAP.copy()
    categories.pop("memory_store")
    monkeypatch.setattr(access_control, "_SINK_CATEGORY_MAP", categories)

    is_complete, errors = access_control.check_capability_matrix_complete()

    assert is_complete is False
    assert "Sink operation 'memory_store' has no IFC sink category mapping" in errors


def test_capability_matrix_rejects_open_saga_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    monkeypatch.setattr(
        access_control.OperationCatalog,
        "_OPEN_OPERATIONS",
        access_control.OperationCatalog._OPEN_OPERATIONS | {"memory_store"},
    )

    is_complete, errors = access_control.check_capability_matrix_complete()

    assert is_complete is False
    assert "SAGA mutation 'memory_store' must not be cataloged OPEN" in errors


def test_capability_matrix_rejects_custom_saga_mutation_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    monkeypatch.setitem(
        access_control._global_operation_catalog._custom_decisions,
        "memory_store",
        OperationDecision.OPEN,
    )

    is_complete, errors = access_control.check_capability_matrix_complete()

    assert is_complete is False
    assert "SAGA mutation 'memory_store' must not be cataloged OPEN" in errors


def test_capability_matrix_rejects_service_sink_without_executable_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control
    from dataclasses import replace

    scheduler = access_control._TRUSTED_SERVICE_PRINCIPALS["scheduled_tick"]
    monkeypatch.setitem(
        access_control._TRUSTED_SERVICE_PRINCIPALS,
        "scheduled_tick",
        replace(
            scheduler,
            sink_policies=tuple(
                policy
                for policy in scheduler.sink_policies
                if policy.operation != "shell_exec"
            ),
        ),
    )

    is_complete, errors = access_control.check_capability_matrix_complete()

    assert is_complete is False
    assert any(
        "shell_exec' has no executable destination policy" in error
        for error in errors
    )


def test_capability_matrix_rejects_unbacked_sink_category_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control
    from dataclasses import replace

    synthesis = access_control._TRUSTED_SERVICE_PRINCIPALS["saga_session_end"]
    monkeypatch.setitem(
        access_control._TRUSTED_SERVICE_PRINCIPALS,
        "saga_session_end",
        replace(
            synthesis,
            sink_destinations=synthesis.sink_destinations + ("network",),
        ),
    )

    is_complete, errors = access_control.check_capability_matrix_complete()

    assert is_complete is False
    assert any(
        "sink destination 'network' has no executable destination policy" in error
        for error in errors
    )


@pytest.mark.parametrize("fail_closed", [True, False])
def test_capability_matrix_reports_seeded_errors_for_both_modes(
    fail_closed: bool,
) -> None:
    from mimir.access_control import (
        ServicePrincipal,
        _TRUSTED_SERVICE_PRINCIPALS,
        check_capability_matrix_complete,
    )

    original_principals = _TRUSTED_SERVICE_PRINCIPALS.copy()
    try:
        scheduler = original_principals["scheduled_tick"]
        _TRUSTED_SERVICE_PRINCIPALS["scheduled_tick"] = ServicePrincipal(
            canonical=scheduler.canonical,
            trigger=scheduler.trigger,
            capabilities=(),
            readable_domains=scheduler.readable_domains,
            sink_destinations=scheduler.sink_destinations,
            creation_path=scheduler.creation_path,
        )

        is_complete, errors = check_capability_matrix_complete(
            fail_closed=fail_closed,
        )

        assert is_complete is False
        assert any("no capabilities" in error for error in errors)
    finally:
        _TRUSTED_SERVICE_PRINCIPALS.clear()
        _TRUSTED_SERVICE_PRINCIPALS.update(original_principals)


@pytest.mark.asyncio
async def test_sink_gate_denial_emits_shadow_decision_when_not_enforced() -> None:
    from mimir.access_control import AccessTier, SinkGate, ToolAuthorization

    registry = ToolRegistry()
    registry.enable_shadow_logging()
    captured: list[tuple[str, dict[str, object]]] = []
    sink_denial = ToolAuthorization(
        tool_name="write_file",
        decision=OperationDecision.ADMIN_REQUIRED,
        allowed=True,
        reason="ifc_label_blocked:file",
        required_tier=AccessTier.ADMIN,
        enforcement_enabled=False,
        is_shadow_decision=True,
    )

    async def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    with (
        patch.object(SinkGate, "check_sink_flow", return_value=sink_denial),
        patch("mimir.event_logger.log_event", new=capture),
    ):
        decision = registry.authorize_tool(
            "write_file",
            enforce=False,
            target_channel="/tmp/example",
            ifc_labels=object(),
        )
        await asyncio.sleep(0)

    assert decision.allowed is True
    assert any(
        kind == "shadow_tool_decision"
        and fields["reason"] == "ifc_label_blocked:file"
        for kind, fields in captured
    )


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


def test_enforcement_enablement_rejects_uncataloged_model_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.access_control import (
        CapabilityMatrixError,
        resolve_access_control_enforcement,
    )

    monkeypatch.setattr(
        "mimir.tools.registry.all_mimir_tools",
        lambda model_spec=None: [SimpleNamespace(name="deliberately_uncataloged")],
    )

    with pytest.raises(
        CapabilityMatrixError,
        match="UNKNOWN model-bound tools: deliberately_uncataloged",
    ):
        resolve_access_control_enforcement(True, model_spec="openai:test")


def test_enforcement_enablement_rejects_cataloged_tool_without_flow_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    name = "deliberately_uncataloged_flow_mutation"
    monkeypatch.setitem(
        access_control._global_operation_catalog._custom_decisions,
        name,
        OperationDecision.ADMIN_REQUIRED,
    )
    monkeypatch.setattr(
        "mimir.tools.registry.all_mimir_tools",
        lambda model_spec=None: [SimpleNamespace(name=name)],
    )

    with pytest.raises(
        access_control.CapabilityMatrixError,
        match=f"without explicit IFC flow metadata: {name}",
    ):
        access_control.resolve_access_control_enforcement(
            True, model_spec="openai:test"
        )


@pytest.mark.parametrize(
    ("tool_name", "args", "expected"),
    [
        ("remove_schedule", {"name": "daily"}, "scheduler:job:daily"),
        ("reload_pollers", {}, "scheduler:pollers"),
        ("commitment_snooze", {"commitment_id": "c-1"}, "commitment:c-1"),
        ("rebuild_index", {"scope": "MEMORY"}, "index:memory"),
    ],
)
def test_mutation_sink_destinations_are_normalized(
    tool_name: str,
    args: dict[str, object],
    expected: str,
) -> None:
    from mimir.tools.budget_gate import _extract_sink_target

    request = SimpleNamespace(tool_call={"name": tool_name, "args": args})
    assert _extract_sink_target(request) == expected


@pytest.mark.parametrize(
    "model_spec",
    ["claude-code:claude-sonnet-4-6", "claude_code:claude-sonnet-4-6"],
)
def test_enforcement_enablement_rejects_claude_code_provider(
    model_spec: str,
) -> None:
    from mimir.access_control import (
        ProviderEnforcementCompatibilityError,
        resolve_access_control_enforcement,
    )

    with pytest.raises(
        ProviderEnforcementCompatibilityError,
        match="claude-code subprocess.*per-turn AuthContext",
    ):
        resolve_access_control_enforcement(True, model_spec=model_spec)


def test_capability_matrix_report_generates_complete_report() -> None:
    from mimir.access_control import get_capability_matrix_report

    report = get_capability_matrix_report()

    assert "scheduled_tick" in report
    assert "poller" not in report
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
    - Inspects async shell job status/output
    - Writes/edits files (write_file, edit_file)
    """
    scheduler = get_service_principal("scheduled_tick")
    assert scheduler is not None

    read_ops = {"read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep", "file_search", "get_turn", "mimir_get_turn"}
    write_ops = {"write_file", "edit_file"}
    shell_ops = {"shell_exec", "bash_async", "bash_jobs_list", "bash_job_output"}
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
    """There is no class-wide poller grant to inherit or widen."""
    assert get_service_principal("poller") is None


def test_synthesis_principal_has_required_capabilities_for_session_end() -> None:
    """Verify synthesis principal has all capabilities needed for saga_session_end workflow.

    Based on saga_session_end.md production prompt:
    - Gets turn content (mimir_get_turn, get_turn)
    - Reads files for memory capture (read_file, ls, glob)
    - Cannot write arbitrary files or invoke shell
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
    memory_ops = {"memory_get", "memory_store"}
    saga_ops = {"saga_feedback", "saga_end_session", "saga_mark_contributions", "saga_record_skill_learning"}

    all_expected = turn_ops | read_ops | memory_ops | saga_ops
    for cap in all_expected:
        assert synthesis.has_capability(cap), f"synthesis missing {cap}"
    assert not synthesis.has_capability("write_file")
    assert not synthesis.has_capability("edit_file")

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
    - Lists schedules before and after migration changes (list_schedules)
    """
    system = get_service_principal("upgrade")
    assert system is not None

    read_ops = {"read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep"}
    write_ops = {"write_file", "edit_file"}
    shell_ops = {"shell_exec", "bash_async"}
    proposal_ops = {"open_proposal", "submit_proposal", "abandon_proposal"}
    schedule_ops = {"add_schedule", "set_schedule_priority", "list_schedules"}
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
        ("saga_session_end", "saga_end_session", True),
        ("saga_session_end", "spawn_claude_code", False),
        ("saga_session_end", "add_schedule", False),
        ("upgrade", "submit_proposal", True),
        ("upgrade", "spawn_codex", False),
        ("upgrade", "worklink_run", False),
    ]

    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    service_principals = {
        "scheduled_tick": "scheduler",
        "saga_session_end": "synthesis",
        "upgrade": "system",
    }

    for trigger, operation, should_allow in test_cases:
        event = AgentEvent(
            trigger=trigger,
            channel_id=f"{trigger}:test",
            service_principal=service_principals.get(trigger),
            source=trigger,
        )
        ctx = create_auth_context(event, enforce=True, ifc_labels=_service_labels(event))
        result = registry.authorize_tool(
            operation,
            ctx,
            enforce=True,
            target_channel="git status" if operation == "shell_exec" else None,
        )

        if should_allow:
            assert result.allowed is True, f"{trigger} should allow {operation}"
        else:
            assert result.allowed is False, f"{trigger} should deny {operation}"
            assert result.reason in ("admin_required", "unknown_operation"), f"{trigger} {operation} denied for wrong reason: {result.reason}"


def test_service_authorization_requires_two_factor_validation() -> None:
    """Verify that service authorization requires both is_service=True AND canonical_principal match.

    This tests the fix for the security issue where authorize_tool granted service
    capabilities based only on trigger matching, without validating the full two-factor
    proof (is_service + canonical_principal match).
    """
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    registry = ToolRegistry()
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )

    event_correct = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    ctx_correct = create_auth_context(event_correct, enforce=True, ifc_labels=_service_labels(event_correct))
    result_correct = registry.authorize_tool(
        "shell_exec", ctx_correct, enforce=True, target_channel="git status"
    )
    assert result_correct.allowed is True, "Correctly-stamped service should get capabilities"
    assert result_correct.service_principal is not None
    assert result_correct.service_principal.canonical == "scheduler"

    event_wrong_principal = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="wrong_principal",
    )
    ctx_wrong_principal = create_auth_context(event_wrong_principal, enforce=True, ifc_labels=_service_labels(event_wrong_principal))
    result_wrong_principal = registry.authorize_tool("shell_exec", ctx_wrong_principal, enforce=True)
    assert result_wrong_principal.allowed is False, "Mismatched service_principal should be denied"
    assert result_wrong_principal.reason == "admin_required"

    event_no_service_principal = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
    )
    ctx_no_service_principal = create_auth_context(event_no_service_principal, enforce=True, ifc_labels=_service_labels(event_no_service_principal))
    result_no_service_principal = registry.authorize_tool("shell_exec", ctx_no_service_principal, enforce=True)
    assert result_no_service_principal.allowed is False, "Missing service_principal should be denied"
    assert result_no_service_principal.reason == "admin_required"

    event_http_ingress = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
    )
    ctx_http_ingress = create_auth_context(event_http_ingress, enforce=True, ifc_labels=_service_labels(event_http_ingress))
    result_http_ingress = registry.authorize_tool("shell_exec", ctx_http_ingress, enforce=True)
    assert result_http_ingress.allowed is False, "HTTP ingress should be denied even with correct service_principal"
    assert result_http_ingress.reason == "admin_required"


def test_service_authorization_shadow_mode_emits_decision_event() -> None:
    """Verify that service-capability grants in shadow mode emit decision events.

    This ensures the enforcement-off audit is not blind to the service path.
    """
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    registry = ToolRegistry()
    registry.register_runtime_tools(
        [SimpleNamespace(name=name, description=name) for name in sorted(_OLD_ADMIN_TOOLS)]
    )

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    ctx = create_auth_context(event, enforce=False, ifc_labels=_service_labels(event))
    result = registry.authorize_tool("shell_exec", ctx, enforce=False)
    assert result.allowed is True
    assert result.is_shadow_decision is True, "Service capability grant in shadow mode should emit shadow decision"


def test_commitment_actor_requires_two_factor_validation() -> None:
    """Verify that _commitment_actor requires two-factor validation for service authority.

    This tests the fix for the security issue where _commitment_actor granted service
    authority based only on trigger matching, without validating the full two-factor
    proof (is_service + canonical_principal match).
    """
    from mimir.tools.registry import _commitment_actor
    from mimir.access_control import create_auth_context
    from mimir.models import AgentEvent

    class MockRuntime:
        def __init__(self, context):
            self.context = context

    event_correct = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    ctx_correct = create_auth_context(event_correct, enforce=True, ifc_labels=_service_labels(event_correct))
    runtime_correct = MockRuntime(ctx_correct)
    actor_correct = _commitment_actor(runtime_correct)
    assert actor_correct is not None
    assert actor_correct[0] == "service:scheduler"
    assert actor_correct[2] is True

    event_wrong_principal = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="wrong_principal",
    )
    ctx_wrong_principal = create_auth_context(event_wrong_principal, enforce=True, ifc_labels=_service_labels(event_wrong_principal))
    runtime_wrong_principal = MockRuntime(ctx_wrong_principal)
    actor_wrong_principal = _commitment_actor(runtime_wrong_principal)
    assert actor_wrong_principal is None or actor_wrong_principal[0] != "service:scheduler"

    event_no_service_principal = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
    )
    ctx_no_service_principal = create_auth_context(event_no_service_principal, enforce=True, ifc_labels=_service_labels(event_no_service_principal))
    runtime_no_service_principal = MockRuntime(ctx_no_service_principal)
    actor_no_service_principal = _commitment_actor(runtime_no_service_principal)
    assert actor_no_service_principal is None or actor_no_service_principal[0] != "service:scheduler"

    event_http_ingress = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
    )
    ctx_http_ingress = create_auth_context(event_http_ingress, enforce=True, ifc_labels=_service_labels(event_http_ingress))
    runtime_http_ingress = MockRuntime(ctx_http_ingress)
    actor_http_ingress = _commitment_actor(runtime_http_ingress)
    assert actor_http_ingress is None or actor_http_ingress[0] != "service:scheduler"
