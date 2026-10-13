"""Discriminating regressions for the plugin policy review's authority guards."""

from __future__ import annotations

import ast
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest

from mimir.plugins.core import CapabilityTier, ServicePrincipal, ServiceSinkPolicy, register_core_tools
from mimir.plugins.manifest import PluginManifestError, parse_manifest
from mimir.plugins.registry import PolicyRegistrar, PolicyRegistrationError, PolicyRegistry
from mimir.tool_descriptors import ResultOriginKind, SinkCategory


def fields(**overrides):
    values = dict(
        name="sample", factory=lambda: object(), flow="source", tier="scope-contained",
        operation_class="open", result_domain="web", result_origin=ResultOriginKind.EXTERNAL,
        sink_category=None, sink_destination=None, sink_target_extractor=None,
        sink_payload_extractor=None, writes_code_or_state=False, profiles=(), read_backend=False,
    )
    values.update(overrides)
    return values


def target(_name, arguments, _context):
    return (arguments.get("endpoint"),)


def payload(_name, arguments, _context):
    return (arguments["body"],) if isinstance(arguments.get("body"), str) else ()


def outbound(**overrides):
    values = fields(
        flow="sink", result_origin=ResultOriginKind.NON_INGESTING,
        sink_category=SinkCategory.NETWORK, sink_destination="network",
        sink_target_extractor=target, sink_payload_extractor=payload,
    )
    values.update(overrides)
    return values


def principal(**overrides):
    values = dict(
        canonical="plugin:example:worker", trigger="plugin:example:tick",
        capabilities=("sample",), authority_profile="worker",
        readable_domains=("web",), sink_destinations=("network",),
        creation_path="example.worker:run",
    )
    values.update(overrides)
    return ServicePrincipal(**values)


def worker_registry():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**outbound(profiles=("worker",), service_sink_adapter="approved_urls"))
    plugin.register_capability("sample")
    plugin.register_authority_profile("worker", capabilities=("sample",))
    return registry, plugin


@pytest.mark.parametrize("flow", ["source", "both", "neither"])
def test_non_ingesting_is_not_a_plugin_read_escape(flow):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="NON_INGESTING requires sink-only"):
        registry.registrar("example").register_tool(**fields(
            flow=flow, result_origin=ResultOriginKind.NON_INGESTING,
        ))
    assert not registry.tools


@pytest.mark.parametrize("origin", [ResultOriginKind.EXTERNAL, ResultOriginKind.REPOSITORY,
                                  ResultOriginKind.EXTERNAL | ResultOriginKind.REPOSITORY])
def test_non_ingesting_cannot_mask_an_ingesting_origin_even_for_sinks(origin):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="NON_INGESTING requires sink-only"):
        registry.registrar("example").register_tool(**outbound(
            result_origin=ResultOriginKind.NON_INGESTING | origin,
        ))
    assert not registry.tools


def test_sink_only_non_ingesting_declaration_remains_valid():
    registry = PolicyRegistry()
    registry.registrar("example").register_tool(**outbound())
    assert registry.freeze().descriptors["sample"].result_origin == ResultOriginKind.NON_INGESTING


@pytest.mark.parametrize("updates", [
    {"flow": "sink"}, {"result_domain": None}, {"result_origin": ResultOriginKind.NONE},
    {"result_origin": ResultOriginKind.NON_INGESTING},
])
def test_read_backend_guard_keeps_its_existing_contract_and_error(updates):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="read backend requires a protected ingesting result"):
        registry.registrar("example").register_tool(**fields(read_backend=True, **updates))
    assert not registry.tools


@pytest.mark.parametrize("entry_name,manifest_name", [
    ("core", "core"), ("core", "example"), ("example", "core"),
])
def test_core_manifest_owner_is_reserved_before_distribution_lookup(entry_name, manifest_name, monkeypatch):
    def distribution_lookup(_name):
        pytest.fail("reserved core manifest reached distribution lookup")
    monkeypatch.setattr("mimir.plugins.manifest.metadata.version", distribution_lookup)
    data = (f'name = "{manifest_name}"\nversion = "1.0"\nlicense = "MIT"\n'
            'requires_mimir = ">=0"\n').encode()
    with pytest.raises(PluginManifestError, match="'core' is reserved"):
        parse_manifest(data, entry_name=entry_name)


@pytest.mark.parametrize("direct", [False, True])
def test_core_registrar_owner_is_reserved(direct):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="'core' is reserved"):
        PolicyRegistrar(registry, "core") if direct else registry.registrar("core")
    # Core itself still registers through the unrestricted internal registry.
    register_core_tools(registry)
    assert registry.freeze().tools["read_file"].owner == "core"


