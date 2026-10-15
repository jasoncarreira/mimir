from __future__ import annotations

from dataclasses import replace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime

from mimir.access_control import (
    ScopedPRGrants, _SCOPED_PR_ISSUER, _valid_scoped_pr_grant,
    create_auth_context,
)
from mimir.models import (
    AgentEvent, AuthContext, RepoPRScopeRegistry, RepoReviewState,
    ServerDiscoveredPRStates, TurnInteractivity,
)
from mimir.plugins.registry import PolicyRegistry
from mimir.pr_checkout_lease import create_pr_checkout_lease
from mimir.tool_descriptors import ResultOriginKind
from mimir.tools.budget_gate import BudgetGateMiddleware
from mimir_cultivate.policy import RepoPRAction, register_action_protocol, validate_scoped_pr_action
from tests.test_pr_checkout_lease import _repo_and_scope
from tests.test_access_control import _github_scope_test_setup


def _tool(repository: str, pull_request: int) -> str:
    return "ok"


@pytest.fixture
def policy():
    registry = PolicyRegistry()
    registrar = registry.registrar("cultivate")
    register_action_protocol(registrar, validate_scoped_pr_action)
    for name, action in (("scoped_checkout", RepoPRAction.CHECKOUT),
                         ("scoped_commit", RepoPRAction.COMMIT)):
        registrar.register_tool(
            name=name, factory=lambda name=name: StructuredTool.from_function(
                _tool, name=name, description="Scoped test tool",
            ), flow="source", tier="scope-contained", operation_class="resource_scoped",
            resource_scope="repo_pr", typed_action=action, result_domain="repository",
            result_origin=ResultOriginKind.REPOSITORY, sink_category=None,
            sink_destination=None, sink_target_extractor=None, sink_payload_extractor=None,
            writes_code_or_state=False, profiles=(), read_backend=False,
        )
    return registry.freeze()


def _auth(policy, state, enforce, *, discovered=False):
    grants = ScopedPRGrants(
        "poller:github", () if discovered else (state.action_scope,),
        protocols=policy.scoped_actions, _issuer=_SCOPED_PR_ISSUER,
    )
    cache = ServerDiscoveredPRStates()
    if discovered:
        cache.remember(state)
    return AuthContext(
        principal="poller:github", canonical_principal="poller:github", roles=(),
        event_ingress=None, trigger="poller", channel_id="poller:github",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        enforcement_enabled=enforce, scoped_pr_grants=grants,
        repo_pr_scope_registry=None if discovered else RepoPRScopeRegistry((state,)),
        server_discovered_pr_states=cache if discovered else None,
    )


