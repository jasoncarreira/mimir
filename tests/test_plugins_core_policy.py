"""Core descriptor declarations and the public projection share one registry."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path
from types import MappingProxyType

import pytest

from mimir import access_control as policy, read_policy
from mimir.plugins.core import ServicePrincipal, core_policy_registry, core_runtime_names, register_core_tools
from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry
from mimir.tool_descriptors import (
    ResultOriginKind,
    TOOL_DESCRIPTORS,
    _PAYLOAD_EXTRACTOR_UNDECLARED,
    _TARGET_EXTRACTOR_UNDECLARED,
)
from mimir.tools import budget_gate


def _extractor(value: object, undeclared: object) -> str:
    if value is undeclared:
        return "undeclared"
    if value is None:
        return "none"
    captured = [cell.cell_contents for cell in getattr(value, "__closure__", None) or ()]
    return f"{value.__module__}.{value.__qualname__}:{json.dumps(captured, sort_keys=True)}"


def _retired_forge_and_repository(oracle: dict) -> frozenset[str]:
    return (frozenset(oracle["tables"]["repo_actions"])
            | frozenset(oracle["tables"]["forge_actions"])
            | {"pr_list", "ci_run_jobs", "ci_run", "ci_recent_runs", "issue_comment",
               "spawn_open_code", "worklink_run", "worklink_resume"})


def test_all_frozen_core_descriptors_come_from_registrations() -> None:
    oracle = json.loads(
        (Path(__file__).parent / "fixtures" / "plugin_core_policy_baseline.json").read_text()
    )
    expected = {
        name: row for name, row in oracle["descriptors"].items()
        if name not in _retired_forge_and_repository(oracle)
    }
    registry = core_policy_registry()
    assert registry.frozen
    assert len(oracle["descriptors"]) == 103
    assert len(registry.descriptors) == len(expected)
    assert set(registry.descriptors) == set(TOOL_DESCRIPTORS) == set(expected)
    for name, descriptor in TOOL_DESCRIPTORS.items():
        tool = registry.tools[name]
        assert tool.owner == "core"
        assert tool.policy_only == (tool.factory is None)
        assert tool.descriptor is descriptor
        row = {}
        for field in fields(descriptor):
            value = getattr(descriptor, field.name)
            if field.name == "sink_target_extractor":
                value = _extractor(value, _TARGET_EXTRACTOR_UNDECLARED)
            elif field.name == "sink_payload_extractor":
                value = _extractor(value, _PAYLOAD_EXTRACTOR_UNDECLARED)
            elif field.name == "result_origin":
                value = int(value)
            row[field.name] = value
        assert row == expected[name], name


def test_core_runtime_factories_cover_each_surface_without_loading_coding() -> None:
    registry = core_policy_registry()
    surfaces = ("default", "web_search", "fetch_url", "coding")
    names = [name for surface in surfaces for name in core_runtime_names(surface)]
    assert len(names) == len(set(names))
    assert all(registry.tools[name].factory is not None for name in names)
    assert all(registry.tools[name].policy_only for name in registry.tools if name not in names)
    assert core_runtime_names("coding") == ()
    assert "repo_test" not in registry.tools
    assert "pr_metadata" not in registry.tools


def test_selected_owner_gets_unregistered_coding_names_before_registration() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry, selected_owners=frozenset({"cultivate"}))
    assert "repo_status" not in registry.tools
    assert "pr_metadata" not in registry.tools
    assert "worklink_run" not in registry.tools
    assert "repo_status" not in core_policy_registry().tools
    assert "send_message" in registry.tools


def test_core_flow_registration_includes_native_aliases_and_descriptorless_tools() -> None:
    registry = core_policy_registry()
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "plugin_core_policy_baseline.json").read_text()
    )
    removed = _retired_forge_and_repository(expected)
    expected = {name: flow for name, flow in expected["tables"]["flow"].items()
                if name not in removed}
    assert dict(registry.flow) == expected
    assert set(policy._TOOL_FLOW_MAP) == set(expected)
    descriptorless = set(expected) - set(TOOL_DESCRIPTORS)
    assert descriptorless
    for name in descriptorless:
        assert registry.tools[name].descriptor is None
        assert policy.get_tool_flow_direction(name).value == expected[name]
    for name in ("Bash", "aread", "hands_shell", "harness_auto_deliver"):
        assert policy.get_tool_flow_direction(name).value == expected[name]
    assert policy.get_tool_flow_direction("not_registered") is policy.ToolFlowDirection.UNKNOWN
    with pytest.raises(TypeError):
        policy._TOOL_FLOW_MAP["other"] = policy.ToolFlowDirection.SINK


def test_registered_flow_lookup_uses_projected_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    registry.registrar("example").register_tool(
        name="example_flow", policy_only=True, flow="source", tier="scope-contained",
        operation_class="open", result_domain="example",
        result_origin=ResultOriginKind.NONE, sink_category=None, sink_destination=None,
        sink_target_extractor=None, sink_payload_extractor=None,
        writes_code_or_state=False, profiles=(), read_backend=False,
    )
    projected = MappingProxyType({
        name: policy.ToolFlowDirection(flow) for name, flow in registry.freeze().flow.items()
    })
    monkeypatch.setattr(policy, "_TOOL_FLOW_MAP", projected)
    assert policy.get_tool_flow_direction("example_flow") is policy.ToolFlowDirection.SOURCE
    assert policy.get_tool_flow_direction("example_missing") is policy.ToolFlowDirection.UNKNOWN


def test_public_descriptors_are_the_core_registry_projection() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    assert registry.freeze().descriptors == TOOL_DESCRIPTORS
    with pytest.raises(TypeError):
        TOOL_DESCRIPTORS["other"] = TOOL_DESCRIPTORS["send_message"]


def test_authority_profile_views_are_frozen_registry_projections() -> None:
    registry = core_policy_registry()
    assert policy.TRIGGER_AUTHORITY_PROFILES == registry.authority_profiles
    assert policy.TRIGGER_CAPABILITY_TIERS == registry.tiers
    assert policy.SCHEDULER_AUTHORITY_PROFILES == frozenset(
        name for name, profile in registry.profiles.items() if profile.scheduler
    )
    assert policy.BOUNDED_PROFILE_CAPABILITIES["session-boundary"] == frozenset({"fetch_url"})
    assert "fetch_url" not in policy.TRIGGER_AUTHORITY_PROFILES["session-boundary"]
    assert policy._SHELL_PROFILE_BY_AUTHORITY_PROFILE == registry.shell_profile_mappings
    assert policy._SHELL_PROFILE_BY_AUTHORITY_PROFILE["github"] == "repo_review"
    assert policy.OPERATOR_SHELL_PROFILE == registry.operator_shell_profile == "scheduler_read_only"
    assert policy._FETCH_URL_POLICY_BY_AUTHORITY_PROFILE["heartbeat"] == (
        "approved_urls", "MIMIR_HEARTBEAT_APPROVED_URLS"
    )
    assert policy._CAPABILITY_COMPANIONS == registry.capability_companions
    assert policy._LEGACY_SERVICE_SINK_TIERS == registry.legacy_service_sink_tiers
    with pytest.raises(TypeError):
        policy.TRIGGER_CAPABILITY_TIERS["new"] = policy.CapabilityTier.UNBOUNDED
    with pytest.raises(TypeError):
        policy.TRIGGER_AUTHORITY_PROFILES["custom"] = frozenset()


def test_plugin_profile_cannot_bound_unbacked_capability() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    registrar = registry.registrar("example")
    registrar.register_capability("plugin_fetch")
    registrar.register_authority_profile("plugin_profile", bounded_capabilities={"plugin_fetch"})
    with pytest.raises(
        PolicyRegistrationError,
        match="plugin_profile: plugin profile cannot add foreign capability",
    ):
        registry.freeze()


def test_legacy_bound_exception_does_not_apply_to_other_core_profiles() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    registry.register_authority_profile("other", bounded_capabilities={"fetch_url"})
    with pytest.raises(PolicyRegistrationError, match="other: bounded capability outside profile"):
        registry.freeze()


def test_operator_shell_profile_registration_is_unique_and_frozen() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    with pytest.raises(PolicyRegistrationError, match="duplicate operator shell profile"):
        registry.register_operator_shell_profile("other")
    registry.freeze()
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry.register_operator_shell_profile("other")


def test_static_service_principals_are_immutable_core_projections() -> None:
    registry = core_policy_registry()
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "plugin_core_policy_baseline.json").read_text()
    )["tables"]
    assert policy._TRUSTED_SERVICE_PRINCIPALS == registry.service_principals
    assert set(registry.service_principals) == policy._REQUIRED_SERVICE_PRINCIPALS == set(
        expected["required_service_principals"]
    )
    assert set(registry.service_principals) == set(expected["trusted_service_principals"])
    assert policy.get_service_principal("poller") is None
    for trigger, principal in registry.service_principals.items():
        assert policy.get_service_principal(trigger) is principal
        assert principal.trigger == trigger
    with pytest.raises(TypeError):
        policy._TRUSTED_SERVICE_PRINCIPALS["poller"] = registry.service_principals["upgrade"]


def test_registered_service_principals_are_typed_and_collision_checked() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    scheduler = registry.service_principals["scheduled_tick"]
    with pytest.raises(PolicyRegistrationError, match="duplicate service principal"):
        registry.register_service_principal(replace(scheduler, trigger="different"))
    with pytest.raises(PolicyRegistrationError, match="expected frozen ServicePrincipal"):
        registry.register_service_principal(object())
    with pytest.raises(PolicyRegistrationError, match="invalid principal capabilities"):
        registry.register_service_principal(ServicePrincipal(
            canonical="new", trigger="new", capabilities=(None,),
        ))
    registry.freeze()
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry.register_service_principal(ServicePrincipal(canonical="new", trigger="new"))


def test_core_service_grants_check_capability_domain_and_destination() -> None:
    scheduler = policy.get_service_principal("scheduled_tick")
    assert scheduler is not None
    assert policy.service_can_invoke_operation(scheduler, "read_file")
    assert policy.service_can_invoke_operation(scheduler, "shell_exec")
    assert not policy.service_can_invoke_operation(scheduler, "memory_store")
    assert not policy.service_can_invoke_operation(
        replace(scheduler, readable_domains=("configured_inputs",)), "read_file"
    )
    assert not policy.service_can_invoke_operation(
        replace(scheduler, sink_destinations=("message",)), "shell_exec"
    )


def test_operation_views_are_derived_from_core_registrations() -> None:
    tools = core_policy_registry().tools
    catalog = policy.OperationCatalog
    for operation_class, view in (
        ("open", catalog._OPEN_OPERATIONS),
        ("resource_scoped", catalog._RESOURCE_SCOPED_OPERATIONS),
        ("admin_required", catalog._ADMIN_REQUIRED_OPERATIONS),
    ):
        assert view == frozenset(
            name for name, tool in tools.items() if tool.operation_class == operation_class
        )
    for membership, view in (
        ("channel_operations", policy.ChannelResourceAdapter._CHANNEL_OPERATIONS),
        ("write_operations", policy.WriteResourceAdapter._WRITE_OPERATIONS),
        ("write_resource_operations", policy.WriteResourceAdapter._RESOURCE_OPERATIONS),
        ("protected_metadata_operations", catalog._PROTECTED_METADATA_OPERATIONS),
        ("catalog_admin_builtin", catalog._ADMIN_BUILTIN_TOOL_NAMES),
    ):
        assert view == frozenset(
            name for name, tool in tools.items() if membership in tool.memberships
        )
    assert catalog._OPEN_OPERATIONS.isdisjoint(catalog._PROTECTED_METADATA_OPERATIONS)
    assert policy.OperationCatalog().get_decision("memory_query") is policy.OperationDecision.OPEN
    assert policy.OperationCatalog().get_decision("send_message") is policy.OperationDecision.UNKNOWN
    assert (
        policy.get_operation_catalog().get_decision("send_message")
        is policy.OperationDecision.RESOURCE_SCOPED
    )
    assert policy.OperationCatalog().get_decision("not_registered") is policy.OperationDecision.UNKNOWN


def test_result_and_read_views_are_frozen_core_projections() -> None:
    tools = core_policy_registry().tools
    assert policy._PROTECTED_RESULT_DOMAINS == {
        name: tool.result_domain for name, tool in tools.items()
        if tool.result_domain is not None
    }
    assert policy._OPERATION_READABLE_DOMAIN == {
        name: tool.readable_domain for name, tool in tools.items()
        if tool.readable_domain is not None
    }
    assert policy._READ_BACKEND_RESULT_TOOLS == frozenset(
        name for name, tool in tools.items() if tool.read_backend
    )
    for membership, view in (
        ("read_resource_operations", read_policy.READ_RESOURCE_OPERATIONS),
        ("acp_hands_result_tools", policy._ACP_HANDS_RESULT_TOOLS),
    ):
        assert view == frozenset(
            name for name, tool in tools.items() if membership in tool.memberships
        )
    assert policy._PROTECTED_RESULT_DOMAINS["hands_shell"] == "client_provider"
    assert "hands_read" in policy._READ_BACKEND_RESULT_TOOLS
    assert "hands_shell" not in policy._READ_BACKEND_RESULT_TOOLS
    assert policy._OPERATION_READABLE_DOMAIN["hands_python"] == "client_provider"
    assert "hands_edit" not in policy._OPERATION_READABLE_DOMAIN
    assert TOOL_DESCRIPTORS["commitment_complete"].result_origin & ResultOriginKind.NON_INGESTING
    with pytest.raises(TypeError):
        policy._PROTECTED_RESULT_DOMAINS["other"] = "filesystem"
    with pytest.raises(TypeError):
        policy._OPERATION_READABLE_DOMAIN["other"] = "filesystem"


def test_sink_and_taint_views_are_exact_registry_memberships() -> None:
    tools = core_policy_registry().tools
    for membership, view in (
        ("fixed_service_sinks", policy._FIXED_SERVICE_SINK_OPERATIONS),
        ("taint_independent_egress", policy._TAINT_INDEPENDENT_EGRESS_TOOLS),
        ("worklink_build_tools", policy._WORKLINK_BUILD_TOOLS),
        ("repo_publish_tools", policy._REPO_PUBLISH_TOOLS),
        ("tainted_writes", policy._TAINTED_TURN_WRITE_TOOLS),
        ("tainted_file_writes", policy._TAINTED_FILE_WRITE_TOOLS),
        ("schedule_writes", policy._SCHEDULE_WRITE_TOOLS),
        ("saga_mutations", policy._SAGA_MUTATION_OPERATIONS),
    ):
        assert view == frozenset(
            name for name, tool in tools.items() if membership in tool.memberships
        ), membership
    assert tools["upload"].descriptor is None
    assert "upload" not in core_policy_registry().flow
    assert policy._FIXED_SERVICE_SINK_OPERATIONS == {"rebuild_index"}
    assert policy._TAINT_INDEPENDENT_EGRESS_TOOLS == {"fetch_url", "web_search"}


def test_typed_action_and_retained_views_are_exact_registry_projections() -> None:
    tools = core_policy_registry().tools
    for membership, view in (
        ("forge_actions", policy._FORGE_TOOL_ACTIONS),
        ("repo_actions", policy._REPO_TOOL_ACTIONS),
    ):
        assert dict(view) == {
            name: tool.typed_action for name, tool in tools.items()
            if membership in tool.memberships
        }
        with pytest.raises(TypeError):
            view["other"] = None
    assert policy._RETAINED_REPO_TOOLS == frozenset(
        name for name, tool in tools.items() if "retained_repo_tools" in tool.memberships
    )
    assert dict(policy._TYPED_REPO_PR_TOOL_ACTIONS) == {
        **policy._FORGE_TOOL_ACTIONS, **policy._REPO_TOOL_ACTIONS,
    }
    assert not policy._FORGE_TOOL_ACTIONS

    assert "pr_metadata" not in tools
    assert "repo_status" not in policy._RETAINED_REPO_TOOLS
    assert not policy._REPO_TOOL_ACTIONS


@pytest.mark.parametrize("name", """
    write_file edit_file Write Edit upload memory_store
    saga_end_session commitment_complete commitment_snooze commitment_dismiss
    open_proposal submit_proposal reload_pollers shell_exec bash_async