def test_mcp_tool_namespace_cannot_select_the_remote_result_classifier():
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="mcp_ prefix is reserved"):
        registry.registrar("example").register_tool(**fields(name="mcp_fake_read"))
    assert not registry.tools


@pytest.mark.parametrize("tool_first", [False, True])
@pytest.mark.parametrize("tool_owner,capability_owner", [
    ("core", "example"), ("example", "core"), ("example", "other"), ("other", "example"),
])
def test_tool_capability_cross_owner_collisions_are_order_independent(tool_first, tool_owner, capability_owner):
    registry = PolicyRegistry()
    def register_tool():
        registry.register_tool(owner=tool_owner, **fields())
    def register_capability():
        registry.register_capability("sample", owner=capability_owner)
    first, second = (register_tool, register_capability) if tool_first else (register_capability, register_tool)
    first()
    with pytest.raises(PolicyRegistrationError, match="owned by"):
        second()
    assert ("sample" in registry.tools) is tool_first
    assert ("sample" in registry.capabilities) is not tool_first


@pytest.mark.parametrize("owner", ["core", "example"])
@pytest.mark.parametrize("tool_first", [False, True])
def test_same_owner_tool_capability_overlap_remains_valid(owner, tool_first):
    registry = PolicyRegistry()
    if tool_first:
        registry.register_tool(owner=owner, **fields())
    registry.register_capability("sample", owner=owner)
    if not tool_first:
        registry.register_tool(owner=owner, **fields())
    assert registry.freeze().capabilities["sample"] == owner


@pytest.mark.parametrize("category,destination", [
    (SinkCategory.NETWORK, "network"), (SinkCategory.HTTP_WEBHOOK, "network"),
    (SinkCategory.NOTIFICATION, "notification"),
    (SinkCategory.EXTERNAL_MCP, "external_mcp"),
    (SinkCategory.CROSS_CHANNEL, "message"), (SinkCategory.FORGE, "bound_pull_request"),
])
@pytest.mark.parametrize("extractor", [None, "payload", 42])
def test_outbound_sinks_require_callable_payload_even_without_profiles(category, destination, extractor):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="payload extractor"):
        registry.registrar("example").register_tool(**outbound(
            sink_category=category, sink_destination=destination, sink_payload_extractor=extractor,
        ))
    assert not registry.tools
    registry.registrar("example").register_tool(**outbound(
        sink_category=category, sink_destination=destination,
    ))
    assert registry.freeze().descriptors["sample"].sink_payload_extractor("sample", {"body": "secret"}, None) == ("secret",)


@pytest.mark.parametrize("field_name,value", [
    ("canonical", "worker"), ("canonical", "plugin:other:worker"),
    ("trigger", "periodic"), ("trigger", "plugin:other:tick"),
    ("canonical", "plugin:example:"), ("trigger", "plugin:example:tick:extra"),
    ("canonical", "plugin:example: worker"),
])
def test_principal_canonical_and_trigger_are_both_owner_namespaced(field_name, value):
    registry, plugin = worker_registry()
    with pytest.raises(PolicyRegistrationError, match="canonical and trigger must use plugin:example:"):
        plugin.register_service_principal(principal(**{field_name: value}))
    assert not registry.service_principals


@pytest.mark.parametrize("field_name,value", [
    ("sink_policies", (ServiceSinkPolicy("sample", "static_service_write_roots", "/"),)),
    ("filesystem_read_roots", ("/",)), ("saga_full_corpus_read", True),
    ("declared_shell_commands", (object(),)), ("owned_skill_directory", "/"),
    ("channel_memory_directory", "scheduler:heartbeat"),
    ("configured_delivery_channel", "private-channel"), ("proposal_surface", "admin"),
    ("capability_tier", CapabilityTier.UNBOUNDED),
])
def test_plugin_principal_cannot_mint_special_grants(field_name, value):
    registry, plugin = worker_registry()
    with pytest.raises(PolicyRegistrationError, match="cannot declare|tier is derived"):
        plugin.register_service_principal(principal(**{field_name: value}))
    assert not registry.service_principals


