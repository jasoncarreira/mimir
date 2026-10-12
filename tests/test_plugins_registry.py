"""Completeness and ownership checks for the frozen policy registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mimir.access_control import ServicePrincipal
from mimir.models import RepoPRAction
from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry
from mimir.tool_descriptors import (
    ResultOriginKind, SinkCategory, _PAYLOAD_EXTRACTOR_UNDECLARED,
    _TARGET_EXTRACTOR_UNDECLARED,
)


def tool_fields(**overrides):
    fields = dict(
        name="sample", factory=lambda: object(), flow="source",
        tier="scope-contained", operation_class="open", result_domain=None,
        result_origin=ResultOriginKind.NONE, sink_category=None,
        sink_destination=None, sink_target_extractor=None,
        sink_payload_extractor=None, writes_code_or_state=False, profiles=(),
        read_backend=False,
    )
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("missing", [
    "name", "flow", "tier", "operation_class", "result_domain", "result_origin",
    "sink_category", "sink_destination", "sink_target_extractor",
    "sink_payload_extractor", "writes_code_or_state", "profiles", "read_backend",
])
def test_mandatory_tool_metadata_cannot_be_omitted(missing):
    fields = tool_fields()
    fields.pop(missing)
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match=f"example: tool {fields.get('name', '<unnamed>')}: missing required metadata: {missing}"):
        registry.registrar("example").register_tool(**fields)


def test_core_policy_entries_do_not_invent_descriptor_or_flow_rows():
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields(
        name="memory_query", factory=None, policy_only=True, tier=None,
        descriptor_registered=False,
    ))
    registry.register_tool(**tool_fields(
        name="bash_jobs_list", factory=None, policy_only=True,
        descriptor_registered=False,
    ))
    registry.register_capability("bash_jobs_list")
    registry.register_capability("pr_review_others", tier="scoped-with-provenance")
    registry.register_authority_profile("github", capabilities={"bash_jobs_list", "pr_review_others"})
    registry.register_tool(**tool_fields(
        name="web_turn_events", factory=None, policy_only=True, flow=None, tier=None,
        operation_class=None,
    ))
    registry.freeze()
    assert set(registry.descriptors) == {"web_turn_events"}
    assert registry.flow == {"memory_query": "source", "bash_jobs_list": "source"}
    assert registry.tiers == {
        "bash_jobs_list": "scope-contained", "pr_review_others": "scoped-with-provenance",
    }
    assert registry.authority_profiles["github"] == {"bash_jobs_list", "pr_review_others"}


@pytest.mark.parametrize("tool_first", [True, False])
def test_capability_tier_cannot_lower_tool_tier_in_either_order(tool_first):
    registry = PolicyRegistry()
    tool = tool_fields(name="dangerous", tier="unbounded")
    if tool_first:
        registry.register_tool(**tool)
        with pytest.raises(PolicyRegistrationError, match="core: capability dangerous: tier scope-contained conflicts with tool tier unbounded"):
            registry.register_capability("dangerous", tier="scope-contained")
        assert "dangerous" not in registry.capabilities
    else:
        registry.register_capability("dangerous", tier="scope-contained")
        with pytest.raises(PolicyRegistrationError, match="core: tool dangerous: tier unbounded conflicts with capability tier scope-contained"):
            registry.register_tool(**tool)
        assert "dangerous" not in registry.tools


def test_matching_tool_and_capability_tiers_project_one_value():
    registry = PolicyRegistry()
    registry.register_capability("sample", tier="scope-contained")
    registry.register_tool(**tool_fields())
    assert registry.freeze().tiers["sample"] == "scope-contained"


def test_plugin_cannot_exclude_its_descriptor_or_flow():
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="descriptor exclusion is core-only"):
        registry.registrar("example").register_tool(**tool_fields(descriptor_registered=False))
    with pytest.raises(PolicyRegistrationError, match="invalid flow"):
        registry.registrar("example").register_tool(**tool_fields(flow=None))


@pytest.mark.parametrize("fields", [
    {"result_origin": ResultOriginKind.NON_INGESTING, "result_domain": "web"},
    {"result_origin": ResultOriginKind.NONE, "result_domain": "web"},
    {"result_origin": ResultOriginKind.EXTERNAL, "result_domain": None},
    {"result_origin": ResultOriginKind.EXTERNAL, "result_domain": "web", "flow": "sink"},
])
def test_plugin_read_backend_requires_ingesting_source(fields):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="read backend requires a protected ingesting result"):
        registry.registrar("example").register_tool(**tool_fields(read_backend=True, **fields))
    assert not registry.tools


def test_profile_sink_requires_bounded_adapter():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**tool_fields(
        flow="sink", sink_category=SinkCategory.FILE, sink_destination="filesystem",
        result_origin=ResultOriginKind.NON_INGESTING,
        profiles=("worker",),
    ))
    plugin.register_capability("sample")
    plugin.register_authority_profile("worker", capabilities=("sample",))
    with pytest.raises(PolicyRegistrationError, match="profile sink requires a service sink adapter"):
        registry.freeze()
    assert not registry.frozen

    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="invalid service sink adapter"):
        registry.registrar("example").register_tool(**tool_fields(
            sink_category=SinkCategory.FILE, sink_destination="filesystem",
            service_sink_adapter="approved_urls",
        ))
        registry.freeze()

    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="invalid service sink adapter"):
        registry.registrar("example").register_tool(**tool_fields(
            sink_category=SinkCategory.FILE, sink_destination="filesystem",
            service_sink_adapter="unregistered_adapter",
        ))
        registry.freeze()

    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="invalid service sink destination"):
        registry.registrar("example").register_tool(**tool_fields(
            sink_category=SinkCategory.FILE, sink_destination="network",
            service_sink_adapter="trigger_service_write_roots",
        ))
        registry.freeze()


@pytest.mark.parametrize("category,destination", [
    (SinkCategory.EXTERNAL_MCP, "external_mcp"),
    (SinkCategory.PROPOSAL, "proposal"),
])
def test_profile_granted_sink_without_executable_adapter_cannot_freeze(category, destination):
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**tool_fields(
        flow="sink", result_origin=ResultOriginKind.NON_INGESTING,
        sink_category=category, sink_destination=destination,
        profiles=("worker",),
    ))
    plugin.register_capability("sample")
    plugin.register_authority_profile("worker", capabilities=("sample",))
    with pytest.raises(PolicyRegistrationError, match="profile sink has no executable service sink adapter"):
        registry.freeze()
    assert not registry.frozen


def test_factory_or_explicit_policy_only_required():
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="sample.*factory"):
        registry.register_tool(**tool_fields(factory=None))
    registry.register_tool(**tool_fields(factory=None, policy_only=True))
    assert registry.tools["sample"].policy_only


def test_duplicate_registrations_are_named():
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields())
    with pytest.raises(PolicyRegistrationError, match="duplicate tool: sample"):
        registry.registrar("other").register_tool(**tool_fields())
    registry.register_capability("sample")
    with pytest.raises(PolicyRegistrationError, match="duplicate capability: sample"):
        registry.registrar("other").register_capability("sample")
    registry.register_authority_profile("research")
    with pytest.raises(PolicyRegistrationError, match="duplicate profile: research"):
        registry.registrar("other").register_authority_profile("research")
    registry.register_shell_profile_mapping("research", "read_only")
    with pytest.raises(PolicyRegistrationError, match="duplicate shell profile mapping: research"):
        registry.register_shell_profile_mapping("research", "other")


def test_plugin_can_only_add_its_own_tool_to_existing_profile():
    registry = PolicyRegistry()
    registry.register_capability("core_tool")
    registry.register_tool(**tool_fields(name="core_tool"))
    registry.register_authority_profile("research", capabilities={"core_tool"})
    plugin = registry.registrar("example")
    with pytest.raises(PolicyRegistrationError, match="foreign tool core_tool"):
        plugin.add_to_profile("research", "core_tool")
    with pytest.raises(PolicyRegistrationError, match="duplicate profile: research"):
        plugin.register_authority_profile("research", capabilities=())
    plugin.register_capability("sample")
    plugin.register_tool(**tool_fields())
    plugin.add_to_profile("research", "sample")
    assert registry.freeze().authority_profiles["research"] == frozenset({"core_tool", "sample"})


def test_plugin_profile_membership_must_be_declared_on_both_sides():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_capability("sample")
    plugin.register_tool(**tool_fields(profiles=()))
    plugin.register_authority_profile("example-profile", capabilities={"sample"})
    with pytest.raises(PolicyRegistrationError, match="example: tool sample: undeclared profile membership example-profile"):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize("membership", ["retained_repo_tools", "fixed_service_sinks", "worklink_build_tools", "catalog_admin_builtin", "repo_publish_tools"])
def test_core_only_memberships_are_not_plugin_grants(membership):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match=f"example: tool sample: core-only memberships.*{membership}"):
        registry.registrar("example").register_tool(**tool_fields(memberships={membership}))
    assert not registry.tools


def test_unknown_membership_is_not_accepted():
    with pytest.raises(PolicyRegistrationError, match="unknown memberships.*admin_bypass"):
        PolicyRegistry().registrar("example").register_tool(**tool_fields(memberships={"admin_bypass"}))


def test_principal_collisions_and_profile_validation():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    first = ServicePrincipal(trigger="periodic", canonical="worker", authority_profile="missing")
    plugin.register_service_principal(first)
    with pytest.raises(PolicyRegistrationError, match="duplicate service principal: periodic"):
        plugin.register_service_principal(first)
    with pytest.raises(PolicyRegistrationError, match="duplicate service principal: another"):
        plugin.register_service_principal(ServicePrincipal(
            trigger="another", canonical="worker", authority_profile="missing",
        ))
    with pytest.raises(PolicyRegistrationError, match="periodic: unknown authority profile missing"):
        registry.freeze()


def test_freeze_rejects_unknown_references_and_resource_scopes():
    registry = PolicyRegistry()
    registry.registrar("example").register_tool(**tool_fields(profiles={"missing"}))
    with pytest.raises(PolicyRegistrationError, match="sample: missing profile membership missing"):
        registry.freeze()
    assert not registry.frozen

    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="resource_scoped requires"):
        registry.registrar("example").register_tool(**tool_fields(operation_class="resource_scoped"))
    with pytest.raises(PolicyRegistrationError, match="repo_pr scope requires typed action"):
        registry.registrar("example").register_tool(**tool_fields(
            operation_class="resource_scoped", resource_scope="repo_pr",
        ))
        registry.freeze()


def test_repo_pr_action_must_be_typed():
    registry = PolicyRegistry()
    registry.registrar("example").register_tool(**tool_fields(
        operation_class="resource_scoped", resource_scope="repo_pr", typed_action="unknown",
    ))
    with pytest.raises(PolicyRegistrationError, match="invalid repo/PR action"):
        registry.freeze()
    assert list(RepoPRAction)


def test_freeze_exposes_immutable_projections_and_refuses_writes():
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields())
    registry.freeze()
    assert registry.flow["sample"] == "source"
    with pytest.raises(TypeError):
        registry.tools["new"] = registry.tools["sample"]
    with pytest.raises(FrozenInstanceError):
        registry.tools["sample"].flow = "sink"
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry.register_capability("new")
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry.registrar("new")
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry.freeze()


def test_external_sink_requires_explicit_payload_and_target():
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields(
        sink_category=SinkCategory.NETWORK, sink_destination="network",
    ))
    assert registry.freeze().descriptors["sample"].sink_payload_extractor is None


@pytest.mark.parametrize("field,sentinel", [
    ("sink_target_extractor", _TARGET_EXTRACTOR_UNDECLARED),
    ("sink_payload_extractor", _PAYLOAD_EXTRACTOR_UNDECLARED),
])
def test_only_core_can_preserve_undeclared_descriptor_extractors(field, sentinel):
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields(factory=None, policy_only=True, **{field: sentinel}))
    assert getattr(registry.freeze().descriptors["sample"], field) is sentinel

    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="invalid .* extractor"):
        registry.registrar("example").register_tool(**tool_fields(**{field: sentinel}))
    assert not registry.tools


def test_only_core_can_preserve_targetless_sink_destination():
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields(
        factory=None, policy_only=True, sink_category=SinkCategory.SAME_CHANNEL,
    ))
    assert registry.freeze().descriptors["sample"].sink_destination is None

    with pytest.raises(PolicyRegistrationError, match="invalid sink destination"):
        PolicyRegistry().registrar("example").register_tool(**tool_fields(
            sink_category=SinkCategory.SAME_CHANNEL,
        ))
