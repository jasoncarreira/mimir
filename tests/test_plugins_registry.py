"""Completeness and ownership checks for the frozen policy registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from enum import StrEnum
from types import SimpleNamespace

import pytest

from mimir.access_control import ServicePrincipal
from mimir.models import RepoPRAction
from mimir.plugins import manager
from mimir.plugins.discovery import PluginDiscoveryError
from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry, TrustedScopeConstructors
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


def test_plugin_non_tool_capability_has_owner_bound_tier_and_core_profile_membership():
    registry = PolicyRegistry()
    registry.register_authority_profile("review")
    plugin = registry.registrar("example")
    plugin.register_capability("review_other_authors", tier="scoped-with-provenance")
    plugin.add_to_profile("review", "review_other_authors")

    policy = registry.freeze()
    assert policy.tiers["review_other_authors"] == "scoped-with-provenance"
    assert "review_other_authors" in policy.authority_profiles["review"]
    assert "review_other_authors" not in policy.tools


def test_plugin_non_tool_capability_cannot_grant_foreign_policy():
    registry = PolicyRegistry()
    registry.register_authority_profile("review")
    registry.register_capability("core_grant", tier="unbounded")
    registry.register_authority_profile("core_review", capabilities={"core_grant"})
    one = registry.registrar("one")
    two = registry.registrar("two")
    one.register_capability("one_grant", tier="scoped-with-provenance")
    two.register_capability("two_grant", tier="scope-contained")
    with pytest.raises(PolicyRegistrationError, match="foreign tool"):
        two.add_to_profile("review", "one_grant")
    with pytest.raises(PolicyRegistrationError, match="foreign tool"):
        one.add_to_profile("review", "core_grant")
    with pytest.raises(PolicyRegistrationError, match="owner cannot be overridden"):
        one.register_tool(**tool_fields(owner="core"))
    with pytest.raises(PolicyRegistrationError, match="invalid capability tier"):
        one.register_capability("bad_grant", tier="unrecognized")
    assert "bad_grant" not in registry.capabilities
    assert registry.authority_profiles["review"] == frozenset()


def test_plugin_cannot_override_registered_tool_tier_with_non_tool_api():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**tool_fields())
    with pytest.raises(PolicyRegistrationError, match="invalid capability tier"):
        plugin.register_capability("sample", tier="unbounded")
    assert "sample" not in registry.capabilities


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
        sink_payload_extractor=lambda _name, args, _context: (args.get("payload", ""),),
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
    registry.register_shell_profile_mapping("research", "scheduler_read_only")
    with pytest.raises(PolicyRegistrationError, match="duplicate shell profile mapping: research"):
        registry.register_shell_profile_mapping("research", "maintenance")


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


@pytest.mark.parametrize("membership", [
    "retained_repo_tools", "fixed_service_sinks", "worklink_build_tools",
    "taint_independent_egress", "catalog_admin_builtin", "acp_hands_result_tools",
    "repo_publish_tools",
])
def test_core_only_memberships_are_not_plugin_grants(membership):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match=f"example: tool sample: core-only memberships.*{membership}"):
        registry.registrar("example").register_tool(**tool_fields(memberships={membership}))
    assert not registry.tools


def selected_candidate(**overrides):
    ep = SimpleNamespace(
        group="mimir.plugins", name="cultivate", value="mimir_cultivate:provider",
        dist=SimpleNamespace(metadata={"Name": "mimir-cultivate"}),
    )
    fields = dict(name="cultivate", distribution="mimir-cultivate", entry_point=ep,
                  manifest=SimpleNamespace(config={}), enabled=True, diagnostic=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def selected_registry(candidate=None):
    candidate = candidate or selected_candidate()
    registry = PolicyRegistry()
    registry.select_plugin_candidates((candidate,))
    registry.registrar("cultivate").register_tool(**tool_fields(name="owned"))
    return registry, candidate


def test_selected_first_party_worklink_sink_has_bound_service_adapter():
    registry = PolicyRegistry()
    registry.select_plugin_candidates((selected_candidate(),))
    registrar = registry.registrar("cultivate")
    registrar.register_tool(**tool_fields(
        name="build_job", flow="both", tier="code-execution",
        sink_category=SinkCategory.SPAWN, sink_destination="worklink",
        service_sink_adapter="worklink_repo", profiles=("builder",),
    ))
    registrar.register_capability("build_job")
    registrar.register_authority_profile("builder", capabilities=("build_job",))
    assert registry.freeze().tools["build_job"].service_sink_adapter == "worklink_repo"


def test_foreign_provider_cannot_declare_worklink_sink_or_null_forge_payload():
    registry = PolicyRegistry()
    plugin = registry.registrar("other")
    with pytest.raises(PolicyRegistrationError, match="outbound sink requires a callable"):
        plugin.register_tool(**tool_fields(
            sink_category=SinkCategory.FORGE, sink_destination="bound_pull_request",
        ))
    plugin.register_tool(**tool_fields(
        name="foreign_spawn", sink_category=SinkCategory.SPAWN,
        sink_destination="worklink", service_sink_adapter="worklink_repo",
    ))
    with pytest.raises(PolicyRegistrationError, match="invalid service sink adapter"):
        registry.freeze()


def test_selected_provider_credential_probes_are_owner_namespaced():
    registry = PolicyRegistry()
    candidate = SimpleNamespace(name="example", enabled=True, diagnostic=None)
    registry.select_plugin_candidates((candidate,))
    registrar = registry.registrar("example")
    with pytest.raises(PolicyRegistrationError, match="credential probe"):
        registrar.register_credential_probe(
            "GITHUB_TOKEN", "A", ("GITHUB_TOKEN",), "foreign shadow", lambda: (True, "ok"),
        )
    registrar.register_credential_probe(
        "MIMIR_PLUGIN_EXAMPLE_TOKEN", "A", ("MIMIR_PLUGIN_EXAMPLE_TOKEN",),
        "own token", lambda: (True, "ok"),
    )
    with pytest.raises(PolicyRegistrationError, match="duplicate"):
        registrar.register_credential_probe(
            "MIMIR_PLUGIN_EXAMPLE_TOKEN", "A", ("MIMIR_PLUGIN_EXAMPLE_TOKEN",),
            "own token", lambda: (True, "ok"),
        )
    assert "MIMIR_PLUGIN_EXAMPLE_TOKEN" in registry.freeze().credential_probes


def test_first_party_projection_is_owner_bound_and_frozen():
    registry, candidate = selected_registry()
    registry._project_first_party_memberships(candidate, {
        "retained_repo_tools": ("owned",), "repo_publish_tools": ("owned",),
    })
    assert registry.freeze().tools["owned"].memberships == {
        "retained_repo_tools", "repo_publish_tools",
    }
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry._project_first_party_memberships(candidate, {})
    with pytest.raises(FrozenInstanceError):
        registry.tools["owned"].memberships = frozenset()


@pytest.mark.parametrize("projection", [
    {"retained_repo_tools": ("missing",)},
    {"retained_repo_tools": ("foreign",)},
    {"retained_repo_tools": ("owned", "owned")},
    {"fixed_service_sinks": ("owned",)},
    {"acp_hands_result_tools": ("owned",)},
    {"catalog_admin_builtin": ("owned",)},
    {"taint_independent_egress": ("owned",)},
])
def test_first_party_projection_rejects_invalid_entries_atomically(projection):
    registry, candidate = selected_registry()
    registry.register_tool(**tool_fields(name="foreign"))
    with pytest.raises(PolicyRegistrationError):
        registry._project_first_party_memberships(candidate, projection)
    assert registry.tools["owned"].memberships == frozenset()


@pytest.mark.parametrize("change", [
    {"distribution": "foreign"}, {"name": "foreign"}, {"enabled": False},
    {"manifest": None}, {"diagnostic": "invalid"},
])
def test_first_party_projection_requires_selected_installed_identity(change):
    candidate = selected_candidate(**change)
    registry = PolicyRegistry()
    registry.select_plugin_candidates((candidate,))
    with pytest.raises(PolicyRegistrationError, match="invalid selected"):
        registry._project_first_party_memberships(candidate, {})


def test_first_party_projection_rejects_unselected_and_repeated_calls():
    registry, candidate = selected_registry()
    with pytest.raises(PolicyRegistrationError, match="invalid selected"):
        registry._project_first_party_memberships(selected_candidate(), {})
    registry._project_first_party_memberships(candidate, {})
    with pytest.raises(PolicyRegistrationError, match="invalid selected"):
        registry._project_first_party_memberships(candidate, {})


def test_first_party_projection_rejects_duplicate_selected_owner():
    candidate = selected_candidate()
    registry = PolicyRegistry()
    registry.select_plugin_candidates((candidate, selected_candidate()))
    with pytest.raises(PolicyRegistrationError, match="invalid selected"):
        registry._project_first_party_memberships(candidate, {})


def test_manager_projects_only_selected_first_party_provider(monkeypatch):
    candidate = selected_candidate()
    from mimir.plugins.core import register_core_tools

    monkeypatch.setattr(manager, "register_core_tools", lambda registry, **_kw: register_core_tools(registry))

    class Contributor:
        def register(self, registrar):
            registrar.register_tool(**tool_fields(name="owned"))

        def host_membership_projection(self):
            return {"worklink_build_tools": ("owned",)}

    candidate.entry_point.load = lambda: Contributor()
    monkeypatch.setattr(manager, "enabled_plugins", lambda *, enable: (candidate,))
    policy = manager.load_plugins(enable="cultivate").policy
    assert policy.tools["owned"].memberships == {"worklink_build_tools"}
    with pytest.raises(TypeError):
        policy.tools["new"] = policy.tools["owned"]


def test_manager_rejects_invalid_first_party_projection(monkeypatch):
    candidate = selected_candidate()

    class Contributor:
        def register(self, registrar):
            registrar.register_tool(**tool_fields(name="owned"))

        def host_membership_projection(self):
            return {"retained_repo_tools": ("missing",)}

    candidate.entry_point.load = lambda: Contributor()
    monkeypatch.setattr(manager, "enabled_plugins", lambda *, enable: (candidate,))
    with pytest.raises(PluginDiscoveryError, match="membership projection failed.*absent or foreign"):
        manager.load_plugins(enable="cultivate")


def test_unknown_membership_is_not_accepted():
    with pytest.raises(PolicyRegistrationError, match="unknown memberships.*admin_bypass"):
        PolicyRegistry().registrar("example").register_tool(**tool_fields(memberships={"admin_bypass"}))


def test_principal_collisions_and_profile_validation():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    first = ServicePrincipal(
        trigger="plugin:example:periodic", canonical="plugin:example:worker",
        authority_profile="missing",
    )
    plugin.register_service_principal(first)
    with pytest.raises(PolicyRegistrationError, match="duplicate service principal: plugin:example:periodic"):
        plugin.register_service_principal(first)
    with pytest.raises(PolicyRegistrationError, match="duplicate service principal: plugin:example:another"):
        plugin.register_service_principal(ServicePrincipal(
            trigger="plugin:example:another", canonical="plugin:example:worker",
            authority_profile="missing",
        ))
    with pytest.raises(PolicyRegistrationError, match="plugin:example:periodic: unknown authority profile missing"):
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
    registry.registrar("example").register_scoped_action(RepoPRAction, lambda *_: True)
    registry.registrar("example").register_tool(**tool_fields(
        operation_class="resource_scoped", resource_scope="repo_pr", typed_action="unknown",
    ))
    with pytest.raises(PolicyRegistrationError, match="invalid repo/PR action"):
        registry.freeze()
    assert list(RepoPRAction)


def test_scoped_action_protocol_is_owner_bound_and_frozen():
    class Action(StrEnum):
        INSPECT = "resource.inspect"

    class OtherAction(StrEnum):
        INSPECT = "resource.inspect"

    registry = PolicyRegistry()
    alpha = registry.registrar("alpha")
    beta = registry.registrar("beta")
    validator = lambda *_: True
    alpha.register_scoped_action(Action, validator)
    alpha.register_tool(**tool_fields(
        operation_class="resource_scoped", resource_scope="repo_pr",
        typed_action=Action.INSPECT,
    ))
    with pytest.raises(PolicyRegistrationError, match="duplicate scoped action protocol"):
        alpha.register_scoped_action(Action, validator)
    with pytest.raises(PolicyRegistrationError, match="scoped action protocol"):
        beta.register_scoped_action(Action, validator)
    beta.register_scoped_action(OtherAction, validator)
    beta.register_tool(**tool_fields(name="foreign", operation_class="resource_scoped",
                                      resource_scope="repo_pr", typed_action=Action.INSPECT))
    with pytest.raises(PolicyRegistrationError, match="invalid repo/PR action"):
        registry.freeze()
    assert not registry.frozen
    assert registry.scoped_actions["alpha"].validate_scope is validator


def test_trusted_scope_constructors_require_selected_owner_and_freeze():
    class Action(StrEnum):
        INSPECT = "resource.inspect"

    def construct(*args, **kwargs):
        return args, kwargs

    constructors = TrustedScopeConstructors(
        issue=construct, review=construct, registry=construct,
        discovered=construct, store=construct, retained=construct,
    )
    with pytest.raises(PolicyRegistrationError, match="scoped action protocol"):
        PolicyRegistry().registrar("cultivate").register_scoped_action(
            Action, lambda *_: True, trusted_scope=constructors,
        )
    registry, _ = selected_registry()
    with pytest.raises(PolicyRegistrationError, match="scoped action protocol"):
        registry.registrar("foreign").register_scoped_action(
            Action, lambda *_: True, trusted_scope=constructors,
        )
    registry.registrar("cultivate").register_scoped_action(
        Action, lambda *_: True, trusted_scope=constructors,
    )
    protocol = registry.freeze().scoped_actions["cultivate"]
    assert protocol.trusted_scope is constructors
    with pytest.raises(FrozenInstanceError):
        protocol.trusted_scope = None
    with pytest.raises(FrozenInstanceError):
        constructors.issue = None
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registry.registrar("cultivate")


def test_trusted_scope_constructors_reject_incomplete_bundle():
    class Action(StrEnum):
        INSPECT = "resource.inspect"

    registry, _ = selected_registry()
    with pytest.raises(PolicyRegistrationError, match="scoped action protocol"):
        registry.registrar("cultivate").register_scoped_action(
            Action, lambda *_: True,
            trusted_scope=TrustedScopeConstructors(
                issue=lambda: None, review=lambda: None, registry=lambda: None,
                discovered=lambda: None, store=lambda: None, retained=None,
            ),
        )
    assert not registry.scoped_actions


def test_registered_owner_action_freezes():
    class Action(StrEnum):
        INSPECT = "resource.inspect"

    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_scoped_action(Action, lambda *_: True)
    plugin.register_tool(**tool_fields(operation_class="resource_scoped",
                                       resource_scope="repo_pr", typed_action=Action.INSPECT))
    assert registry.freeze().tools["sample"].typed_action is Action.INSPECT


def test_registered_action_freezes_without_legacy_model_type(monkeypatch):
    from mimir import models

    class Action(StrEnum):
        INSPECT = "resource.inspect"

    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_scoped_action(Action, lambda *_: True)
    plugin.register_tool(**tool_fields(operation_class="resource_scoped",
                                       resource_scope="repo_pr", typed_action=Action.INSPECT))
    monkeypatch.delattr(models, "RepoPRAction")
    assert registry.freeze().tools["sample"].typed_action is Action.INSPECT


def test_plugin_cannot_borrow_legacy_core_action_without_own_protocol():
    registry = PolicyRegistry()
    registry.registrar("example").register_tool(**tool_fields(
        operation_class="resource_scoped", resource_scope="repo_pr",
        typed_action=RepoPRAction.INSPECT,
    ))
    with pytest.raises(PolicyRegistrationError, match="requires owner action protocol"):
        registry.freeze()
    assert not registry.frozen


def test_core_legacy_action_freezes_without_plugin_protocol():
    registry = PolicyRegistry()
    registry.register_tool(**tool_fields(
        operation_class="resource_scoped", resource_scope="repo_pr",
        typed_action=RepoPRAction.INSPECT,
    ))
    assert registry.freeze().tools["sample"].typed_action is RepoPRAction.INSPECT


def test_registered_action_cannot_be_replaced_by_legacy_action():
    class Action(StrEnum):
        INSPECT = "resource.inspect"

    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_scoped_action(Action, lambda *_: True)
    plugin.register_tool(**tool_fields(operation_class="resource_scoped",
                                       resource_scope="repo_pr", typed_action=RepoPRAction.INSPECT))
    with pytest.raises(PolicyRegistrationError, match="invalid repo/PR action"):
        registry.freeze()


@pytest.mark.parametrize("action_type,validator", [
    (str, lambda *_: True), (RepoPRAction, None),
])
def test_invalid_action_protocol_does_not_publish(action_type, validator):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="scoped action protocol"):
        registry.registrar("example").register_scoped_action(action_type, validator)
    assert not registry.scoped_actions


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


def test_extension_declarations_are_owner_scoped_and_frozen():
    registry = PolicyRegistry()
    alpha = registry.registrar("alpha")
    beta = registry.registrar("beta")
    callback = lambda *args: None
    alpha.register_poller_hook("alpha-hook", callback)
    alpha.register_scheduler_job("tick", "tick-id", callback)
    alpha.register_cli_command("alpha", callback, callback)
    alpha.register_route("get", "/api/v1/plugins/alpha/status", callback, role="admin")
    alpha.register_dashboard_entry("alpha-page", {"path": "/alpha"})
    alpha.register_prompt_provider("alpha_memory", "channel_memory", 512, callback, callback)

    @dataclass(frozen=True)
    class Slot:
        value: str

    alpha.register_state_slot(Slot)
    with pytest.raises(PolicyRegistrationError, match="duplicate scheduler job"):
        beta.register_scheduler_job("other", "tick-id", callback)
    with pytest.raises(PolicyRegistrationError, match="duplicate CLI command"):
        beta.register_cli_command("alpha", callback, callback)
    with pytest.raises(PolicyRegistrationError, match="route must be under"):
        beta.register_route("GET", "/api/v1/plugins/alpha/status", callback)
    with pytest.raises(PolicyRegistrationError, match="duplicate prompt provider"):
        beta.register_prompt_provider("alpha_memory", "feedback", 10, callback, callback)
    with pytest.raises(PolicyRegistrationError, match="unique frozen dataclass"):
        alpha.register_state_slot(Slot)
    policy = registry.freeze()
    assert policy.routes[("GET", "/api/v1/plugins/alpha/status")].role == "admin"
    assert policy.scheduler_jobs["tick"].owner == "alpha"
    assert policy.state_slots["alpha"] is Slot
    with pytest.raises(TypeError):
        policy.prompt_providers["new"] = policy.prompt_providers["alpha_memory"]
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        alpha.register_poller_hook("late", callback)


@pytest.mark.parametrize("section,budget,name", [
    ("unknown", 10, "block"), ("feedback", 0, "block"),
    ("feedback", True, "block"), ("feedback", 10, "domain:block"),
])
def test_prompt_registration_rejects_invalid_contract(section, budget, name):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError):
        registry.registrar("alpha").register_prompt_provider(name, section, budget, lambda *_: True, lambda *_: "text")
    assert not registry.prompt_providers