@pytest.mark.parametrize("flag", ["github_activity", "worklink_retained_checkout", "github_ci_watch"])
def test_new_core_only_service_flags_cannot_be_set_by_plugins(flag):
    # Exercise the real authority fields; a missing field is a regression, not a skip.
    registry, plugin = worker_registry()
    assert next(field for field in dataclass_fields(ServicePrincipal) if field.name == flag).default is False
    assert getattr(principal(), flag) is False
    with pytest.raises(PolicyRegistrationError, match=f"cannot declare {flag}"):
        plugin.register_service_principal(principal(**{flag: True}))
    assert not registry.service_principals


@pytest.mark.parametrize("profile_owner", ["core", "other"])
def test_static_principal_cannot_reuse_foreign_profile_even_with_own_tools(profile_owner):
    registry, plugin = worker_registry()
    # The foreign profile is independently valid. Failure must come from the
    # principal's owner check, not an earlier foreign-capability validation.
    registry.register_authority_profile("foreign", owner=profile_owner)
    plugin.register_service_principal(principal(authority_profile="foreign"))
    with pytest.raises(PolicyRegistrationError, match="requires its own authority profile"):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize("capability_owner", ["core", "other", "example"])
def test_static_principal_cannot_invoke_capability_only_grants(capability_owner):
    registry, plugin = worker_registry()
    registry.register_capability("not_a_tool", owner=capability_owner)
    plugin.register_authority_profile("extra", capabilities=("not_a_tool",))
    plugin.register_service_principal(principal(
        authority_profile="extra", capabilities=("not_a_tool",),
    ))
    with pytest.raises(PolicyRegistrationError, match="foreign capability|not its own tool"):
        registry.freeze()
    assert not registry.frozen


def test_principal_destinations_must_cover_declared_sink():
    registry, plugin = worker_registry()
    plugin.register_service_principal(principal(sink_destinations=("filesystem",)))
    with pytest.raises(PolicyRegistrationError, match="missing declared sink destination"):
        registry.freeze()
    assert not registry.frozen


def test_principal_capabilities_must_be_in_own_profile():
    registry, plugin = worker_registry()
    plugin.register_service_principal(principal(capabilities=("unregistered",)))
    with pytest.raises(PolicyRegistrationError, match="capabilities outside profile"):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize("tier,expected", [
    ("scope-contained", CapabilityTier.SCOPE_CONTAINED),
    ("scoped-with-provenance", CapabilityTier.SCOPED_WITH_PROVENANCE),
    ("code-execution", CapabilityTier.CODE_EXECUTION),
    ("unbounded", CapabilityTier.UNBOUNDED),
])
def test_principal_tier_uses_the_actual_tool_ceiling(tier, expected):
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**outbound(
        profiles=("worker",), tier=tier, service_sink_adapter="approved_urls",
    ))
    plugin.register_capability("sample")
    plugin.register_authority_profile("worker", capabilities=("sample",))
    plugin.register_service_principal(principal())
    assert registry.freeze().service_principals["plugin:example:tick"].capability_tier is expected


@pytest.mark.parametrize("category,destination,adapter,bound", [
    (SinkCategory.NETWORK, "network", "approved_urls", "MIMIR_EGRESS_APPROVED_URLS"),
    (SinkCategory.HTTP_WEBHOOK, "network", "approved_urls", "MIMIR_EGRESS_APPROVED_URLS"),
    (SinkCategory.NOTIFICATION, "notification", "operator_alert", "MIMIR_OPERATOR_ALERT_CHANNEL"),
    (SinkCategory.FILE, "filesystem", "trigger_service_write_roots", "[]"),
    (SinkCategory.SHELL_PROCESS, "shell_process", "shell_profile", "maintenance"),
    (SinkCategory.SPAWN, "spawn_process", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS"),
])
def test_executable_policies_and_tier_are_derived_not_plugin_supplied(category, destination, adapter, bound):
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**outbound(
        profiles=("worker",), tier="code-execution", sink_category=category,
        sink_destination=destination, service_sink_adapter=adapter,
    ))
    plugin.register_capability("sample")
    plugin.register_tool(**fields(name="read", profiles=("worker",), tier="scope-contained"))
    plugin.register_capability("read")
    plugin.register_authority_profile("worker", capabilities=("sample", "read"))
    plugin.register_shell_profile_mapping("worker", "maintenance")
    supplied = principal(capabilities=("read", "sample"), sink_destinations=(destination,))
    plugin.register_service_principal(supplied)
    selected = registry.freeze().service_principals[supplied.trigger]
    assert selected.capability_tier is CapabilityTier.CODE_EXECUTION
    assert selected.sink_policies == (ServiceSinkPolicy("sample", adapter, bound),)
    assert supplied.capability_tier is None and supplied.sink_policies == ()
    assert selected.filesystem_read_roots == () and not selected.saga_full_corpus_read


