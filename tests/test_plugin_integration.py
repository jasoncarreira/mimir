from __future__ import annotations

from dataclasses import replace

import pytest

from mimir.access_control import ToolRegistry
from mimir.models import (
    AuthContext, InformationFlowLabels, RepoPRAction, RepoPRActionScope, RepoPRScopeRegistry,
    RepoReviewState, RetainedFactoryScope,
)
from mimir.plugins.core import register_core_tools
from mimir.plugins.registry import PolicyRegistrar, PolicyRegistry
from mimir.tool_descriptors import ResultOriginKind, SinkCategory
from mimir.tools.budget_gate import BudgetGateMiddleware, _authorize_tool_call


def _channel_target(_name, arguments, _context):
    return (arguments.get("channel_id"),)


def _repo_target(_name, arguments, _context):
    return (arguments.get("repository"),)


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
            name=name, factory=lambda: None, flow=flow, tier="scope-contained",
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
        ifc_labels=InformationFlowLabels(),
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
