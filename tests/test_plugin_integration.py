from __future__ import annotations

from dataclasses import replace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime

from mimir.access_control import (
    CapabilityTier, ToolRegistry, build_trigger_service_principal,
    builtin_trigger_service_principal, get_service_principal,
    service_can_invoke_operation,
)
from mimir.models import (
    AuthContext, InformationFlowLabels, InformationFlowState, RepoPRAction,
    RepoPRActionScope, RepoPRScopeRegistry,
    RepoReviewState, RetainedFactoryScope,
)
from mimir.plugins.core import ServicePrincipal, register_core_tools
from mimir.plugins.registry import PolicyRegistrar, PolicyRegistry
from mimir.tool_descriptors import ResultOriginKind, SinkCategory
from mimir.tools.budget_gate import BudgetGateMiddleware, _authorize_tool_call


def _channel_target(_name, arguments, _context):
    return (arguments.get("channel_id"),)


def _repo_target(_name, arguments, _context):
    return (arguments.get("repository"),)


def _plugin_tool(
    channel_id: str | None = None,
    repository: str | None = None,
    pull_request: int | None = None,
) -> str:
    """Return a harmless result; middleware tests supply an observable handler."""
    return "result"


@pytest.fixture
def policy():
    registry = PolicyRegistry()
    register_core_tools(registry)
    registrar = PolicyRegistrar(registry, "example")
    for name, scope, extractor, category, flow, action, writes in (
        ("example_channel", "channel", _channel_target, SinkCategory.SAME_CHANNEL, "sink", None, False),
        ("example_repo", "repo_pr", _repo_target, SinkCategory.FORGE, "source", RepoPRAction.INSPECT, False),
        ("example_pr", "repo_pr", _repo_target, SinkCategory.FORGE, "source", RepoPRAction.PR_COMMENT, False),
        ("example_write", "repo_pr", _repo_target, SinkCategory.FORGE, "source", RepoPRAction.WRITE, True),
    ):
        registrar.register_tool(
            name=name,
            factory=lambda name=name: StructuredTool.from_function(
                _plugin_tool, name=name, description="Example registered plugin tool",
            ),
            flow=flow, tier="scope-contained",
            operation_class="resource_scoped", resource_scope=scope,
            typed_action=action, result_domain="repository", result_origin=ResultOriginKind.EXTERNAL,
            sink_category=category if flow == "sink" else None,
            sink_destination="message" if flow == "sink" else None,
            sink_target_extractor=extractor, sink_payload_extractor=None,
            writes_code_or_state=writes, profiles=(), read_backend=False,
        )
    return registry.freeze()


def _auth(**updates):
    auth = AuthContext(
        principal=None, canonical_principal=None, roles=("user",),
        event_ingress=None, trigger="user_message", channel_id="scheduler:own",
        interactivity=None, enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(), ifc_state=InformationFlowState(),
    )
    return replace(auth, **updates)


def _state(operations):
    return RepoReviewState(RepoPRActionScope(
        provenance="poller_payload", canonical_repo="acme/widget",
        canonical_root="/tmp/checkout", canonical_origin="https://github.com/acme/widget.git",
        principal="mimir-bot", event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(operations), pr_number=7,
        head_repo="acme/widget", head_remote="origin", destination_ref="refs/heads/fix",
        observed_head_sha="a" * 40, base_ref="main", observed_base_sha="b" * 40,
    ))


@pytest.fixture
def principal_policy():
    registry = PolicyRegistry()
    register_core_tools(registry)
    registrar = registry.registrar("example")
    registrar.register_tool(
        name="example_inspect", factory=lambda: StructuredTool.from_function(
            _plugin_tool, name="example_inspect", description="Read example state",
        ),
        flow="source", tier="scope-contained", operation_class="open",
        result_domain="repository", result_origin=ResultOriginKind.EXTERNAL,
        sink_category=None, sink_destination=None, sink_target_extractor=None,
        sink_payload_extractor=None, writes_code_or_state=False, profiles=(),
        readable_domain="repository", read_backend=False,
    )
    registrar.register_capability("example_inspect")
    registrar.add_to_profile("heartbeat", "example_inspect")
    registrar.register_tool(
        name="example_deliver", factory=lambda: StructuredTool.from_function(
            _plugin_tool, name="example_deliver", description="Deliver example result",
        ),
        flow="sink", tier="scope-contained", operation_class="admin_required",
        result_domain="repository", result_origin=ResultOriginKind.EXTERNAL,
        sink_category=SinkCategory.FILE, sink_destination="filesystem",
        sink_target_extractor=None, sink_payload_extractor=None,
        writes_code_or_state=False, profiles=(), read_backend=False,
    )
    registrar.register_capability("example_deliver")
    registrar.add_to_profile("heartbeat", "example_deliver")
    registrar.register_service_principal(ServicePrincipal(
        canonical="example:worker", trigger="example_worker",
        authority_profile="heartbeat", capability_tier=CapabilityTier.SCOPE_CONTAINED,
        capabilities=("example_inspect", "example_deliver"), readable_domains=("repository",),
        sink_destinations=("filesystem",), creation_path="example.worker:run",
    ))
    return registry.freeze()