@pytest.mark.parametrize("shell_profile", [
    "scheduler_read_only", "repo_review", "maintenance", "session_boundary", "upgrade_workspace",
])
def test_known_shell_profiles_resolve_without_importing_access_control(shell_profile):
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_authority_profile("worker")
    plugin.register_shell_profile_mapping("worker", shell_profile)
    assert registry.freeze().shell_profile_mappings["worker"] == shell_profile


def test_shell_mapping_names_match_the_actual_executable_dispatch():
    # shell_profiles.py does not exist in this checkout; the dispatch lives here.
    import mimir.plugins.registry as module
    tree = ast.parse((Path(module.__file__).parents[1] / "access_control.py").read_text())
    dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "parse_service_shell_argv_with_diagnostics")
    known = {
        node.comparators[0].value for node in ast.walk(dispatch)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
        and node.left.id == "destination" and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq) and isinstance(node.comparators[0], ast.Constant)
    }
    assert module._SHELL_PROFILES == known


def test_operator_shell_profile_also_requires_executable_dispatch_and_rejection_is_atomic():
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="unknown shell profile: arbitrary"):
        registry.register_operator_shell_profile("arbitrary")
    assert registry.operator_shell_profile is None
    registry.register_operator_shell_profile("scheduler_read_only")
    assert registry.freeze().operator_shell_profile == "scheduler_read_only"


def test_unknown_shell_profile_cannot_be_a_dormant_grant():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_authority_profile("worker")
    with pytest.raises(PolicyRegistrationError, match="unknown shell profile: arbitrary"):
        plugin.register_shell_profile_mapping("worker", "arbitrary")
    assert not registry.shell_profile_mappings


def test_registry_import_closure_does_not_pull_in_access_control():
    import mimir.plugins.registry as module
    tree = ast.parse(Path(module.__file__).read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "mimir.access_control"
        or isinstance(node, ast.Import) and any(alias.name == "mimir.access_control" for alias in node.names)
        for node in imports
    )


@pytest.mark.parametrize("category,destination", [
    (SinkCategory.NETWORK, "network"), (SinkCategory.HTTP_WEBHOOK, "network"),
    (SinkCategory.NOTIFICATION, "notification"),
])
def test_outbound_extractor_is_used_to_block_private_payload(category, destination, tmp_path, monkeypatch):
    from langchain.agents.middleware import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import StructuredTool
    from langgraph.runtime import Runtime
    from mimir.models import AuthContext, InformationFlowLabels, InformationFlowState
    from mimir.tools import budget_gate

    private_term = "42 Maplewood Drive"
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    (tmp_path / "private-terms.txt").write_text(private_term + "\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_OUTBOUND_PRIVACY_ENFORCE", "1")
    events = []
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda event, **data: events.append((event, data)))
    registry = PolicyRegistry()
    register_core_tools(registry)

    def operation(endpoint: str, body: str) -> str:
        """Deliver an outbound payload."""
        return "delivered"

    registry.registrar("example").register_tool(**outbound(
        factory=lambda: StructuredTool.from_function(operation, name="sample"),
        sink_category=category, sink_destination=destination,
    ))
    policy = registry.freeze()
    auth = AuthContext(
        principal=None, canonical_principal=None, roles=("admin",), event_ingress=None,
        trigger="user_message", channel_id="scheduler:own", interactivity=None,
        enforcement_enabled=True, ifc_labels=InformationFlowLabels(), ifc_state=InformationFlowState(),
    )
    request = ToolCallRequest(
        tool_call={"name": "sample", "args": {"endpoint": "https://example.test/hook", "body": private_term},
                   "id": "privacy", "type": "tool_call"},
        tool=policy.tools["sample"].factory(), state=None, runtime=Runtime(context=auth),
    )
    executed = []

    def handler(_request):
        executed.append(True)
        return ToolMessage(content="delivered", tool_call_id="privacy")

    result = budget_gate.BudgetGateMiddleware(policy).wrap_tool_call(request, handler)
    assert result.status == "error" and executed == []
    assert any(data.get("reason") == "outbound_private_term" for _event, data in events), events
    assert private_term not in str(result.content) and private_term not in str(events)


