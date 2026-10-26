from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime
from langchain_core.tools import ToolException

from mimir.access_control import (
    ScopedPRGrants, _SCOPED_PR_ISSUER, _valid_scoped_pr_grant,
    create_auth_context, consume_scoped_pr_checkout, observe_scoped_pr_checkout,
    ToolRegistry, CapabilityTier, build_trigger_service_principal,
    publish_protected_result,
)
from mimir.models import (
    AgentEvent, AuthContext, InformationFlowLabels, InformationFlowState,
    SourceLabel, TurnInteractivity,
)
from mimir.plugins.registry import PolicyRegistry
from mimir_cultivate.repository import active_pr_checkout_lease_for_path, create_pr_checkout_lease
from mimir.tool_descriptors import ResultOriginKind
from mimir.tools.budget_gate import BudgetGateMiddleware
from mimir_cultivate.policy import (
    RepoPRAction, RepoPRScopeRegistry, RepoReviewState, ServerDiscoveredPRStates,
    register_action_protocol, validate_scoped_pr_action,
)
from tests.test_pr_checkout_lease import _repo_and_scope
from tests.test_access_control import _github_scope_test_setup


def _tool(repository: str, pull_request: int) -> str:
    return "ok"


@pytest.fixture
def policy():
    from tests.test_plugins_registry import selected_candidate

    registry = PolicyRegistry()
    registry.select_plugin_candidates((selected_candidate(),))
    registrar = registry.registrar("cultivate")
    register_action_protocol(
        registrar, validate_scoped_pr_action, checkout_tool="scoped_checkout",
    )
    for name, action in (("scoped_checkout", RepoPRAction.CHECKOUT),
                         ("scoped_fetch", RepoPRAction.CHECKOUT),
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


def test_checkout_action_follow_on_is_bound_to_designated_tool_and_live_lease(
    policy, tmp_path, monkeypatch,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = _auth(policy, state, True)
    protocol = policy.scoped_actions["cultivate"]
    assert protocol.checkout_tool == "scoped_checkout"
    assert _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.CHECKOUT, protocol, scope, state,
        tool_name="scoped_checkout",
    )
    assert not _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.CHECKOUT, protocol, scope, state,
        tool_name="scoped_fetch",
    )

    token = consume_scoped_pr_checkout(auth, scope, "cultivate")
    assert token is not None
    lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
    assert observe_scoped_pr_checkout(auth, scope, state, token, "cultivate", protocol)
    assert not _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.CHECKOUT, protocol, scope, state,
        tool_name="scoped_checkout",
    )
    assert _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.CHECKOUT, protocol, scope, state,
        tool_name="scoped_fetch",
    )
    lease.revoke()
    assert not _valid_scoped_pr_grant(
        auth, "cultivate", RepoPRAction.CHECKOUT, protocol, scope, state,
        tool_name="scoped_fetch",
    )


def test_trusted_poller_issues_checkout_grant_but_http_ingress_does_not(tmp_path, monkeypatch):
    from mimir.plugins.manager import load_plugins

    policy = load_plugins(enable="cultivate").policy
    _root, authority, item = _github_scope_test_setup(
        tmp_path, monkeypatch, selected_policy=policy,
    )
    event = AgentEvent(
        trigger="poller", channel_id="poller:github-activity",
        service_principal=authority.canonical, service_authority=authority,
        extra={"poller_name": "github-activity", "items": [item]},
    )
    trusted = create_auth_context(event, policy=policy, enforce=True)
    protocol = policy.scoped_actions["cultivate"]
    assert trusted.repo_pr_action_scope is not None
    assert not _valid_scoped_pr_grant(
        trusted, "cultivate", RepoPRAction.CHECKOUT, protocol,
        trusted.repo_pr_action_scope, trusted.repo_review_state,
    )
    assert _valid_scoped_pr_grant(
        trusted, "cultivate", RepoPRAction.CHECKOUT, protocol,
        trusted.repo_pr_action_scope, trusted.repo_review_state,
        tool_name="repo_checkout",
    )
    assert not _valid_scoped_pr_grant(
        trusted, "cultivate", RepoPRAction.COMMIT, protocol,
        trusted.repo_pr_action_scope, trusted.repo_review_state,
    )
    untrusted = create_auth_context(event, event_ingress="http_event", policy=policy, enforce=False)
    assert untrusted.scoped_pr_grants is None


