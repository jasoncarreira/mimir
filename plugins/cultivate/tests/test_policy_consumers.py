from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import json
import builtins
import subprocess

import pytest
from langchain_core.tools import ToolException
from langchain_core.messages import ToolMessage
from langchain.agents.middleware import ToolCallRequest
from langgraph.runtime import Runtime

from mimir.plugins.core import core_policy_registry, policy_membership, policy_scope
from mimir.plugins.manager import load_plugins
from mimir.plugins.registry import PolicyRegistrationError
from mimir.tools import budget_gate
from mimir_cultivate import policy as cultivate_policy, review_guard
from tests.auth_helpers import middleware_auth_context


def test_delegated_memberships_are_selected_and_owner_bound() -> None:
    selected = load_plugins(enable="cultivate").policy
    with policy_scope(core_policy_registry()):
        for category, names in cultivate_policy.host_membership_projection().items():
            assert not (set(names) & set(policy_membership(category))) or all(
                core_policy_registry().tools[name].owner == "core" for name in names
            )
    with policy_scope(selected):
        for category, names in cultivate_policy.host_membership_projection().items():
            assert set(policy_membership(category)) == set(names)
            assert all(selected.tools[name].owner == "cultivate" for name in names)


def test_selected_review_uses_provider_guard_not_core_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = load_plugins(enable="cultivate").policy
    observed = []

    def deny(_auth, repository, pull_request):
        observed.append((repository, pull_request))
        raise ToolException("head changed")

    monkeypatch.setattr(selected, "_policy_consumer_hooks", replace(
        selected.policy_consumer_hooks, resolve_review=deny,
    ))
    with policy_scope(selected):
        assert budget_gate._resolve_standing_review(
            "repo_status", SimpleNamespace(retained_factory_scope=None),
            {"repository": "owner/repo", "pull_request": 7}, policy=selected,
        ) == "head changed"
    assert observed == [("owner/repo", 7)]


def test_retained_scope_without_authenticated_service_cannot_skip_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = load_plugins(enable="cultivate").policy
    observed = []

    def deny(*_args):
        observed.append(True)
        raise ToolException("review required")

    monkeypatch.setattr(selected, "_policy_consumer_hooks", replace(
        selected.policy_consumer_hooks, resolve_review=deny,
    ))
    auth = SimpleNamespace(retained_factory_scope=SimpleNamespace(repository="owner/repo", issue_id=7))
    with policy_scope(selected):
        assert budget_gate._resolve_standing_review(
            "repo_status", auth, {"repository": "owner/repo", "pull_request": 7}, policy=selected,
        ) == "review required"
    assert observed == [True]


def test_selected_review_fails_closed_if_owner_hook_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = load_plugins(enable="cultivate").policy
    monkeypatch.setattr(selected, "_policy_consumer_hooks", None)
    with policy_scope(selected):
        assert budget_gate._resolve_standing_review(
            "repo_status", SimpleNamespace(retained_factory_scope=None),
            {"repository": "owner/repo", "pull_request": 7}, policy=selected,
        ) == "repository review provider unavailable"


def test_consumer_hook_is_frozen_and_cannot_be_self_declared_by_core() -> None:
    selected = load_plugins(enable="cultivate").policy
    hooks = selected.policy_consumer_hooks
    assert hooks is not None and hooks.owner == "cultivate"
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        selected.register_policy_consumer_hooks(hooks, owner="cultivate")
    with pytest.raises(PolicyRegistrationError):
        core_policy_registry().register_policy_consumer_hooks(hooks, owner="core")


def test_provider_review_claim_tracks_current_head_and_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    head = ["a" * 40]
    reviews = []

    def github(_spec, args):
        if args[:2] == ["api", "user"]:
            body = "reviewer"
        elif args[-1] == ".head.sha":
            body = head[0]
        elif args[-1] == "--paginate":
            body = json.dumps(reviews)
        else:
            raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout=body)

    monkeypatch.setattr(review_guard, "_run", github)
    spec = review_guard.ReviewSubmission("gh", "owner/repo", 7, "APPROVED", None)
    first = review_guard.claim_review_submission(spec)
    assert first is not None and not first.duplicate and first.head == head[0]
    try:
        reviews.append({"user": {"login": "reviewer"}, "commit_id": head[0], "state": "APPROVED"})
    finally:
        first.release()
    duplicate = review_guard.claim_review_submission(spec)
    assert duplicate is not None and duplicate.duplicate
    duplicate.release()
    head[0] = "b" * 40
    fresh = review_guard.claim_review_submission(spec)
    assert fresh is not None and not fresh.duplicate and fresh.head == head[0]
    fresh.release()