""".split())
def test_core_mutating_tools_truthfully_declare_write_potential(name) -> None:
    assert core_policy_registry().tools[name].writes_code_or_state is True


@pytest.mark.parametrize("name", """
    read_file ls grep glob
    memory_query memory_get commitment_list
""".split())
def test_core_observation_tools_do_not_claim_write_potential(name) -> None:
    assert core_policy_registry().tools[name].writes_code_or_state is False


def test_budget_policy_views_are_frozen_core_memberships() -> None:
    tools = core_policy_registry().tools
    for membership, view in (
        ("standing_review", budget_gate._STANDING_REVIEW_TOOLS),
        ("remediation_effect", budget_gate._REMEDIATION_EFFECT_TOOLS),
        ("typed_error_signal", budget_gate._TYPED_ERROR_SIGNAL_TOOLS),
    ):
        from mimir.plugins.core import PolicySetView
        assert isinstance(view, PolicySetView)
        assert not hasattr(view, "add")
        assert view == frozenset(
            name for name, tool in tools.items() if membership in tool.memberships
        ), membership
    assert "pr_job_log" not in budget_gate._STANDING_REVIEW_TOOLS
    assert "repo_commit" not in budget_gate._REMEDIATION_EFFECT_TOOLS
    assert "pr_comment" not in budget_gate._REMEDIATION_EFFECT_TOOLS
    assert "worklink_run" not in budget_gate._TYPED_ERROR_SIGNAL_TOOLS