@pytest.mark.parametrize("origin,domain", [
    (ResultOriginKind.NON_INGESTING, None), (ResultOriginKind.NON_INGESTING, "web"),
    (ResultOriginKind.EXTERNAL, "web"), (ResultOriginKind.NONE, None),
])
@pytest.mark.parametrize("result_kind", ["success", "error", "empty_artifact", "trusted_artifact"])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.asyncio
async def test_plugin_sink_result_still_taints_a_following_live_write(origin, domain, result_kind, enforce, mode, tmp_path, monkeypatch):
    from langchain.agents.middleware import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import StructuredTool
    from langgraph.runtime import Runtime
    from mimir.access_control import ProtectedResultProvenance, ToolRegistry
    from mimir.models import AuthContext, InformationFlowLabels, InformationFlowState, SourceLabel
    from mimir.plugins.core import policy_scope
    from mimir.tool_descriptors import get_tool_descriptor
    from mimir.tools import budget_gate
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *_args, **_kwargs: None)
    registry = PolicyRegistry()
    register_core_tools(registry)
    def operation(endpoint: str, body: str) -> str:
        """Send a payload and return an untrusted remote response."""
        return "remote content"
    registry.registrar("example").register_tool(**outbound(
        factory=lambda: StructuredTool.from_function(operation, name="sample"),
        result_origin=origin, result_domain=domain,
    ))
    policy = registry.freeze()
    auth = AuthContext(
        principal=None, canonical_principal=None, roles=("admin",), event_ingress=None,
        trigger="user_message", channel_id="scheduler:own", interactivity=None,
        enforcement_enabled=enforce, ifc_labels=InformationFlowLabels(), ifc_state=InformationFlowState(),
    )
    request = ToolCallRequest(
        tool_call={"name": "sample", "args": {"endpoint": "https://example.test/hook", "body": "safe"},
                   "id": "remote", "type": "tool_call"},
        tool=policy.tools["sample"].factory(), state=None, runtime=Runtime(context=auth),
    )
    executed = []
    write_arguments = {"file_path": str(tmp_path / "state" / "live.md"), "content": "remote response"}
    # Establish that this exact live write is allowed before the provider result.
    # Otherwise a static path/scope denial can make the regression pass vacuously.
    clean = ToolRegistry().authorize_tool(
        "write_file", auth, enforce=enforce, policy=policy, arguments=write_arguments,
    )
    assert clean.allowed, clean.reason
    with policy_scope(policy):
        assert get_tool_descriptor("sample") is policy.descriptors["sample"]
        assert get_tool_descriptor("write_file") is policy.descriptors["write_file"]

    def handler(_request):
        executed.append(True)
        result = ToolMessage(content="untrusted remote response", tool_call_id="remote",
                             status="error" if result_kind == "error" else "success")
        if result_kind == "empty_artifact":
            result.artifact = ProtectedResultProvenance(())
        elif result_kind == "trusted_artifact":
            result.artifact = ProtectedResultProvenance((SourceLabel(
                principal="operator", domain="web", resource_id="https://example.test/",
                bridge_instance=None, sensitivity="public",
                authorized_principals=frozenset({"operator"}), source_kind="protected_tool",
                integrity="trusted", integrity_effect="active_ingest",
            ),))
        return result

    middleware = BudgetGateMiddleware(policy)
    if mode == "async":
        async def async_handler(_request):
            return handler(_request)
        result = await middleware.awrap_tool_call(request, async_handler)
    else:
        result = middleware.wrap_tool_call(request, handler)
    assert executed == [True]
    assert result.status == ("error" if result_kind == "error" else "success")
    labels = auth.ifc_state.current(auth.ifc_labels)
    assert labels.has_untrusted_active_ingest
    assert {source.domain for source in labels.sources} == {domain or "unknown"}
    denial = ToolRegistry().authorize_tool(
        "write_file", auth, enforce=enforce, policy=policy, arguments=write_arguments,
    )
    assert not denial.allowed and denial.reason == "write_blocked_by_untrusted_ingest"

    def write_operation(file_path: str, content: str) -> str:
        """Write live state."""
        return "written"

    write_request = ToolCallRequest(
        tool_call={"name": "write_file", "args": write_arguments, "id": "write", "type": "tool_call"},
        tool=StructuredTool.from_function(write_operation, name="write_file"),
        state=None, runtime=Runtime(context=auth),
    )
    written = []
    def write_handler(_request):
        written.append(True)
        return ToolMessage(content="written", tool_call_id="write")
    if mode == "async":
        async def async_write_handler(_request):
            return write_handler(_request)
        blocked = await middleware.awrap_tool_call(write_request, async_write_handler)
    else:
        blocked = middleware.wrap_tool_call(write_request, write_handler)
    assert blocked.status == "error" and written == []