def test_trusted_poller_issues_checkout_grant_but_http_ingress_does_not(policy, tmp_path, monkeypatch):
    _root, authority, item = _github_scope_test_setup(tmp_path, monkeypatch)
    event = AgentEvent(
        trigger="poller", channel_id="poller:github-activity",
        service_principal=authority.canonical, service_authority=authority,
        extra={"poller_name": "github-activity", "items": [item]},
    )
    trusted = create_auth_context(event, policy=policy, enforce=True)
    protocol = policy.scoped_actions["cultivate"]
    assert trusted.repo_pr_action_scope is not None
    assert _valid_scoped_pr_grant(
        trusted, "cultivate", RepoPRAction.CHECKOUT, protocol,
        trusted.repo_pr_action_scope, trusted.repo_review_state,
    )
    assert not _valid_scoped_pr_grant(
        trusted, "cultivate", RepoPRAction.COMMIT, protocol,
        trusted.repo_pr_action_scope, trusted.repo_review_state,
    )
    untrusted = create_auth_context(event, event_ingress="http_event", policy=policy, enforce=False)
    assert untrusted.scoped_pr_grants is None


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("discovered", [False, True])
async def test_checkout_grant_consumed_and_follow_on_bound_to_live_lease(
    policy, tmp_path, monkeypatch, asynchronous, enforce, discovered,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = _auth(policy, state, enforce, discovered=discovered)
    gate = BudgetGateMiddleware(policy)
    calls = []

    async def invoke(name, handler):
        request = ToolCallRequest(
            tool_call={"name": name, "args": {
                "repository": scope.canonical_repo, "pull_request": scope.pr_number,
            }, "id": name, "type": "tool_call"},
            tool=policy.tools[name].factory(), state=None, runtime=Runtime(context=auth),
        )
        if asynchronous:
            async def async_handler(tool_request):
                return handler(tool_request)
            return await gate.awrap_tool_call(request, async_handler)
        return gate.wrap_tool_call(request, handler)

    def follow(_request):
        calls.append("commit")
        return ToolMessage(content="ok", tool_call_id="scoped_commit")

    assert (await invoke("scoped_commit", follow)).status == "error"
    assert calls == []

    def checkout(_request):
        calls.append("checkout")
        create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
        return ToolMessage(content="ok", tool_call_id="scoped_checkout")

    assert (await invoke("scoped_checkout", checkout)).status != "error"
    assert calls == ["checkout"]
    assert (await invoke("scoped_checkout", checkout)).status == "error"
    assert calls == ["checkout"]
    assert (await invoke("scoped_commit", follow)).status != "error"
    assert calls == ["checkout", "commit"]
    protocol = policy.scoped_actions["cultivate"]
    assert not _valid_scoped_pr_grant(
        auth, "foreign", RepoPRAction.COMMIT, protocol, scope, state,
    )
    assert not _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.COMMIT, protocol,
        replace(scope, observed_head_sha="f" * 40), state,
    )
    state.checkout_lease.revoke()
    assert (await invoke("scoped_commit", follow)).status == "error"
    assert calls == ["checkout", "commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("invalid", ["missing", "expired", "foreign", "failed_checkout"])
async def test_invalid_grant_never_invokes_handler(
    policy, tmp_path, monkeypatch, asynchronous, enforce, invalid,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    auth = _auth(policy, state, enforce)
    grants = auth.scoped_pr_grants
    key = ("cultivate", scope.scope_id)
    if invalid == "missing":
        grants._prelease.clear()
    elif invalid == "expired":
        grants._prelease[key] = replace(grants._prelease[key], expires_at=0)
    elif invalid == "foreign":
        grants._prelease[key] = replace(grants._prelease[key], owner="foreign")
    called = []
    request = ToolCallRequest(
        tool_call={"name": "scoped_checkout", "args": {
            "repository": scope.canonical_repo, "pull_request": scope.pr_number,
        }, "id": "checkout", "type": "tool_call"},
        tool=policy.tools["scoped_checkout"].factory(), state=None,
        runtime=Runtime(context=auth),
    )

    def handler(_request):
        called.append(True)
        return ToolMessage(content="ok", tool_call_id="checkout")

    gate = BudgetGateMiddleware(policy)
    if asynchronous:
        async def async_handler(tool_request):
            return handler(tool_request)
        result = await gate.awrap_tool_call(request, async_handler)
    else:
        result = gate.wrap_tool_call(request, handler)
    assert result.status == "error"
    assert called == ([True] if invalid == "failed_checkout" else [])


def test_reminted_server_scope_issues_only_its_current_checkout_grant(policy, tmp_path):
    _repo, scope = _repo_and_scope(tmp_path)
    old = RepoReviewState(scope)
    auth = _auth(policy, old, True)
    registry = ServerDiscoveredPRStates()
    auth = replace(auth, server_discovered_pr_states=registry)
    args = {"repository": scope.canonical_repo, "pull_request": scope.pr_number}
    from mimir.access_control import ToolRegistry

    assert ToolRegistry().authorize_tool(
        "scoped_checkout", auth, enforce=True, arguments=args, policy=policy,
    ).allowed
    fresh = RepoReviewState(replace(scope, provenance="server_discovered", observed_head_sha="f" * 40))
    assert fresh.action_scope.scope_id != old.action_scope.scope_id
    assert registry.begin_remint(old.repo, old.pr_number)
    assert registry.remember_remint(old, fresh) is fresh
    decision = ToolRegistry().authorize_tool(
        "scoped_checkout", auth, enforce=True, arguments=args, policy=policy,
    )
    assert decision.allowed and decision.repo_pr_action_scope is fresh.action_scope
    assert not _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.COMMIT, policy.scoped_actions["cultivate"],
        old.action_scope, old,
    )