def test_selected_review_probe_env_uses_provider_identity_without_core_forge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools._shell_env import bind_direct_exec_argv, reset_direct_exec_argv
    from mimir.tools.refusals import ToolPolicyRefusal
    from mimir_cultivate import forge

    monkeypatch.setenv("GITHUB_TOKEN", "bound-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    seen = []
    monkeypatch.setattr(forge, "confirm_github_tool_identity", lambda principal, token: seen.append((principal, token)))
    imported = builtins.__import__

    def without_core_forge(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "forge" and level == 1 and (globals or {}).get("__package__") == "mimir.tools":
            raise AssertionError("selected review probe imported retired core forge")
        return imported(name, globals, locals, fromlist, level)

    with monkeypatch.context() as no_core:
        no_core.setattr(builtins, "__import__", without_core_forge)
        env = review_guard._gh_env()
        assert env["GITHUB_TOKEN"] == "bound-token"
        assert env["GH_CONFIG_DIR"] == review_guard._GH_CONFIG_DIR
        assert seen == [("reviewer", "bound-token")]
        token = bind_direct_exec_argv(["gh"], operator_declared=True)
        try:
            with pytest.raises(ToolPolicyRefusal, match="operator-declared gh is refused"):
                review_guard._gh_env()
        finally:
            reset_direct_exec_argv(token)


def test_selected_checkout_attestation_uses_provider_git_and_rejects_changed_head(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control as ac, repo_tools
    from mimir.models import AuthContext, InformationFlowLabels, InformationFlowState, RepoReviewState
    from mimir_cultivate import repository
    from tests.test_pr_checkout_lease import _repo_and_scope

    _repo, scope = _repo_and_scope(tmp_path)
    state = RepoReviewState(scope)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    selected = load_plugins(enable="cultivate").policy
    lease = repository.create_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root, review_state=state,
    )
    labels = InformationFlowLabels()
    auth = AuthContext(
        principal=scope.principal, canonical_principal=scope.principal,
        roles=("user",), event_ingress=None, trigger="user_message",
        channel_id="web:own", interactivity=None,
        repo_review_state=state, repo_pr_action_scope=scope,
        ifc_labels=labels, ifc_state=InformationFlowState(labels),
    )
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = True
    def core_git_forbidden(*_args, **_kwargs):
        raise AssertionError("selected checkout consulted retired core Git")

    monkeypatch.setattr(repo_tools, "hardened_git_command", core_git_forbidden)
    with policy_scope(selected):
        assert ac.pr_checkout_lease_attestation(auth, scope, lease).ok
        changed = replace(scope, observed_head_sha="0" * 40)
        assert not ac.pr_checkout_lease_attestation(auth, changed, lease).ok


@pytest.mark.parametrize("enforced", [False, True])
def test_selected_issue_comment_requires_repository_source_and_owner_resolution(
    monkeypatch: pytest.MonkeyPatch, enforced: bool,
) -> None:
    from mimir.access_control import (
        CapabilityTier, ToolRegistry, build_trigger_service_principal,
    )
    from mimir.models import AuthContext, InformationFlowLabels, InformationFlowState, SourceLabel
    from mimir.tools import forge as core_forge
    from mimir_cultivate.forge import IssueTarget

    monkeypatch.setenv("GITHUB_REPOS", "owner/repo,other/repo")
    selected = load_plugins(enable="cultivate").policy
    resolved = []

    def owner_resolve(repository, issue):
        resolved.append((repository, issue))
        return IssueTarget(repository, issue)

    def core_forbidden(*_args):
        raise AssertionError("selected issue resolution imported retired core forge")

    monkeypatch.setattr(core_forge, "resolve_issue_comment_target", core_forbidden)
    monkeypatch.setattr(selected, "_policy_consumer_hooks", replace(
        selected.policy_consumer_hooks, resolve_issue=owner_resolve,
    ))
    principal = "poller:github-activity"
    source = SourceLabel(
        principal=f"service:{principal}", domain="repository",
        resource_id=f"owner/repo#pull/7@{'a' * 40}", bridge_instance="forge",
        sensitivity="internal", authorized_principals=frozenset({f"service:{principal}"}),
        source_kind="protected_tool", integrity="trusted", integrity_effect="informational",
    )
    labels = InformationFlowLabels().with_channel("poller:github").with_source(source)
    with policy_scope(selected):
        service = build_trigger_service_principal(
            canonical=principal, trigger="poller", profile="github",
            tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
            capabilities=("issue_comment",), creation_path="test",
        )

        def auth(current):
            return AuthContext(
                principal=principal, canonical_principal=principal, roles=("service",),
                event_ingress=None, trigger="poller", channel_id="poller:github",
                interactivity=None, is_service=True, service_authority=service,
                enforcement_enabled=enforced, ifc_labels=current,
                ifc_state=InformationFlowState(current),
            )

        arguments = {"repository": "owner/repo", "issue": 220, "body": "analysis"}
        decision = ToolRegistry().authorize_tool(
            "issue_comment", auth(labels), enforce=enforced, arguments=arguments,
        )
        assert decision.allowed and not decision.would_block, (decision.reason, decision.refusal_detail)
        assert decision.resolved_sink_target == "owner/repo#issue/220"
        assert resolved == [("owner/repo", 220)]
        missing = ToolRegistry().authorize_tool(
            "issue_comment", auth(InformationFlowLabels()), enforce=enforced, arguments=arguments,
        )
        assert missing.reason == "issue_repository_source_required" and missing.would_block
        assert resolved == [("owner/repo", 220)]
        request = ToolCallRequest(
            tool_call={"name": "issue_comment", "args": arguments,
                       "id": "selected-issue", "type": "tool_call"},
            tool=selected.tools["issue_comment"].factory(), state=None,
            runtime=Runtime(context=auth(labels)),
        )
        invoked = []

        def handler(_request):
            invoked.append(True)
            return ToolMessage(content="commented", tool_call_id="selected-issue")

        output = budget_gate.BudgetGateMiddleware(selected).wrap_tool_call(request, handler)
        assert output.status != "error", output.content
        assert invoked == [True]
        other = ToolRegistry().authorize_tool(
            "issue_comment", auth(labels), enforce=enforced,
            arguments={**arguments, "repository": "other/repo"},
        )
        assert other.reason == "ifc_label_blocked:forge" and other.would_block
        monkeypatch.setattr(selected, "_policy_consumer_hooks", None)
        unavailable = ToolRegistry().authorize_tool(
            "issue_comment", auth(labels), enforce=enforced, arguments=arguments,
        )
        assert unavailable.reason == "issue_destination_resolution_failed"
        assert not unavailable.allowed and unavailable.would_block


def test_selected_repository_inventory_and_fallback_roots_do_not_import_core_repository(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control as ac

    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                    "https://github.com/owner/repo.git"], check=True)
    inventory = home / "repositories.yaml"
    inventory.write_text(
        "repositories:\n"
        "  - slug: owner/repo\n"
        f"    root: {root}\n"
        "    mode: rw\n"
        "    origin: https://github.com/owner/repo.git\n"
        "    base_branch: main\n"
        "    test_command: /usr/bin/true\n", encoding="utf-8",
    )
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{tmp_path / 'other'}:rw")
    selected = load_plugins(enable="cultivate").policy
    imported = builtins.__import__

    def no_core_repository(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "repository_config" and level == 1:
            raise AssertionError("selected policy imported retired core repository inventory")
        return imported(name, globals, locals, fromlist, level)

    with policy_scope(selected), monkeypatch.context() as check:
        check.setattr(builtins, "__import__", no_core_repository)
        assert ac._configured_repo_roots() == [root]
        assert ac._configured_repo_write_roots() == [root]
        assert ac._configured_scope_github_repos() == {"owner/repo"}
        assert ac._canonical_repo_binding_resolution("OWNER/REPO").binding == (
            str(root), "https://github.com/owner/repo.git",
        )
        inventory.unlink()
        (tmp_path / "other").mkdir()
        assert ac._configured_repo_roots() == [tmp_path / "other"]
        assert ac._configured_repo_write_roots() == [tmp_path / "other"]
        assert ac._canonical_repo_binding_resolution("owner/repo").binding is None


def test_selected_worklink_inventory_importers_use_provider_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir_cultivate import repository

    imported = builtins.__import__

    def without_core_repository(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mimir.repository_config" or (name == "repository_config" and level == 1):
            raise AssertionError("provider requested retired core repository inventory")
        return imported(name, globals, locals, fromlist, level)

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "__import__", without_core_repository)
        selected = load_plugins(enable="cultivate").policy
        from mimir_cultivate.worklink.backends import registry
        from mimir_cultivate.worklink import merge_closure, orchestrator

    assert selected.policy_consumer_hooks.repository_inventory == repository.RepositoryInventory.load
    assert selected.policy_consumer_hooks.tracked_file is repository.is_tracked_file_in_pr_checkout
    assert selected.policy_consumer_hooks.verified_push is repository.was_verified_push
    assert selected.policy_consumer_hooks.agent_push is repository.was_agent_push
    assert registry.valid_repository_slug("owner/repo") == repository.valid_repository_slug("owner/repo")
    assert registry.valid_repository_slug("../repo") == repository.valid_repository_slug("../repo")
    assert merge_closure.RepositoryConfig is repository.RepositoryConfig
    assert merge_closure.RepositoryInventory is orchestrator.RepositoryInventory is repository.RepositoryInventory


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stale", [False, True])
async def test_selected_pr_review_revalidates_head_before_middleware_handler(
    tmp_path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool, stale: bool,
) -> None:
    from mimir.access_control import (
        ScopedPRGrants, _SCOPED_PR_ISSUER, consume_scoped_pr_checkout,
        observe_scoped_pr_checkout,
    )
    from mimir.models import (
        AuthContext, InformationFlowLabels, InformationFlowState,
        RepoPRScopeRegistry, RepoReviewState, TurnInteractivity,
    )
    from mimir_cultivate import repository
    from tests.test_pr_checkout_lease import _repo_and_scope

    _repo, original_scope = _repo_and_scope(tmp_path)
    scope = replace(original_scope, owner="cultivate")
    state = RepoReviewState(scope)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    selected = load_plugins(enable="cultivate").policy
    grants = ScopedPRGrants(
        scope.principal, (scope,), protocols=selected.scoped_actions, _issuer=_SCOPED_PR_ISSUER,
    )
    labels = InformationFlowLabels()
    auth = AuthContext(
        principal=scope.principal, canonical_principal=scope.principal,
        roles=("user",), event_ingress=None, trigger="user_message",
        channel_id="web:own", interactivity=TurnInteractivity.INTERACTIVE,
        is_service=False, enforcement_enabled=True,
        repo_pr_scope_registry=RepoPRScopeRegistry((state,)), repo_review_state=state,
        repo_pr_action_scope=scope, scoped_pr_grants=grants,
        ifc_labels=labels, ifc_state=InformationFlowState(labels),
    )
    token = consume_scoped_pr_checkout(auth, scope, "cultivate")
    assert token is not None
    repository.create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root, review_state=state)
    assert observe_scoped_pr_checkout(
        auth, scope, state, token, "cultivate", selected.scoped_actions["cultivate"],
    )
    observed = []

    def reject(_auth, repository_name, number):
        observed.append((repository_name, number))
        if stale:
            raise ToolException("head changed")

    monkeypatch.setattr(selected, "_policy_consumer_hooks", replace(
        selected.policy_consumer_hooks, resolve_review=lambda *_: state, revalidate_head=reject,
    ))
    request = ToolCallRequest(
        tool_call={
            "name": "pr_submit_review",
            "args": {"repository": scope.canonical_repo, "pull_request": scope.pr_number,
                     "verdict": "approve", "body": "review"},
            "id": "selected-pr-review", "type": "tool_call",
        }, tool=selected.tools["pr_submit_review"].factory(), state=None,
        runtime=Runtime(context=auth),
    )
    executed = []

    def handler(_request):
        executed.append(True)
        return ToolMessage(content="reviewed", tool_call_id="selected-pr-review")

    async def async_handler(tool_request):
        return handler(tool_request)

    gate = budget_gate.BudgetGateMiddleware(selected)
    imported = builtins.__import__

    def no_legacy(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mimir.tools.github_review_guard" or (level and name == "github_review_guard"):
            raise ModuleNotFoundError(name=name)
        return imported(name, globals, locals, fromlist, level)

    with monkeypatch.context() as absent:
        absent.setattr(builtins, "__import__", no_legacy)
        result = (
            await gate.awrap_tool_call(request, async_handler)
            if asynchronous else gate.wrap_tool_call(request, handler)
        )
    assert observed == [(scope.canonical_repo, scope.pr_number)]
    if stale:
        assert result.status == "error"
        assert "head changed" in str(result.content)
        assert not executed
    else:
        assert result.status != "error", result.content
        assert executed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("guard_available", [False, True])
async def test_selected_review_submission_uses_owner_guard_at_middleware_boundary(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, guard_available: bool,
) -> None:
    from mimir.tools import budget_gate

    selected = load_plugins(enable="cultivate").policy
    observed: list[review_guard.ReviewSubmission] = []
    if guard_available:
        monkeypatch.setattr(selected, "_policy_consumer_hooks", replace(
            selected.policy_consumer_hooks,
            claim_review=lambda spec: observed.append(spec) or None,
        ))
    else:
        monkeypatch.setattr(selected, "_policy_consumer_hooks", None)
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(budget_gate, "_request_for_authorized_execution", lambda request, *_: request)
    request = ToolCallRequest(
        tool_call={
            "name": "shell_exec",
            "args": {"command": "gh pr review 152 --repo owner/repo --approve"},
            "id": "selected-review", "type": "tool_call",
        },
        tool=None, state=None, runtime=Runtime(context=middleware_auth_context()),
    )
    executed = []

    def handler(_request):
        executed.append(True)
        return ToolMessage(content="submitted", tool_call_id="selected-review")

    async def async_handler(tool_request):
        return handler(tool_request)

    gate = budget_gate.BudgetGateMiddleware(selected)
    result = (
        await gate.awrap_tool_call(request, async_handler)
        if asynchronous else gate.wrap_tool_call(request, handler)
    )
    if guard_available:
        assert [replace(spec, cwd=None) for spec in observed] == [review_guard.ReviewSubmission(
            "gh", "owner/repo", 152, "APPROVED", None,
        )]
        assert executed == [True]
    else:
        assert result.status == "error"
        assert "selected review submission guard unavailable" in str(result.content)
        assert executed == []


def test_selected_typed_review_never_uses_legacy_guard_when_owner_hook_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    selected = load_plugins(enable="cultivate").policy
    monkeypatch.setattr(selected, "_policy_consumer_hooks", None)
    call = SimpleNamespace(
        policy=selected, tool_name="pr_submit_review",
        execution_request=SimpleNamespace(tool_call={
            "name": "pr_submit_review", "args": {
                "repository": "owner/repo", "pull_request": 152,
                "verdict": "approve", "body": "Looks good",
            },
        }),
    )

    with pytest.raises(ToolException, match="selected review submission guard unavailable"):
        budget_gate._review_submission_for_call(call)


def test_selected_typed_review_dispatches_through_owner_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = load_plugins(enable="cultivate").policy
    observed = []
    monkeypatch.setattr(selected, "_policy_consumer_hooks", replace(
        selected.policy_consumer_hooks,
        review_submission=lambda request: observed.append(request) or None,
    ))
    request = SimpleNamespace(tool_call={
        "name": "pr_submit_review", "args": {
            "repository": "owner/repo", "pull_request": 152,
            "verdict": "approve", "body": "Looks good",
        },
    })
    call = SimpleNamespace(
        policy=selected, tool_name="pr_submit_review", execution_request=request,
    )

    assert budget_gate._review_submission_for_call(call) is None
    assert observed == [request]