@pytest.mark.parametrize("metadata", [
    {"builtin_trigger_config": {
        "canonical": "heartbeat", "trigger": "scheduled_tick", "root_parts": ("..",),
        "tier": "unbounded", "channel_memory_directory": "scheduler:heartbeat",
        "creation_path": "example.worker:run", "saga_full_corpus_read": True,
    }},
    {"fetch_url_policy": ("approved_urls", "https://private.test/")},
    {"pr_review_scope": True},
])
def test_plugin_profile_cannot_launder_raw_builtin_authority(metadata):
    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="plugin profile cannot declare"):
        registry.registrar("example").register_authority_profile("worker", **metadata)
    assert not registry.profiles


@pytest.mark.parametrize("profile_owner", ["core", "other"])
@pytest.mark.parametrize("direct", [False, True])
def test_shell_mapping_requires_own_profile_at_both_registration_surfaces(profile_owner, direct):
    registry = PolicyRegistry()
    registry.register_authority_profile("foreign", owner=profile_owner)
    with pytest.raises(PolicyRegistrationError, match="cannot map foreign profile"):
        if direct:
            registry.register_shell_profile_mapping("foreign", "maintenance", owner="example")
        else:
            registry.registrar("example").register_shell_profile_mapping("foreign", "maintenance")
    assert not registry.shell_profile_mappings


def test_unmapped_plugin_shell_sink_uses_read_only_default():
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**outbound(
        profiles=("worker",), tier="code-execution", sink_category=SinkCategory.SHELL_PROCESS,
        sink_destination="shell_process", service_sink_adapter="shell_profile",
    ))
    plugin.register_capability("sample")
    plugin.register_authority_profile("worker", capabilities=("sample",))
    plugin.register_service_principal(principal(sink_destinations=("shell_process",)))
    selected = registry.freeze().service_principals["plugin:example:tick"]
    assert selected.sink_policies == (ServiceSinkPolicy("sample", "shell_profile", "scheduler_read_only"),)


@pytest.mark.parametrize("owner", ["core", "other"])
def test_plugin_profile_rejects_real_foreign_tool_not_only_capability_only_grants(owner):
    registry = PolicyRegistry()
    registry.register_tool(owner=owner, **fields(profiles=("worker",)))
    registry.register_capability("sample", owner=owner)
    registry.registrar("example").register_authority_profile("worker", capabilities=("sample",))
    with pytest.raises(PolicyRegistrationError, match="foreign capability sample"):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize("metadata,reason", [
    ({"authority_profile": None}, "requires its own authority profile"),
    ({"capabilities": ()}, "incomplete service principal"),
    ({"readable_domains": ()}, "incomplete service principal"),
    ({"sink_destinations": ()}, "incomplete service principal"),
    ({"creation_path": None}, "missing principal creation path"),
])
def test_static_principal_cannot_freeze_incomplete_authority(metadata, reason):
    registry, plugin = worker_registry()
    plugin.register_service_principal(principal(**metadata))
    with pytest.raises(PolicyRegistrationError, match=reason):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize("duplicate", ["canonical", "trigger"])
def test_namespaced_principal_collisions_are_rejected_without_replacing_first(duplicate):
    registry, plugin = worker_registry()
    first = principal()
    plugin.register_service_principal(first)
    second = principal(**{
        "canonical": "plugin:example:second", "trigger": "plugin:example:second-tick",
        duplicate: getattr(first, duplicate),
    })
    with pytest.raises(PolicyRegistrationError, match="duplicate service principal"):
        plugin.register_service_principal(second)
    assert dict(registry.service_principals) == {first.trigger: first}


@pytest.mark.parametrize("metadata", [
    {"service_sink_adapter": None},
    {"service_sink_adapter": "static_service_write_roots"},
    {"service_sink_adapter": "operator_alert"},
    {"sink_destination": "filesystem"},
])
def test_sink_adapter_cannot_be_missing_raw_or_cross_category(metadata):
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    plugin.register_tool(**outbound(
        **dict({"profiles": ("worker",), "service_sink_adapter": "approved_urls"}, **metadata),
    ))
    plugin.register_capability("sample")
    plugin.register_authority_profile("worker", capabilities=("sample",))
    plugin.register_service_principal(principal())
    with pytest.raises(PolicyRegistrationError, match="service sink adapter|service sink destination"):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize("method", ["register_tool", "register_authority_profile"])