def test_selected_builtin_issues_only_profile_membership(principal_policy, tmp_path):
    core = builtin_trigger_service_principal("heartbeat", tmp_path)
    selected = builtin_trigger_service_principal(
        "heartbeat", tmp_path, policy=principal_policy,
    )
    assert set(selected.capabilities) == set(core.capabilities) | {
        "example_inspect", "example_deliver",
    }
    assert not service_can_invoke_operation(core, "example_inspect", policy=principal_policy)
    assert service_can_invoke_operation(selected, "example_inspect", policy=principal_policy)
    assert not service_can_invoke_operation(selected, "example_inspect")
    assert not service_can_invoke_operation(selected, "example_unregistered", policy=principal_policy)
    assert not service_can_invoke_operation(
        replace(selected, readable_domains=()), "example_inspect", policy=principal_policy,
    )


def test_static_plugin_principal_and_explicit_instance_grants(principal_policy, tmp_path):
    assert get_service_principal("example_worker") is None
    static = get_service_principal("example_worker", policy=principal_policy)
    assert static is not None
    assert service_can_invoke_operation(static, "example_inspect", policy=principal_policy)
    assert not service_can_invoke_operation(static, "read_file", policy=principal_policy)
    assert service_can_invoke_operation(static, "example_deliver", policy=principal_policy)
    assert not service_can_invoke_operation(
        replace(static, sink_destinations=()), "example_deliver", policy=principal_policy,
    )
    instance = build_trigger_service_principal(
        canonical="poller:example", trigger="poller", profile="heartbeat",
        tier=CapabilityTier.SCOPE_CONTAINED, capabilities=("example_inspect",),
        roots=(tmp_path,), creation_path="example.poller:manifest", policy=principal_policy,
    )
    assert service_can_invoke_operation(instance, "example_inspect", policy=principal_policy)
    assert not service_can_invoke_operation(instance, "read_file", policy=principal_policy)
    assert get_service_principal("poller", policy=principal_policy) is None
    with pytest.raises(ValueError, match="outside.*profile"):
        build_trigger_service_principal(
            canonical="poller:example", trigger="poller", profile="custom",
            tier=CapabilityTier.SCOPE_CONTAINED, capabilities=("example_inspect",),
            creation_path="example.poller:manifest", policy=principal_policy,
        )


@pytest.mark.parametrize("enforce", [True, False])
def test_plugin_channel_requires_one_explicit_same_scope_target(policy, enforce):
    registry = ToolRegistry()
    auth = _auth()

    def decide(args):
        return registry.authorize_tool(
            "example_channel", auth, enforce=enforce, arguments=args, policy=policy,
        )

    assert decide({"channel_id": "scheduler:own"}).allowed
    for args in ({}, {"channel_id": ""}, {"channel_id": None}):
        refusal = decide(args)
        assert refusal.would_block and refusal.allowed is not enforce
        assert refusal.reason == "channel_target_required"
    other = decide({"channel_id": "scheduler:other"})
    assert other.reason == "cross_channel_scope" and other.would_block
    assert other.allowed is not enforce
    assert registry.authorize_tool(
        "example_channel", auth, enforce=True, target_channel="scheduler:other",
        arguments={"channel_id": "scheduler:own"}, policy=policy,
    ).reason == "channel_target_required"