@pytest.mark.parametrize("read", ["pr_metadata", "pr_diff", "pr_checks", "pr_comments", "pr_file_content"])
def test_precheckout_pr_read_reports_checkout_remedy_and_recovers(
    tmp_path, monkeypatch, read,
):
    from mimir.plugins.manager import load_plugins
    policy = load_plugins(enable="cultivate").policy
    _repo, scope = _repo_and_scope(tmp_path)
    scope = replace(scope, event_type="pr_opened", principal="collaborator",
                    pull_request_author="collaborator")
    state = RepoReviewState(scope)
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = _auth(policy, state, True)
    registry = ToolRegistry()
    args = {"repository": scope.canonical_repo, "pull_request": scope.pr_number}
    if read == "pr_file_content":
        args["path"] = "file.txt"
    refused = registry.authorize_tool(read, auth, arguments=args, enforce=True, policy=policy)
    assert not refused.allowed and refused.reason == "repo_pr_grant_denied"
    assert refused.refusal_detail == (
        "Call repo_checkout(repository, pull_request) for this repository and PR first."
    )
    request = ToolCallRequest(
        tool_call={"name": read, "args": args, "id": read, "type": "tool_call"},
        tool=policy.tools[read].factory(), state=None, runtime=Runtime(context=auth),
    )
    calls = []
    surfaced = BudgetGateMiddleware(policy).wrap_tool_call(
        request, lambda _request: calls.append(read) or ToolMessage(content="ok", tool_call_id=read),
    )
    assert surfaced.status == "error" and "repo_checkout" in str(surfaced.content)
    assert not calls
    token = consume_scoped_pr_checkout(auth, scope, "cultivate")
    assert token is not None
    lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
    assert observe_scoped_pr_checkout(auth, scope, state, token, "cultivate", policy.scoped_actions["cultivate"])
    assert registry.authorize_tool(read, auth, arguments=args, enforce=True, policy=policy).allowed
    lease.revoke()
    expired = registry.authorize_tool(read, auth, arguments=args, enforce=True, policy=policy)
    assert not expired.allowed and expired.refusal_detail is None


def test_expired_precheckout_grant_does_not_offer_checkout_remedy(tmp_path):
    from mimir.plugins.manager import load_plugins

    policy = load_plugins(enable="cultivate").policy
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    auth = _auth(policy, state, True)
    grants = auth.scoped_pr_grants
    key = ("cultivate", scope.scope_id)
    grants._prelease[key] = replace(grants._prelease[key], expires_at=0)
    refusal = ToolRegistry().authorize_tool(
        "pr_metadata", auth,
        arguments={"repository": scope.canonical_repo, "pull_request": scope.pr_number},
        enforce=True, policy=policy,
    )
    assert refusal.reason == "repo_pr_grant_denied"
    assert refusal.refusal_detail is None