def test_registrar_owner_override_cannot_reach_core_registration(method):
    registry = PolicyRegistry()
    plugin = registry.registrar("example")
    with pytest.raises(PolicyRegistrationError, match="owner cannot be overridden"):
        if method == "register_tool":
            plugin.register_tool(owner="core", **fields())
        else:
            plugin.register_authority_profile("worker", owner="core")
    assert not registry.tools and not registry.profiles


@pytest.mark.parametrize("flow,origin,domain", [
    ("sink", ResultOriginKind.NON_INGESTING, None),
    ("sink", ResultOriginKind.NON_INGESTING, "web"),
    ("sink", ResultOriginKind.NONE, None),
    ("sink", ResultOriginKind.EXTERNAL, "web"),
    ("source", ResultOriginKind.REPOSITORY, "repository"),
    ("both", ResultOriginKind.EXTERNAL, "web"),
    ("neither", ResultOriginKind.NONE, None),
])
@pytest.mark.parametrize("result_kind", [
    "success", "error", "exception", "empty_provenance", "empty_artifact",
    "trusted_provenance", "trusted_artifact", "typed_refusal",
])
def test_plugin_result_classifier_always_labels_provider_controlled_results(flow, origin, domain, result_kind):
    from langchain_core.messages import ToolMessage
    from mimir.access_control import (
        OperationDecision, ProtectedResultProvenance, ToolAuthorization, classify_protected_result,
    )
    from mimir.models import SourceLabel
    from mimir.tools.refusals import ToolPolicyRefusal

    registry = PolicyRegistry()
    # No runtime inventory validation here: classify even policy-only/unknown-domain records.
    registry.registrar("example").register_tool(**outbound(
        flow=flow, result_origin=origin, result_domain=domain,
    ))
    policy = registry.freeze()
    authorization = ToolAuthorization("sample", OperationDecision.OPEN, True, policy=policy)
    result = ToolMessage(content="provider content", tool_call_id="result")
    provenance = None
    refusal = None
    failed = result_kind in {"error", "exception", "typed_refusal"}
    if result_kind == "error":
        result = ToolMessage(content="provider failure content", tool_call_id="result", status="error")
    elif result_kind == "exception":
        result = RuntimeError("provider failure content")
    elif result_kind in {"empty_provenance", "empty_artifact"}:
        provenance = ProtectedResultProvenance(())
    elif result_kind in {"trusted_provenance", "trusted_artifact"}:
        provenance = ProtectedResultProvenance((SourceLabel(
            principal="operator", domain="web", resource_id="https://example.test/", bridge_instance=None,
            sensitivity="public", authorized_principals=frozenset({"operator"}),
            source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
        ),))
    elif result_kind == "typed_refusal":
        refusal = ToolPolicyRefusal("provider refusal text")
        result = ToolMessage(content=str(refusal), tool_call_id="result", status="error")
    if result_kind in {"empty_artifact", "trusted_artifact"}:
        result.artifact = provenance
        provenance = None
    labels = classify_protected_result(
        "sample", {}, None, authorization, result=result, provenance=provenance,
        policy_refusal=refusal, failed=failed,
    )
    assert labels is not None and labels.has_untrusted_active_ingest
    assert len(labels.sources) == 1
    source = next(iter(labels.sources))
    assert source.domain == (domain or "unknown")
    assert source.source_kind == "protected_tool"
    assert source.integrity == "untrusted" and source.integrity_effect == "active_ingest"
    assert source.principal is None and source.authorized_principals == frozenset()


@pytest.mark.parametrize("exemption", ["empty_provenance", "non_ingesting", "typed_refusal"])
def test_core_result_classifier_exemptions_remain_core_only(exemption):
    from langchain_core.messages import ToolMessage
    from mimir.access_control import (
        OperationDecision, ProtectedResultProvenance, ToolAuthorization, classify_protected_result,
    )
    from mimir.tools.refusals import ToolPolicyRefusal

    registry = PolicyRegistry()
    registry.register_tool(**outbound(
        result_domain=None, result_origin=(
            ResultOriginKind.NON_INGESTING if exemption == "non_ingesting" else ResultOriginKind.NONE
        ),
    ))
    authorization = ToolAuthorization("sample", OperationDecision.OPEN, True, policy=registry.freeze())
    refusal = ToolPolicyRefusal("core policy refusal") if exemption == "typed_refusal" else None
    result = ToolMessage(
        content=str(refusal) if refusal else "core empty result", tool_call_id="core",
        status="error" if refusal else "success",
    )
    assert classify_protected_result(
        "sample", {}, None, authorization, result=result,
        provenance=ProtectedResultProvenance(()) if exemption == "empty_provenance" else None,
        policy_refusal=refusal, failed=refusal is not None,
    ) is None


