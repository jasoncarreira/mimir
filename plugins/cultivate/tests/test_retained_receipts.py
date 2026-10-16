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
    policy = SimpleNamespace(frozen=True, descriptors={}, tools={"repo_diff": SimpleNamespace(
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


def test_only_host_observed_retained_output_is_complete(monkeypatch):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
    incomplete = classify_protected_result(
        "repo_diff", arguments, context, authorization, result=result,
    )
    assert next(iter(incomplete.sources)).integrity == "untrusted"
    receipt = _RetainedResultReceipt("repo_diff", context, scope, result, False, policy)
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
    ):
        receipt = _RetainedResultReceipt("repo_diff", context, scope, result, False, policy)
        params = dict(result=result, failed=False, authorization=authorization)
        params.update(changed)
        labels = classify_protected_result(
            "repo_diff", arguments, context, params["authorization"],
            result=params["result"], failed=params["failed"], retained_receipt=receipt,
        )
        assert next(iter(labels.sources)).integrity == "untrusted"


def test_retained_dispatch_requires_live_lease_and_captures_actual_output(tmp_path, monkeypatch):
    scope, context, policy, authorization, result, arguments = _case(monkeypatch)
    policy.tools["repo_diff"].resource_scope = "repo_pr"
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(budget_gate, "get_trusted_service_from_auth_context", lambda _ctx: object())
    monkeypatch.setattr(
        "mimir.access_control._is_worklink_retained_checkout_service", lambda *_args, **_kw: True,
    )
    call = SimpleNamespace(
        tool_name="repo_diff", policy=policy, auth_context=context,
        authorization=authorization, arguments=arguments,
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
    assert capture.retained_receipt.consume("repo_diff", context, scope, result, False, policy)
    assert not capture.retained_receipt.consume("repo_diff", context, scope, result, False, policy)

    @contextmanager
    def stale(_home, _scope):
        yield SimpleNamespace(scope=None, refusal_reason="stale occurrence")

    monkeypatch.setattr("mimir.worklink.retained_scope.retained_factory_effect_lease", stale)
    with pytest.raises(ToolException, match="stale occurrence"):
        with budget_gate._retained_execution(call):
            pytest.fail("handler ran without a live lease")
