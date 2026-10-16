from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from mimir.access_control import (
    AccessTier, OperationDecision, ToolAuthorization, ToolFlowDirection,
    _RetainedResultReceipt, classify_protected_result,
)
from mimir.models import RetainedFactoryScope
from mimir.tools import budget_gate


def _case(monkeypatch):
    monkeypatch.setattr(
        "mimir.access_control._is_worklink_retained_checkout_service", lambda *_args, **_kw: True,
    )
    scope = RetainedFactoryScope(
        issue_id=1949, signature="failure", occurrence_id="occurrence",
        run_id="run", attempt=1, session="session", repository="owner/repo",
        branch="feature", sandbox="/tmp/retained",
    )
    context = SimpleNamespace(
        retained_factory_scope=scope, canonical_principal="poller:github",
        is_service=True, channel_id="poller:github",
    )
    pr_scope = SimpleNamespace(canonical_repo=scope.repository, pr_number=scope.issue_id,
                               observed_head_sha="a" * 40, scope_id="pr-scope")
    state = SimpleNamespace(action_scope=pr_scope)
    lease = SimpleNamespace(path="/lease/checkout", recovery_id="lease-id",
                            head_sha="a" * 40, scope_id="pr-scope", is_active=True)
    context.repo_pr_scope_registry = SimpleNamespace(resolve=lambda *_args: state)
    policy = SimpleNamespace(frozen=True, descriptors={}, scoped_actions={
        "cultivate": SimpleNamespace(live_lease=lambda *_args: lease,
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
    return _RetainedResultReceipt(tool, context, scope, result, failed, policy,
                                  execution_id="host-execution", head="a" * 40,
                                  lease_identity=("/lease/checkout", "lease-id"))


@pytest.mark.parametrize("enforced", [False, True])
def test_only_host_observed_retained_output_is_complete(monkeypatch, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
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


def test_receipt_cannot_attest_other_bytes_outcome_scope_or_policy(monkeypatch):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
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
def test_non_retained_plugin_results_remain_incomplete_with_receipt(monkeypatch, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
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


def test_receipt_rejects_mutated_output_even_on_same_result_object(monkeypatch):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
    receipt = _receipt("repo_diff", context, scope, result, False, policy)
    result.content = "different output"
    labels = classify_protected_result(
        "repo_diff", arguments, context, authorization,
        result=result, retained_receipt=receipt,
    )
    assert next(iter(labels.sources)).integrity == "untrusted"


def test_retained_dispatch_requires_live_lease_and_captures_actual_output(tmp_path, monkeypatch):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
    policy.tools["repo_diff"].resource_scope = "repo_pr"
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
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

    @contextmanager
    def lease(_home, received):
        events.append(("lease", received))
        yield SimpleNamespace(scope=received)
        events.append(("release", received))

    monkeypatch.setattr("mimir.worklink.retained_scope.retained_factory_effect_lease", lease)
    capture = budget_gate._ToolExecutionCapture()
    with budget_gate._retained_execution(call) as live:
        assert live is scope
        assert events == [("lease", scope), ("release", scope)]
    budget_gate._capture_retained_result(call, capture, live, result)
    assert events[-1] == ("release", scope)
    assert len(events) == 4
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
def test_retained_dispatch_refuses_missing_or_stale_authority_in_both_modes(monkeypatch, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
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
def test_retained_receipt_rejects_failed_and_mismatched_invocation(monkeypatch, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
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


@pytest.mark.parametrize("tool", ["repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit"])
@pytest.mark.parametrize("enforced", [False, True])
def test_selected_retained_middleware_attests_only_live_outputs(tmp_path, monkeypatch, tool, enforced):
    scope, context, policy, authorization, _, arguments = _case(monkeypatch)
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
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
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

    @contextmanager
    def lease(_home, received):
        yield SimpleNamespace(scope=received)

    monkeypatch.setattr("mimir.worklink.retained_scope.retained_factory_effect_lease", lease)
    observed = []

    def handler(_request):
        observed.append(True)
        return ToolMessage(content="observed output", tool_call_id="model-id")

    output = budget_gate.BudgetGateMiddleware(policy).wrap_tool_call(request, handler)
    assert output.content == "observed output"
    assert observed == [True]
    assert next(iter(labels[-1].sources)).integrity == "trusted"

    changed = ToolMessage(content="failed output", tool_call_id="model-id", status="error")
    budget_gate.BudgetGateMiddleware(policy).wrap_tool_call(request, lambda _request: changed)
    assert next(iter(labels[-1].sources)).integrity == "untrusted"

    mismatched = ToolMessage(content="forged output", tool_call_id="other-call")
    budget_gate.BudgetGateMiddleware(policy).wrap_tool_call(request, lambda _request: mismatched)
    assert next(iter(labels[-1].sources)).integrity == "untrusted"

    context.repo_pr_scope_registry = None
    budget_gate.BudgetGateMiddleware(policy).wrap_tool_call(request, handler)
    assert next(iter(labels[-1].sources)).integrity == "untrusted"

    context.retained_factory_scope = None
    monkeypatch.setattr(budget_gate, "_tool_call_refusal", lambda *_args, **_kw: ToolMessage(
        content="retained factory scope refused", tool_call_id="model-id", status="error",
    ))
    before = len(observed)
    refused = budget_gate.BudgetGateMiddleware(policy).wrap_tool_call(request, handler)
    assert refused.status == "error"
    assert len(observed) == before


@pytest.mark.parametrize("enforced", [False, True])
def test_ordinary_pr_dispatch_uses_owner_grant_not_retained_scope(monkeypatch, enforced):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
    policy.tools["repo_diff"].resource_scope = "repo_pr"
    policy.tools["repo_diff"].typed_action = None
    authorization.enforcement_enabled = enforced
    ordinary = SimpleNamespace(owner="cultivate")
    authorization.repo_pr_action_scope = ordinary
    context.retained_factory_scope = None
    context.repo_pr_scope_registry = SimpleNamespace(resolve=lambda *_args: SimpleNamespace(action_scope=ordinary))
    monkeypatch.setattr("mimir.access_control._valid_scoped_pr_grant", lambda *_args: True)
    call = SimpleNamespace(tool_name="repo_diff", policy=policy, auth_context=context,
                           authorization=authorization, arguments=arguments)
    assert budget_gate._scoped_pr_dispatch_refusal(call) is None