def test_unrequested_collaborator_review_recovers_in_same_poller_turn(tmp_path, monkeypatch):
    from mimir.plugins.manager import load_plugins
    from mimir.access_control import RepoBindingResolution
    from mimir_cultivate import forge
    from mimir_cultivate.forge import CheckProjection, PullRequestProjection

    policy = load_plugins(enable="cultivate").policy
    repo, checkout_scope = _repo_and_scope(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", "origin", "HEAD:refs/pull/7/head"],
        check=True,
    )
    monkeypatch.setenv("GITHUB_REPOS", "SquashMerge/trellis")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")
    monkeypatch.setattr(
        "mimir.access_control._canonical_repo_binding_resolution",
        lambda _name: RepoBindingResolution(
            (str(repo.resolve()), checkout_scope.canonical_origin), (str(repo.resolve()),), 1,
        ),
    )
    item = {
        "event_type": "pr_opened", "repo": "SquashMerge/trellis", "number": 7,
        "author": "test", "pr_author_is_trusted": True,
        "head_repo": "SquashMerge/trellis", "head_remote": "origin",
        "head_ref": checkout_scope.head_ref,
        "head_sha": checkout_scope.observed_head_sha,
        "base_ref": checkout_scope.base_ref,
        "base_sha": checkout_scope.observed_base_sha,
    }
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    service = build_trigger_service_principal(
        canonical="poller:github-activity", trigger="poller", profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=("repo_checkout", "pr_metadata", "pr_diff", "pr_checks",
                      "pr_comments", "pr_submit_review", "pr_review_others"),
        creation_path="test", policy=policy, poller_owner="cultivate",
    )
    event = AgentEvent(
        trigger="poller", channel_id="poller:github-activity",
        service_principal=service.canonical, service_authority=service,
        extra={"poller_name": "github-activity", "items": [item]},
    )
    labels = InformationFlowLabels()
    auth = replace(
        create_auth_context(event, policy=policy, enforce=True),
        ifc_labels=labels, ifc_state=InformationFlowState(labels=labels),
    )
    assert auth.repo_pr_action_scope is not None
    state = auth.repo_pr_scope_registry.resolve("squashmerge/trellis", 7)
    scope = state.action_scope
    client = SimpleNamespace(
        get_pull_request_snapshot=lambda *_: SimpleNamespace(
            repo=scope.canonical_repo, number=scope.pr_number,
            state="open", head_sha=scope.observed_head_sha,
        ),
        get_pull_request=lambda _: PullRequestProjection(
            7, "Title", "open", "test", False, "main", "worklink/7",
            scope.observed_head_sha, True, "created", "updated",
        ),
        author_is_trusted=lambda *_: True,
        list_checks=lambda _: (
            CheckProjection("test (ubuntu-latest)", "completed", "success", None, None,
                            "https://github.com/SquashMerge/trellis/actions/runs/40411654556/job/1"),
            CheckProjection("test (macos-latest)", "completed", "success", None, None,
                            "https://github.com/SquashMerge/trellis/actions/runs/40411654556/job/2"),
        ),
    )
    monkeypatch.setattr(forge, "_client_for_repository", lambda _repo: client)
    # The local fixture uses a bare file:// origin; this test exercises the
    # bound review turn while origin validation is covered separately.
    monkeypatch.setattr(forge, "_github_origin_matches", lambda _: True)
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = True
    assert not auth.ifc_state.has_untrusted_active_ingest()
    gate = BudgetGateMiddleware(policy)
    calls = []
    args = {"repository": scope.canonical_repo, "pull_request": scope.pr_number}

    def publish_attestation():
        principal = "service:poller:github-activity"
        publish_protected_result((SourceLabel(
            principal=principal, domain="repository",
            resource_id=f"{scope.canonical_repo}#pull/{scope.pr_number}@{scope.observed_head_sha}",
            bridge_instance="forge", sensitivity="internal",
            authorized_principals=frozenset({principal}),
            source_kind="protected_tool", integrity="trusted",
            integrity_effect="active_ingest",
        ),))

    def invoke(name, handler, **extra):
        request = ToolCallRequest(
            tool_call={"name": name, "args": {**args, **extra}, "id": name, "type": "tool_call"},
            tool=policy.tools[name].factory(), state=None, runtime=Runtime(context=auth),
        )
        return gate.wrap_tool_call(request, handler)

    def checkout(_request):
        calls.append("repo_checkout")
        create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
        publish_attestation()
        return ToolMessage(content="checked out", tool_call_id="repo_checkout")

    decision = ToolRegistry().authorize_tool(
        "repo_checkout", auth, arguments=args, enforce=True, policy=policy,
    )
    assert decision.allowed, decision.reason
    assert invoke("repo_checkout", checkout).status != "error"
    for name in ("pr_metadata", "pr_diff", "pr_checks", "pr_comments"):
        def read(_request, name=name):
            calls.append(name)
            if name == "pr_checks":
                checks = forge.pr_checks.func(
                    scope.canonical_repo, scope.pr_number,
                    runtime=Runtime(context=auth),
                )
                assert len(checks) == 2
            else:
                publish_attestation()
            return ToolMessage(content="review evidence", tool_call_id=name)

        assert invoke(name, read).status != "error"
    assert not auth.ifc_state.has_untrusted_active_ingest()
    def submit(_request):
        calls.append("pr_submit_review")
        return ToolMessage(content="posted", tool_call_id="pr_submit_review")

    assert invoke("pr_submit_review", submit, body="Reviewed", verdict="comment").status != "error"
    assert calls == ["repo_checkout", "pr_metadata", "pr_diff", "pr_checks",
                     "pr_comments", "pr_submit_review"]