@pytest.mark.parametrize("enforce", [True, False])
def test_plugin_repo_and_pr_require_exact_server_scope_and_action(policy, enforce, tmp_path):
    state = _state({RepoPRAction.INSPECT.value})
    auth = _auth(repo_pr_scope_registry=RepoPRScopeRegistry((state,)))
    registry = ToolRegistry()

    def decide(name, context, repository="acme/widget", number=7):
        return registry.authorize_tool(
            name, context, enforce=enforce,
            arguments={"repository": repository, "pull_request": number}, policy=policy,
        )

    assert decide("example_repo", auth).allowed
    for name, context, repository, number in (
        ("example_pr", auth, "acme/widget", 7),
        ("example_repo", auth, "acme/other", 7),
        ("example_repo", auth, "acme/widget", 8),
        ("example_repo", auth, "acme/widget", True),
        ("example_repo", _auth(), "acme/widget", 7),
    ):
        refusal = decide(name, context, repository, number)
        assert refusal.reason == "repo_pr_scope_denied" and refusal.would_block
        assert refusal.allowed is not enforce
    with_comment = _auth(repo_pr_scope_registry=RepoPRScopeRegistry((
        _state({RepoPRAction.PR_COMMENT.value}),
    )))
    assert decide("example_pr", with_comment).allowed
    retained = RetainedFactoryScope(
        issue_id=7, signature="sig", occurrence_id="occ", run_id="run", attempt=1,
        session="session", repository="acme/widget", branch="fix", sandbox=str(tmp_path),
    )
    refusal = decide("example_repo", _auth(retained_factory_scope=retained))
    assert refusal.reason == "repo_pr_scope_denied" and refusal.would_block


def test_plugin_channel_sink_ifc_and_middleware_policy(policy):
    auth = _auth()
    call = {"channel_id": "scheduler:own"}
    admitted, denial = _authorize_tool_call(
        "example_channel", auth, "scheduler:own", auth.ifc_labels,
        arguments=call, policy=policy,
    )
    assert admitted.allowed and denial is None
    unknown, denial = _authorize_tool_call(
        "example_channel", auth, "scheduler:own", auth.ifc_labels, arguments=call,
    )
    assert not unknown.allowed and denial is not None
    blocked, denial = _authorize_tool_call(
        "example_channel", replace(auth, ifc_labels=None), "scheduler:own",
        arguments=call, policy=policy,
    )
    assert blocked.reason == "missing_ifc_labels" and denial is not None
    assert BudgetGateMiddleware(policy)._policy is policy


def test_plugin_channel_rejects_ambiguous_extractor_targets(policy):
    original = policy.tools["example_channel"]
    ambiguous = replace(
        original, descriptor=replace(
            original.descriptor,
            sink_target_extractor=lambda _name, _args, _ctx: (
                "scheduler:own", "scheduler:other",
            ),
        ),
    )
    # A per-test frozen policy view; no mutation of core's shared registry.
    from types import MappingProxyType
    from unittest.mock import patch

    with patch.object(policy, "_tools", MappingProxyType({
        **policy.tools, "example_channel": ambiguous,
    })):
        decision = ToolRegistry().authorize_tool(
            "example_channel", _auth(), enforce=True,
            arguments={"channel_id": "scheduler:own"}, policy=policy,
        )
    assert decision.reason == "channel_target_required" and not decision.allowed


