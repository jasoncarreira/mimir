from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langchain.agents.middleware import ToolCallRequest
from langchain.tools import ToolRuntime
from langgraph.runtime import Runtime

from mimir.access_control import (
    AccessTier, OperationDecision, ToolAuthorization, ToolFlowDirection,
    _RetainedResultReceipt, classify_protected_result,
    _retained_checkout_binding,
)
from mimir.models import RetainedFactoryScope
from mimir.tools import budget_gate


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("name", [
    "repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit",
])
async def test_selected_ordinary_repo_tool_holds_real_issued_pr_lease(
    tmp_path, monkeypatch, name, enforce, asynchronous,
):
    from mimir.access_control import (
        CapabilityTier, ScopedPRGrants, _SCOPED_PR_ISSUER,
        build_trigger_service_principal,
    )
    from mimir.models import (
        AuthContext, InformationFlowLabels, InformationFlowState,
        RepoPRScopeRegistry, RepoReviewState,
    )
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins
    from mimir_cultivate import repository
    from mimir_cultivate.policy import RepoPRAction
    from tests.test_pr_checkout_lease import _repo_and_scope

    _repo, original_scope = _repo_and_scope(tmp_path)
    scope = replace(original_scope, principal="poller:github")
    state = RepoReviewState(scope)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        service = build_trigger_service_principal(
            canonical=scope.principal, trigger="poller", profile="github",
            tier=CapabilityTier.CODE_EXECUTION,
            capabilities=("repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit"),
            creation_path="test_selected_ordinary_repo_tool_holds_real_issued_pr_lease",
        )
        grants = ScopedPRGrants(
            scope.principal, (scope,), protocols=selected.scoped_actions,
            _issuer=_SCOPED_PR_ISSUER,
        )
        labels = InformationFlowLabels()
        auth = AuthContext(
            principal=scope.principal, canonical_principal=scope.principal,
            roles=("service",), event_ingress=None, trigger="poller",
            channel_id="poller:github", interactivity=None,
            is_service=True, service_authority=service, enforcement_enabled=enforce,
            repo_pr_scope_registry=RepoPRScopeRegistry((state,)),
            repo_review_state=state, repo_pr_action_scope=scope, scoped_pr_grants=grants,
            ifc_labels=labels, ifc_state=InformationFlowState(labels),
        )
        token = grants.consume_checkout("cultivate", scope)
        assert token is not None
        lease = repository.create_pr_checkout_lease(
            scope, owner=scope.principal, lease_root=lease_root, review_state=state,
        )
        assert grants.observe_checkout("cultivate", scope, lease, token)
        if name in {"repo_stage", "repo_commit"}:
            (lease.path / "file.txt").write_text("changed\n", encoding="utf-8")

        async def passing_test(self, selectors=(), *, suite=None):
            assert self._state is state
            return repository.ProjectTestResult(True, "tests_passed", 0)

        monkeypatch.setattr(repository.RepoProjectTests, "execute", passing_test)
        tool = selected.tools[name].factory()
        arguments = {"repository": scope.canonical_repo, "pull_request": scope.pr_number}
        if name in {"repo_stage", "repo_commit"}:
            arguments["paths"] = ("file.txt",)
        if name == "repo_commit":
            arguments["message"] = "test commit"
        request = ToolCallRequest(
            tool_call={"name": name, "args": arguments, "id": "ordinary-call", "type": "tool_call"},
            tool=tool, state=None, runtime=Runtime(context=auth),
        )
        runtime = ToolRuntime(
            state={}, context=auth, config={}, stream_writer=lambda _: None,
            tool_call_id="ordinary-call", store=None,
        )
        calls = []

        async def execute():
            calls.append(lease.recovery_id)
            if name == "repo_test":
                return await tool.coroutine(**arguments, runtime=runtime)
            return tool.func(**arguments, runtime=runtime)

        async def async_handler(_request):
            value = await execute()
            return ToolMessage(content=json.dumps(value), tool_call_id="ordinary-call")

        def sync_handler(_request):
            value = asyncio.run(execute())
            return ToolMessage(content=json.dumps(value), tool_call_id="ordinary-call")

        gate = budget_gate.BudgetGateMiddleware(selected)
        if asynchronous:
            output = await gate.awrap_tool_call(request, async_handler)
        else:
            output = await asyncio.to_thread(gate.wrap_tool_call, request, sync_handler)
        assert output.status != "error", output.content
        assert calls == [lease.recovery_id]
        assert state.checkout_lease is lease