@pytest.mark.parametrize("tool_first", [False, True])
@pytest.mark.parametrize("owner", ["core", "example"])
def test_rejected_capability_registration_leaves_no_companion_or_sink_tier_grants(tool_first, owner):
    registry = PolicyRegistry()
    registry.register_capability("companion")
    if tool_first:
        registry.register_tool(**fields(tier="unbounded"))
    kwargs = dict(companions=("companion",), tier="scope-contained",
                  legacy_service_sink_tier="scope-contained")
    # With no tool, plugin core-only metadata rejects; with a tool, either
    # ownership or the lower-tier collision rejects before anything publishes.
    if owner == "core" and not tool_first:
        kwargs["tier"] = "invalid"
    with pytest.raises(PolicyRegistrationError):
        registry.register_capability("sample", owner=owner, **kwargs)
    assert "sample" not in registry.capabilities
    assert "sample" not in registry.capability_companions
    assert "sample" not in registry.legacy_service_sink_tiers
    if not tool_first:
        assert "sample" not in registry.tiers


@pytest.mark.parametrize("method", ["register_tool", "register_capability"])
@pytest.mark.parametrize("owner", ["core", "example", "other"])
def test_duplicate_tool_and_capability_records_never_replace_the_original(method, owner):
    registry = PolicyRegistry()
    registry.register_tool(owner="example", **fields())
    registry.register_capability("sample", owner="example")
    original = registry.tools["sample"]
    with pytest.raises(PolicyRegistrationError, match="duplicate tool|duplicate capability"):
        if method == "register_tool":
            registry.register_tool(owner=owner, **fields(tier="unbounded"))
        else:
            registry.register_capability("sample", owner=owner)
    assert registry.tools["sample"] is original
    assert dict(registry.capabilities) == {"sample": "example"}


def test_core_tool_can_preserve_reserved_mcp_prefix_without_plugin_privileges():
    registry = PolicyRegistry()
    registry.register_tool(**fields(name="mcp_core_alias"))
    assert registry.freeze().tools["mcp_core_alias"].owner == "core"


@pytest.mark.parametrize("canonical,flag", [
    ("poller:github-activity", "github_activity"),
    ("poller:worklink-ready-queue", "worklink_retained_checkout"),
    ("poller:github-ci-watch", "github_ci_watch"),
])
def test_poller_builder_uses_registry_properties_not_canonical_literals(canonical, flag):
    from mimir.access_control import build_trigger_service_principal
    from mimir.plugins.core import PollerProperties, core_policy_registry

    def build(policy):
        return build_trigger_service_principal(
            canonical=canonical, trigger="poller", profile="github",
            tier=CapabilityTier.SCOPE_CONTAINED, capabilities=(),
            creation_path="test:poller", policy=policy,
        )

    assert getattr(build(core_policy_registry()), flag) is True
    registry = PolicyRegistry()
    register_core_tools(registry)
    # Test-only replacement isolates the declaration's effect. A literal-name
    # fallback would incorrectly keep the property despite the selected policy.
    registry._poller_properties[canonical] = PollerProperties()
    selected = registry.freeze()
    assert getattr(build(selected), flag) is False
    assert getattr(build(core_policy_registry()), flag) is True
    with pytest.raises(TypeError):
        selected.poller_properties[canonical] = PollerProperties()
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        selected.register_poller_properties("poller:another", PollerProperties())


def test_poller_properties_are_core_only_and_duplicate_safe():
    from mimir.plugins.core import PollerProperties

    registry = PolicyRegistry()
    with pytest.raises(PolicyRegistrationError, match="core-only"):
        registry.register_poller_properties(
            "poller:example", PollerProperties(github_activity=True), owner="example",
        )
    assert not registry.poller_properties
    assert not hasattr(registry.registrar("example"), "register_poller_properties")
    properties = PollerProperties(github_activity=True)
    registry.register_poller_properties("poller:example", properties)
    with pytest.raises(PolicyRegistrationError, match="duplicate"):
        registry.register_poller_properties("poller:example", PollerProperties())
    assert registry.poller_properties["poller:example"] is properties


def test_authorization_module_contains_no_reserved_poller_name_literals():
    import mimir.access_control as module

    tree = ast.parse(Path(module.__file__).read_text())
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                and isinstance(node.value, str)}
    assert not literals.intersection({
        "poller:github-activity", "poller:worklink-ready-queue", "poller:github-ci-watch",
    })