@pytest.mark.parametrize("enforce", [True, False])
def test_plugin_write_veto_applies_in_shadow_and_enforced(policy, enforce, monkeypatch):
    from mimir import access_control

    monkeypatch.setattr(access_control, "_turn_has_untrusted_active_ingest", lambda *_: True)
    decision = ToolRegistry().authorize_tool(
        "example_write", _auth(), enforce=enforce,
        arguments={"repository": "acme/widget", "pull_request": 7}, policy=policy,
    )
    assert not decision.allowed
    assert decision.reason == "write_blocked_by_untrusted_ingest"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["parent", "general-purpose"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
async def test_selected_plugin_resource_calls_cross_model_middleware_boundary(
    policy, tmp_path, monkeypatch, surface, asynchronous, enforce,
    middleware_event_logger,
):
    import deepagents

    from mimir.agent import Agent
    from mimir.config import Config
    from mimir.history import MessageBuffer
    from mimir.index import IndexGenerator
    from mimir.plugins.manager import PluginManager
    from mimir.turn_logger import TurnLogger

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_PLUGINS", raising=False)
    config = replace(Config.from_env(), plugin_manager=PluginManager(policy, ()))
    agent = Agent(
        config=config, turn_logger=TurnLogger(config.turns_log),
        message_buffer=MessageBuffer(history_path=tmp_path / "messages.jsonl"),
        index_generator=IndexGenerator(tmp_path),
    )
    monkeypatch.setattr(agent, "_current_system_prompt", lambda **_kwargs: "test prompt")
    monkeypatch.setattr("mimir.agent.resolve_model_from_config", lambda *args, **kwargs: object())
    captured = []
    graph = object()
    monkeypatch.setattr(deepagents, "create_deep_agent", lambda **kwargs: captured.append(kwargs) or graph)
    assert await agent._build_agent_if_needed() is graph
    assembly = captured[0]
    middleware = (
        assembly["middleware"] if surface == "parent"
        else next(child for child in assembly["subagents"]
                  if child["name"] == "general-purpose")["middleware"]
    )
    gate = next(item for item in middleware if isinstance(item, BudgetGateMiddleware))
    assert gate._policy is policy
    decisions = []
    shadow_decisions = []
    original_authorize = ToolRegistry.authorize_tool
    original_shadow = ToolRegistry._emit_shadow_decision

    def observe_authorization(self, *args, **kwargs):
        decision = original_authorize(self, *args, **kwargs)
        decisions.append(decision)
        return decision

    def observe_shadow(self, decision, **kwargs):
        shadow_decisions.append(decision)
        return original_shadow(self, decision, **kwargs)

    monkeypatch.setattr(ToolRegistry, "authorize_tool", observe_authorization)
    monkeypatch.setattr(ToolRegistry, "_emit_shadow_decision", observe_shadow)

    inspect = _auth(
        enforcement_enabled=enforce,
        repo_pr_scope_registry=RepoPRScopeRegistry((
            _state({RepoPRAction.INSPECT.value}),
        )),
    )
    comment = _auth(
        enforcement_enabled=enforce,
        repo_pr_scope_registry=RepoPRScopeRegistry((
            _state({RepoPRAction.PR_COMMENT.value}),
        )),
    )
    retained = RetainedFactoryScope(
        issue_id=7, signature="sig", occurrence_id="occ", run_id="run", attempt=1,
        session="session", repository="acme/widget", branch="fix", sandbox=str(tmp_path),
    )
    cases = (
        ("example_channel", {"channel_id": "scheduler:own"}, inspect, None),
        ("example_channel", {}, inspect, "channel_target_required"),
        ("example_channel", {"channel_id": "scheduler:other"}, inspect, "cross_channel_scope"),
        ("example_repo", {"repository": "acme/widget", "pull_request": 7}, inspect, None),
        ("example_pr", {"repository": "acme/widget", "pull_request": 7}, comment, None),
        ("example_repo", {"repository": "acme/other", "pull_request": 7}, inspect, "repo_pr_scope_denied"),
        ("example_repo", {"repository": "acme/widget", "pull_request": 8}, inspect, "repo_pr_scope_denied"),
        ("example_pr", {"repository": "acme/widget", "pull_request": 7}, inspect, "repo_pr_scope_denied"),
        ("example_repo", {"repository": "acme/widget", "pull_request": 7},
         _auth(enforcement_enabled=enforce, retained_factory_scope=retained), "repo_pr_scope_denied"),
    )
    for index, (name, args, context, refusal_reason) in enumerate(cases):
        called = []
        request = ToolCallRequest(
            tool_call={"name": name, "args": args, "id": f"plugin-{index}", "type": "tool_call"},
            tool=policy.tools[name].factory(), state=None, runtime=Runtime(context=context),
        )

        def handler(tool_request):
            called.append(tool_request.tool_call["id"])
            return ToolMessage(content="executed", tool_call_id=tool_request.tool_call["id"])

        async def async_handler(tool_request):
            return handler(tool_request)

        result = (
            await gate.awrap_tool_call(request, async_handler)
            if asynchronous else gate.wrap_tool_call(request, handler)
        )
        decision = decisions[-1]
        expected_reason = (
            "same_scope_channel" if name == "example_channel" and refusal_reason is None
            else "ifc_label_blocked:same_channel" if index in {1, 2} and enforce
            else refusal_reason
        )
        assert decision.reason == expected_reason
        if refusal_reason is None or not enforce:
            assert result.status != "error"
            assert called == [f"plugin-{index}"]
        else:
            assert result.status == "error"
            assert called == []
            assert expected_reason in str(result.content)
        if refusal_reason is not None:
            assert decision.would_block
            assert decision.is_shadow_decision is not enforce
            if not enforce:
                assert shadow_decisions[-1].would_block
                assert shadow_decisions[-1].tool_name == name
                assert shadow_decisions[-1].reason == (
                    "ifc_label_blocked:same_channel" if index in {1, 2}
                    else refusal_reason
                )