def test_legacy_scope_id_preserves_recorded_checkout_lease(tmp_path, monkeypatch):
    _repo, scope = _repo_and_scope(tmp_path)
    legacy_authority = {
        "provenance": scope.provenance,
        "canonical_repo": scope.canonical_repo,
        "canonical_root": scope.canonical_root,
        "canonical_origin": scope.canonical_origin,
        "principal": scope.principal,
        "event_type": scope.event_type,
        "allowed_operations": sorted(scope.allowed_operations),
        "pr_number": scope.pr_number,
        "head_repo": scope.head_repo,
        "head_remote": scope.head_remote,
        "destination_ref": scope.destination_ref,
        "observed_head_sha": scope.observed_head_sha,
        "base_ref": scope.base_ref,
        "observed_base_sha": scope.observed_base_sha,
    }
    if scope.checkout_ref is not None:
        legacy_authority["checkout_ref"] = scope.checkout_ref
    if scope.pull_request_author is not None:
        legacy_authority["pull_request_author"] = scope.pull_request_author
    legacy_id = hashlib.sha256(json.dumps(
        legacy_authority, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert scope.scope_id == legacy_id
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    state = RepoReviewState(scope)
    lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
    recovered = active_pr_checkout_lease_for_path(lease.path)
    assert recovered is not None and recovered.scope_id == legacy_id
    restarted = RepoReviewState(replace(scope))
    restarted.attach_checkout_lease(recovered)
    assert restarted.checkout_lease.recovery_id == lease.recovery_id


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
@pytest.mark.parametrize("invalid", [
    "absent_scope", "missing_action", "foreign_grant", "expired_grant",
    "changed_head", "replaced_lease", "mismatched_lease", "revoked_lease",
])
async def test_follow_on_refused_before_registered_handler(
    policy, tmp_path, monkeypatch, asynchronous, enforce, invalid,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = _auth(policy, state, enforce)
    if invalid not in {"absent_scope", "missing_action"}:
        token = consume_scoped_pr_checkout(auth, scope, "cultivate")
        assert token is not None
        lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
        assert observe_scoped_pr_checkout(
            auth, scope, state, token, "cultivate", policy.scoped_actions["cultivate"],
        )
        grant = auth.scoped_pr_grants._follow[("cultivate", scope.scope_id)][RepoPRAction.COMMIT.value]
        if invalid == "foreign_grant":
            auth.scoped_pr_grants._follow[("cultivate", scope.scope_id)][RepoPRAction.COMMIT.value] = replace(
                grant, owner="foreign",
            )
        elif invalid == "expired_grant":
            auth.scoped_pr_grants._follow[("cultivate", scope.scope_id)][RepoPRAction.COMMIT.value] = replace(
                grant, expires_at=0,
            )
        elif invalid == "changed_head":
            state = RepoReviewState(replace(scope, observed_head_sha="f" * 40))
            auth = replace(auth, repo_pr_scope_registry=RepoPRScopeRegistry((state,)))
        elif invalid == "replaced_lease":
            lease.revoke()
            create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
        elif invalid == "mismatched_lease":
            state.attach_checkout_lease(replace(lease, recovery_id="different"))
        elif invalid == "revoked_lease":
            lease.revoke()
    elif invalid == "absent_scope":
        auth = replace(auth, repo_pr_scope_registry=None, scoped_pr_grants=None)
    else:
        state = RepoReviewState(replace(scope, allowed_operations=frozenset({RepoPRAction.CHECKOUT.value})))
        auth = _auth(policy, state, enforce)

    request = ToolCallRequest(
        tool_call={"name": "scoped_commit", "args": {
            "repository": scope.canonical_repo, "pull_request": scope.pr_number,
        }, "id": "commit", "type": "tool_call"},
        tool=policy.tools["scoped_commit"].factory(), state=None,
        runtime=Runtime(context=auth),
    )
    called = []

    def handler(_request):
        called.append(True)
        return ToolMessage(content="ok", tool_call_id="commit")

    gate = BudgetGateMiddleware(policy)
    if asynchronous:
        async def async_handler(tool_request):
            return handler(tool_request)
        result = await gate.awrap_tool_call(request, async_handler)
    else:
        result = gate.wrap_tool_call(request, handler)
    assert result.status == "error" and called == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_oversized_checkout_status_refused_before_registered_handler(
    policy, tmp_path, monkeypatch, asynchronous,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = _auth(policy, state, True)
    token = consume_scoped_pr_checkout(auth, scope, "cultivate")
    assert token is not None
    create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
    assert observe_scoped_pr_checkout(auth, scope, state, token, "cultivate", policy.scoped_actions["cultivate"])

    real_popen = subprocess.Popen
    processes = []

    def oversized_status(argv, **kwargs):
        assert argv[0] == "/usr/bin/git"
        assert kwargs["env"]["HOME"] == "/nonexistent"
        process = real_popen(
            [sys.executable, "-c", "import os\nfor _ in range(16384): os.write(1, b'x' * 65536)"],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr("mimir_cultivate.policy.subprocess.Popen", oversized_status)
    request = ToolCallRequest(
        tool_call={"name": "scoped_commit", "args": {
            "repository": scope.canonical_repo, "pull_request": scope.pr_number,
        }, "id": "commit", "type": "tool_call"},
        tool=policy.tools["scoped_commit"].factory(), state=None, runtime=Runtime(context=auth),
    )
    called = []

    def handler(_request):
        called.append(True)
        return ToolMessage(content="ok", tool_call_id="commit")

    gate = BudgetGateMiddleware(policy)
    if asynchronous:
        async def async_handler(tool_request):
            return handler(tool_request)
        result = await gate.awrap_tool_call(request, async_handler)
    else:
        result = gate.wrap_tool_call(request, handler)
    assert result.status == "error" and called == []
    assert processes and all(process.returncode == -9 for process in processes)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("failure", ["resolver", "profile"])
async def test_heartbeat_scope_and_profile_refused_before_handler(
    policy, tmp_path, monkeypatch, asynchronous, enforce, failure,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    auth = _auth(policy, state, enforce)
    monkeypatch.setattr("mimir.access_control.heartbeat_git_authority_enabled", lambda _service: True)
    if failure == "resolver":
        def resolve(*_args):
            raise ToolException("untrusted PR target")
    else:
        def resolve(*_args):
            return state
        monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "someone-else")
    monkeypatch.setattr("mimir_cultivate.forge.resolve_review_state_for_context", resolve)
    calls = []
    request = ToolCallRequest(
        tool_call={"name": "scoped_checkout", "args": {
            "repository": scope.canonical_repo, "pull_request": scope.pr_number,
        }, "id": "checkout", "type": "tool_call"},
        tool=policy.tools["scoped_checkout"].factory(), state=None,
        runtime=Runtime(context=auth),
    )

    def handler(_request):
        calls.append(True)
        return ToolMessage(content="ok", tool_call_id="checkout")

    gate = BudgetGateMiddleware(policy)
    if asynchronous:
        async def async_handler(tool_request):
            return handler(tool_request)
        result = await gate.awrap_tool_call(request, async_handler)
    else:
        result = gate.wrap_tool_call(request, handler)
    assert result.status == "error" and calls == []


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


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
async def test_reminted_server_scope_issues_only_its_current_checkout_grant(
    policy, tmp_path, monkeypatch, asynchronous, enforce,
):
    _repo, scope = _repo_and_scope(tmp_path)
    old = RepoReviewState(scope)
    auth = _auth(policy, old, enforce)
    registry = ServerDiscoveredPRStates()
    auth = replace(auth, server_discovered_pr_states=registry)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    fresh = RepoReviewState(replace(scope, provenance="server_discovered"))
    assert fresh.action_scope.scope_id != old.action_scope.scope_id
    gate = BudgetGateMiddleware(policy)
    calls = []

    async def invoke(name, *, use_old=False):
        request = ToolCallRequest(
            tool_call={"name": name, "args": {
                "repository": scope.canonical_repo, "pull_request": scope.pr_number,
            }, "id": name, "type": "tool_call"},
            tool=policy.tools[name].factory(), state=None, runtime=Runtime(context=auth),
        )

        def handler(_request):
            calls.append(name)
            if name == "scoped_checkout":
                target = old if use_old else fresh
                create_pr_checkout_lease(target.action_scope, owner=scope.principal,
                                         lease_root=lease_root, review_state=target)
            return ToolMessage(content="ok", tool_call_id=name)

        if asynchronous:
            async def async_handler(tool_request):
                return handler(tool_request)
            return await gate.awrap_tool_call(request, async_handler)
        return gate.wrap_tool_call(request, handler)

    assert (await invoke("scoped_checkout", use_old=True)).status != "error"
    assert (await invoke("scoped_commit")).status != "error"
    assert registry.begin_remint(old.repo, old.pr_number)
    assert registry.remember_remint(old, fresh) is fresh
    # Old lease and old follow-on grant still exist, but target resolution now
    # selects the new scope; no old handler may execute against it.
    assert (await invoke("scoped_commit")).status == "error"
    assert calls == ["scoped_checkout", "scoped_commit"]
    assert (await invoke("scoped_checkout")).status != "error"
    assert (await invoke("scoped_commit")).status != "error"
    assert calls == ["scoped_checkout", "scoped_commit", "scoped_checkout", "scoped_commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("invalid", ["revoked", "expired", "replaced", "head_changed"])
async def test_follow_on_rechecked_after_permission_wait(
    policy, tmp_path, monkeypatch, asynchronous, enforce, invalid,
):
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    auth = _auth(policy, state, enforce)
    token = consume_scoped_pr_checkout(auth, scope, "cultivate")
    lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
    assert observe_scoped_pr_checkout(auth, scope, state, token, "cultivate", policy.scoped_actions["cultivate"])
    calls = []

    def invalidate(*_args, **_kwargs):
        if invalid == "revoked":
            lease.revoke()
        elif invalid == "expired":
            grant = auth.scoped_pr_grants._follow[("cultivate", scope.scope_id)][RepoPRAction.COMMIT.value]
            auth.scoped_pr_grants._follow[("cultivate", scope.scope_id)][RepoPRAction.COMMIT.value] = replace(
                grant, expires_at=0,
            )
        elif invalid == "replaced":
            lease.revoke()
            create_pr_checkout_lease(scope, owner=scope.principal, lease_root=root, review_state=state)
        else:
            subprocess.run(["git", "-C", str(lease.path), "-c", "user.name=Test",
                            "-c", "user.email=test@example.com", "commit", "--allow-empty",
                            "-qm", "advance"], check=True)
        return None

    monkeypatch.setattr("mimir.tools.budget_gate._request_permission_sync", invalidate)

    async def async_invalidate(*args, **kwargs):
        return invalidate(*args, **kwargs)

    monkeypatch.setattr("mimir.tools.budget_gate._request_permission_async", async_invalidate)
    request = ToolCallRequest(
        tool_call={"name": "scoped_commit", "args": {
            "repository": scope.canonical_repo, "pull_request": scope.pr_number,
        }, "id": "commit", "type": "tool_call"},
        tool=policy.tools["scoped_commit"].factory(), state=None, runtime=Runtime(context=auth),
    )

    def handler(_request):
        calls.append(True)
        return ToolMessage(content="ok", tool_call_id="commit")

    gate = BudgetGateMiddleware(policy)
    if asynchronous:
        async def async_handler(tool_request):
            return handler(tool_request)
        result = await gate.awrap_tool_call(request, async_handler)
    else:
        result = gate.wrap_tool_call(request, handler)
    assert result.status == "error" and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
async def test_same_action_vocabulary_cannot_cross_scope_owner(
    tmp_path, asynchronous, enforce,
):
    registry = PolicyRegistry()
    ForeignAction = StrEnum("ForeignAction", {
        "CHECKOUT": RepoPRAction.CHECKOUT.value, "COMMIT": RepoPRAction.COMMIT.value,
    })
    for owner in ("cultivate", "foreign"):
        registrar = registry.registrar(owner)
        action_type = RepoPRAction if owner == "cultivate" else ForeignAction
        registrar.register_scoped_action(
            action_type, lambda *_: True, checkout_action=action_type.CHECKOUT,
            live_lease=lambda _scope, state: state.checkout_lease,
        )
        for suffix, action in (("checkout", action_type.CHECKOUT),
                               ("commit", action_type.COMMIT)):
            name = f"{owner}_{suffix}"
            registrar.register_tool(
                name=name, factory=lambda name=name: StructuredTool.from_function(
                    _tool, name=name, description="Scoped test tool",
                ), flow="source", tier="scope-contained", operation_class="resource_scoped",
                resource_scope="repo_pr", typed_action=action, result_domain="repository",
                result_origin=ResultOriginKind.REPOSITORY, sink_category=None,
                sink_destination=None, sink_target_extractor=None, sink_payload_extractor=None,
                writes_code_or_state=False, profiles=(), read_backend=False,
            )
    selected = registry.freeze()
    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    auth = _auth(selected, state, enforce)
    assert auth.scoped_pr_grants.get("cultivate", scope, RepoPRAction.CHECKOUT.value) is not None
    assert auth.scoped_pr_grants.get("foreign", scope, RepoPRAction.CHECKOUT.value) is None
    calls = []
    gate = BudgetGateMiddleware(selected)
    for name in ("foreign_checkout", "foreign_commit"):
        request = ToolCallRequest(
            tool_call={"name": name, "args": {
                "repository": scope.canonical_repo, "pull_request": scope.pr_number,
            }, "id": name, "type": "tool_call"},
            tool=selected.tools[name].factory(), state=None, runtime=Runtime(context=auth),
        )

        def handler(_request):
            calls.append(name)
            return ToolMessage(content="ok", tool_call_id=name)

        if asynchronous:
            async def async_handler(tool_request):
                return handler(tool_request)
            result = await gate.awrap_tool_call(request, async_handler)
        else:
            result = gate.wrap_tool_call(request, handler)
        assert result.status == "error"
    assert calls == []