def _case(monkeypatch, tmp_path):
    from mimir.worklink import worker_client, factory_state, retained_scope
    from mimir.worklink.dispatch_failures import dispatch_failure_state_dir, record_failure
    from mimir.worklink.compute import LaunchHandle

    home = tmp_path / "home"
    root = tmp_path / "worklink"
    sandbox = root / "repo" / "1949-1" / "checkout" / ".factory-sandboxes" / "chainlink-1949"
    sandbox.mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", root)
    subprocess.run(["git", "-C", str(sandbox), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(sandbox), "checkout", "-q", "-b", "feature"], check=True)
    (sandbox / "file.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(sandbox), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(sandbox), "-c", "user.name=Tester",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "seed"], check=True)
    incident = record_failure(
        dispatch_failure_state_dir(home), issue_id=1949, attempt=1, exit_status=1,
        error="factory failed", log_path="run.log", run_id="chainlink-1949", work_path=str(sandbox),
    )
    factory_state.save_factory_record(home, factory_state.FactoryRunRecord(
        run_id="chainlink-1949", issue_id=1949, attempt=1, repository="owner/repo",
        base_ref="main", branch="feature", launcher="/opt/factory.js",
        sandbox=str(sandbox), session="session",
        handle=LaunchHandle("local_subprocess", "99999999", 1), status=None,
        observed_at=None, controller_phase="failed",
    ))
    monkeypatch.setattr(retained_scope, "_factory_session_lock_is_fresh", lambda _record: False)

    def owner_control(checkout, argv, *, env, timeout, output_limit):
        assert checkout == sandbox
        return subprocess.run(argv, capture_output=True, check=False,
                              env={**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    monkeypatch.setattr(worker_client, "run_factory_control", owner_control)
    monkeypatch.setattr(
        "mimir.access_control._is_worklink_retained_checkout_service", lambda *_args, **_kw: True,
    )
    scope = RetainedFactoryScope(
        issue_id=1949, signature=incident["signature"], occurrence_id=incident["occurrence_id"],
        run_id="chainlink-1949", attempt=1, session="session", repository="owner/repo",
        branch="feature", sandbox=str(sandbox),
    )
    context = SimpleNamespace(
        retained_factory_scope=scope, canonical_principal="poller:github",
        is_service=True, channel_id="poller:github",
    )
    context.repo_pr_scope_registry = None
    policy = SimpleNamespace(frozen=True, descriptors={}, scoped_actions={
        "cultivate": SimpleNamespace(live_lease=lambda *_args: None,
                                      checkout_action=object()),
    }, tools={"repo_diff": SimpleNamespace(
        owner="cultivate", result_domain="repository",
        memberships=frozenset({"retained_repo_tools"}),
    )})
    authorization = ToolAuthorization(
        tool_name="repo_diff", decision=OperationDecision.RESOURCE_SCOPED,
        allowed=True, required_tier=AccessTier.USER, enforcement_enabled=True,
        is_shadow_decision=False, flow_direction=ToolFlowDirection.SOURCE,
        repo_pr_action_scope=scope, policy=policy,
    )
    result = ToolMessage(content="actual diff", tool_call_id="invocation")
    arguments = {"repository": scope.repository, "pull_request": scope.issue_id}
    return scope, context, policy, authorization, result, arguments


def _receipt(tool, context, scope, result, failed, policy):
    head, identity = _retained_checkout_binding(scope)
    return _RetainedResultReceipt(tool, context, scope, result, failed, policy,
                                  execution_id="host-execution", head=head,
                                  lease_identity=identity)


@pytest.mark.parametrize("enforced", [False, True])
def test_only_host_observed_retained_output_is_complete(monkeypatch, tmp_path, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    authorization.enforcement_enabled = enforced
    incomplete = classify_protected_result(
        "repo_diff", arguments, context, authorization, result=result,
    )
    assert next(iter(incomplete.sources)).integrity == "untrusted"
    receipt = _receipt("repo_diff", context, scope, result, False, policy)
    labels = classify_protected_result(
        "repo_diff", arguments, context, authorization,
        result=result, retained_receipt=receipt,
    )
    source = next(iter(labels.sources))
    assert source.integrity == "trusted"
    assert source.integrity_effect == "active_ingest"
    assert source.domain == "worklink_retained_repository"
    assert source.resource_id.endswith(f"@retained/{scope.scope_id}")
    assert next(iter(classify_protected_result(
        "repo_diff", arguments, context, authorization,
        result=result, retained_receipt=receipt,
    ).sources)).integrity == "untrusted"


def test_receipt_cannot_attest_other_bytes_outcome_scope_or_policy(monkeypatch, tmp_path):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    for changed in (
        {"result": ToolMessage(content="forged", tool_call_id="invocation")},
        {"failed": True},
        {"authorization": replace(authorization, repo_pr_action_scope=replace(scope, branch="other"))},
        {"authorization": replace(authorization, would_block=True)},
        {"authorization": replace(authorization, allowed=False)},
        {"authorization": replace(authorization, decision=OperationDecision.OPEN)},
        {"authorization": replace(authorization, policy=SimpleNamespace(
            tools=policy.tools, descriptors={}, frozen=True,
        ))},
    ):
        receipt = _receipt("repo_diff", context, scope, result, False, policy)
        params = dict(result=result, failed=False, authorization=authorization)
        params.update(changed)
        labels = classify_protected_result(
            "repo_diff", arguments, context, params["authorization"],
            result=params["result"], failed=params["failed"], retained_receipt=receipt,
        )
        assert next(iter(labels.sources)).integrity == "untrusted"


@pytest.mark.parametrize("enforced", [False, True])
def test_non_retained_plugin_results_remain_incomplete_with_receipt(monkeypatch, tmp_path, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    authorization.enforcement_enabled = enforced
    policy.tools["other_read"] = SimpleNamespace(
        owner="cultivate", result_domain="repository", memberships=frozenset(),
    )
    receipt = _receipt("other_read", context, scope, result, False, policy)
    labels = classify_protected_result(
        "other_read", arguments, context, replace(authorization, tool_name="other_read"),
        result=result, retained_receipt=receipt,
    )
    assert next(iter(labels.sources)).integrity == "untrusted"


def test_receipt_rejects_mutated_output_even_on_same_result_object(monkeypatch, tmp_path):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    receipt = _receipt("repo_diff", context, scope, result, False, policy)
    result.content = "different output"
    labels = classify_protected_result(
        "repo_diff", arguments, context, authorization,
        result=result, retained_receipt=receipt,
    )
    assert next(iter(labels.sources)).integrity == "untrusted"


def test_receipt_rejects_head_change_and_colliding_pr_lease(monkeypatch, tmp_path):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    receipt = _receipt("repo_diff", context, scope, result, False, policy)
    context.repo_pr_scope_registry = SimpleNamespace(resolve=lambda *_args: SimpleNamespace(
        action_scope=SimpleNamespace(pr_number=scope.issue_id, observed_head_sha=receipt.head,
                                     scope_id="unrelated-pr"),
    ))
    assert next(iter(classify_protected_result(
        "repo_diff", arguments, context, authorization, result=result,
        retained_receipt=receipt,
    ).sources)).integrity == "trusted"

    receipt = _receipt("repo_diff", context, scope, result, False, policy)
    (tmp_path / "worklink" / "repo" / "1949-1" / "checkout" /
     ".factory-sandboxes" / "chainlink-1949" / "file.txt").write_text("changed\n")
    subprocess.run(["git", "-C", scope.sandbox, "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", scope.sandbox, "-c", "user.name=Tester",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "new head"], check=True)
    assert next(iter(classify_protected_result(
        "repo_diff", arguments, context, authorization, result=result,
        retained_receipt=receipt,
    ).sources)).integrity == "untrusted"


def test_retained_dispatch_requires_live_lease_and_captures_actual_output(tmp_path, monkeypatch):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    policy.tools["repo_diff"].resource_scope = "repo_pr"
    monkeypatch.setattr(budget_gate, "get_trusted_service_from_auth_context",
                        lambda _ctx: SimpleNamespace(canonical="poller:worklink-ready-queue"))
    monkeypatch.setattr(
        "mimir.access_control._is_worklink_retained_checkout_service", lambda *_args, **_kw: True,
    )
    call = SimpleNamespace(
        tool_name="repo_diff", policy=policy, auth_context=context,
        authorization=authorization, arguments=arguments,
        request=SimpleNamespace(tool_call={"id": "invocation"}),
    )
    events = []

    from mimir.worklink import retained_scope
    real_lease = retained_scope.retained_factory_effect_lease

    @contextmanager
    def lease(home, received):
        with real_lease(home, received) as resolution:
            events.append(("lease", received))
            yield resolution
            events.append(("release", received))

    monkeypatch.setattr(retained_scope, "retained_factory_effect_lease", lease)
    capture = budget_gate._ToolExecutionCapture()
    with budget_gate._retained_execution(call) as live:
        assert live is scope
        assert events == [("lease", scope), ("release", scope)]
    budget_gate._capture_retained_result(call, capture, live, result)
    assert events[-1] == ("release", scope)
    assert len(events) >= 6
    assert capture.retained_receipt.execution_id != result.tool_call_id
    assert capture.retained_receipt.consume("repo_diff", context, scope, result, False, policy)
    assert not capture.retained_receipt.consume("repo_diff", context, scope, result, False, policy)

    capture.retained_receipt = None
    budget_gate._capture_retained_result(
        call, capture, scope, ToolMessage(content="claimed", tool_call_id="other"),
    )
    assert capture.retained_receipt is None

    @contextmanager
    def stale(_home, _scope):
        yield SimpleNamespace(scope=None, refusal_reason="stale occurrence")

    monkeypatch.setattr("mimir.worklink.retained_scope.retained_factory_effect_lease", stale)
    with pytest.raises(ToolException, match="stale occurrence"):
        with budget_gate._retained_execution(call):
            pytest.fail("handler ran without a live lease")


@pytest.mark.parametrize("enforced", [False, True])
def test_retained_dispatch_refuses_missing_or_stale_authority_in_both_modes(monkeypatch, tmp_path, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    policy.tools["repo_diff"].resource_scope = "repo_pr"
    authorization.enforcement_enabled = enforced
    call = SimpleNamespace(
        tool_name="repo_diff", policy=policy, auth_context=context,
        authorization=authorization, arguments=arguments,
        request=SimpleNamespace(tool_call={"id": "invocation"}),
    )
    monkeypatch.setattr(budget_gate, "get_trusted_service_from_auth_context",
                        lambda _ctx: SimpleNamespace(canonical="poller:worklink-ready-queue"))
    monkeypatch.setattr(budget_gate, "_tool_call_refusal", lambda *_args, **_kwargs: result)
    authorization.repo_pr_action_scope = None
    assert budget_gate._scoped_pr_dispatch_refusal(call) is result
    authorization.repo_pr_action_scope = scope
    authorization.would_block = True
    assert budget_gate._scoped_pr_dispatch_refusal(call) is result


@pytest.mark.parametrize("enforced", [False, True])
def test_retained_receipt_rejects_failed_and_mismatched_invocation(monkeypatch, tmp_path, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    authorization.enforcement_enabled = enforced
    for changed in (
        {"result": ToolMessage(content="actual diff", tool_call_id="other")},
        {"result": ToolMessage(content="actual diff", tool_call_id="invocation", status="error")},
        {"failed": True},
    ):
        receipt = _receipt("repo_diff", context, scope, result, False, policy)
        labels = classify_protected_result(
            "repo_diff", arguments, context, authorization, retained_receipt=receipt,
            result=changed.get("result", result), failed=changed.get("failed", False),
        )
        assert next(iter(labels.sources)).integrity == "untrusted"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("tool", ["repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit"])
@pytest.mark.parametrize("enforced", [False, True])
async def test_selected_retained_middleware_attests_only_live_outputs(
    tmp_path, monkeypatch, tool, enforced, async_call,
):
    scope, context, policy, authorization, _, arguments = _case(monkeypatch, tmp_path)
    policy.tools[tool] = policy.tools.pop("repo_diff")
    policy.tools[tool].resource_scope = "repo_pr"
    policy.tools[tool].typed_action = None
    authorization.tool_name = tool
    authorization.enforcement_enabled = enforced
    request = SimpleNamespace(tool_call={"id": "model-id", "args": arguments})
    call = budget_gate._PreparedToolCall(
        request=request, execution_request=request, tool_name=tool,
        auth_context=context, arguments=arguments, authorization=authorization,
        operator_shell_preparation=None, operator_shell_audit=None,
        host_execution=None, started=0, policy=policy,
    )
    monkeypatch.setattr(budget_gate, "get_trusted_service_from_auth_context",
                        lambda _ctx: SimpleNamespace(canonical="poller:worklink-ready-queue"))
    monkeypatch.setattr(budget_gate, "_prepare_tool_call_review", lambda _request: SimpleNamespace(
        tool_name=tool, auth_context=context, arguments=arguments,
    ))
    monkeypatch.setattr(budget_gate, "_resolve_standing_review", lambda *_args, **_kw: None)
    monkeypatch.setattr(budget_gate, "_prepare_tool_call_execution", lambda *_args, **_kw: call)
    monkeypatch.setattr(budget_gate, "_review_submission_for_call", lambda _call: None)
    monkeypatch.setattr(budget_gate, "_begin_tool_execution", lambda *_args: None)
    monkeypatch.setattr(budget_gate, "_end_tool_execution", lambda _capture: None)
    monkeypatch.setattr(budget_gate, "_request_permission_sync", lambda *_args: None)
    async def no_permission(*_args):
        return None
    monkeypatch.setattr(budget_gate, "_request_permission_async", no_permission)
    monkeypatch.setattr(budget_gate, "_soft_live_execution_refusal", lambda _call: None)
    monkeypatch.setattr(budget_gate, "_result_is_error", lambda _tool, result, **_kw: result.status == "error")
    labels = []

    def finish(_call, capture, result):
        labels.append(classify_protected_result(
            tool, arguments, context, authorization, result=result,
            failed=result.status == "error", retained_receipt=capture.retained_receipt,
        ))
        return result

    monkeypatch.setattr(budget_gate, "_finish_tool_result", finish)

    observed = []

    def handler(_request):
        observed.append(True)
        return ToolMessage(content="observed output", tool_call_id="model-id")

    async def invoke(produce):
        middleware = budget_gate.BudgetGateMiddleware(policy)
        if async_call:
            async def async_produce(request):
                return produce(request)
            return await middleware.awrap_tool_call(request, async_produce)
        return middleware.wrap_tool_call(request, produce)

    output = await invoke(handler)
    assert output.content == "observed output"
    assert observed == [True]
    assert next(iter(labels[-1].sources)).integrity == "trusted"

    changed = ToolMessage(content="failed output", tool_call_id="model-id", status="error")
    await invoke(lambda _request: changed)
    assert next(iter(labels[-1].sources)).integrity == "untrusted"

    mismatched = ToolMessage(content="forged output", tool_call_id="other-call")
    await invoke(lambda _request: mismatched)
    assert next(iter(labels[-1].sources)).integrity == "untrusted"

    context.repo_pr_scope_registry = SimpleNamespace(resolve=lambda *_args: SimpleNamespace(
        action_scope=SimpleNamespace(observed_head_sha="f" * 40, pr_number=scope.issue_id),
    ))
    await invoke(handler)
    assert next(iter(labels[-1].sources)).integrity == "trusted"

    context.retained_factory_scope = None
    monkeypatch.setattr(budget_gate, "_tool_call_refusal", lambda *_args, **_kw: ToolMessage(
        content="retained factory scope refused", tool_call_id="model-id", status="error",
    ))
    before = len(observed)
    refused = await invoke(handler)
    assert refused.status == "error"
    assert len(observed) == before


@pytest.mark.parametrize("tool,action", [
    ("repo_status", "INSPECT"), ("repo_diff", "INSPECT"),
    ("repo_test", "TEST"), ("repo_stage", "WRITE"), ("repo_commit", "COMMIT"),
])
@pytest.mark.parametrize("enforced", [False, True])
def test_ordinary_pr_dispatch_uses_owner_grant_not_retained_scope(monkeypatch, tmp_path, enforced, tool, action):
    from mimir.access_control import ScopedPRGrants, _SCOPED_PR_ISSUER
    from mimir.models import RepoPRAction, RepoPRActionScope, RepoReviewState, RepoPRScopeRegistry

    scope, context, policy, authorization, result, arguments = _case(monkeypatch, tmp_path)
    registered = SimpleNamespace(
        owner="cultivate", resource_scope="repo_pr",
        typed_action=RepoPRAction[action],
    )
    live = SimpleNamespace(path=tmp_path / "pr-checkout", recovery_id="issued", is_active=True)
    protocol = SimpleNamespace(
        owner="cultivate", checkout_action=RepoPRAction.CHECKOUT,
        action_type=RepoPRAction, live_lease=lambda *_args: live,
        validate_scope=lambda issued_action, issued_scope, grant: (
            grant.action == issued_action.value and grant.scope_id == issued_scope.scope_id
        ),
    )
    ordinary = RepoPRActionScope(
        provenance="poller_payload", canonical_repo=scope.repository,
        canonical_root=str(tmp_path), canonical_origin="https://github.com/owner/repo.git",
        principal="poller:github", event_type="pr_changes_requested_stale",
        allowed_operations=frozenset({protocol.checkout_action.value, registered.typed_action.value}),
        pr_number=scope.issue_id, head_repo=scope.repository, head_remote="origin",
        destination_ref="refs/heads/feature", observed_head_sha="a" * 40,
        base_ref="main", observed_base_sha="b" * 40, owner=registered.owner,
    )
    state = RepoReviewState(ordinary)
    # A grant is issued by the turn ingress and upgraded on checkout observation.
    grants = ScopedPRGrants("poller:github", (ordinary,),
                            protocols={registered.owner: protocol}, _issuer=_SCOPED_PR_ISSUER)
    token = grants.consume_checkout(registered.owner, ordinary)
    assert token is not None
    grants.observe_checkout(registered.owner, ordinary, live, token)
    policy = SimpleNamespace(tools={tool: registered}, scoped_actions={registered.owner: protocol})
    context.scoped_pr_grants = grants
    context.canonical_principal = "poller:github"
    context.repo_pr_scope_registry = RepoPRScopeRegistry((state,))
    policy.tools[tool].resource_scope = "repo_pr"
    authorization.tool_name = tool
    authorization.policy = policy
    authorization.enforcement_enabled = enforced
    authorization.repo_pr_action_scope = ordinary
    context.retained_factory_scope = None
    call = SimpleNamespace(tool_name=tool, policy=policy, auth_context=context,
                           authorization=authorization, arguments=arguments)
    assert budget_gate._scoped_pr_dispatch_refusal(call) is None
