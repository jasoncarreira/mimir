from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from mimir.access_control import (
    ServicePrincipal,
    SinkGate,
    ToolAuthorization,
    ToolFlowDirection,
    classify_protected_result,
)
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    RepoPRAction,
    RepoPRActionScope,
    RepoPRScopeRegistry,
    RepoReviewState,
    SourceLabel,
)
from mimir.tools.repo import repo_checkout, repo_cleanup, repo_status


def _scope(*actions: RepoPRAction, number: int = 7) -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="server_discovered",
        canonical_repo="owner/repo",
        canonical_root="/srv/repo",
        canonical_origin="https://github.com/owner/repo.git",
        principal="mimir-bot",
        event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(action.value for action in actions),
        pr_number=number,
        head_repo="owner/repo",
        head_remote="origin",
        destination_ref="refs/heads/fix",
        observed_head_sha="a" * 40,
        base_ref="main",
        observed_base_sha="b" * 40,
    )


def _auth(scope: RepoPRActionScope, *additional: RepoPRActionScope) -> AuthContext:
    states = tuple(RepoReviewState(item) for item in (scope, *additional))
    state = states[0]
    labels = InformationFlowLabels().with_source(SourceLabel(
        principal="service:heartbeat",
        domain="channel",
        resource_id="scheduler:heartbeat",
        bridge_instance="service:heartbeat",
        sensitivity="internal",
        authorized_principals=frozenset({"service:heartbeat"}),
    )).with_channel("scheduler:heartbeat")
    service = ServicePrincipal(
        canonical="heartbeat",
        trigger="scheduled_tick",
        capabilities=("pr_comment",),
        readable_domains=("repository",),
        sink_destinations=("bound_pull_request",),
        authority_profile="heartbeat",
    )
    return AuthContext(
        principal=None,
        canonical_principal="heartbeat",
        roles=(),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        interactivity=None,
        is_service=True,
        service_authority=service,
        enforcement_enabled=True,
        ifc_labels=labels,
        repo_pr_scope_registry=RepoPRScopeRegistry(states),
        repo_review_state=state if len(states) == 1 else None,
        repo_pr_action_scope=scope if len(states) == 1 else None,
    )


def test_repo_wrapper_refuses_missing_immutable_scope() -> None:
    runtime = SimpleNamespace(context=AuthContext(
        principal=None, canonical_principal=None, roles=(), event_ingress=None,
        trigger="scheduled_tick", channel_id="scheduler:heartbeat",
        interactivity=None,
    ))

    with pytest.raises(ToolException, match="no authorized pull requests"):
        repo_status.func(
            repository="owner/repo", pull_request=7, runtime=runtime,
        )


def test_repo_wrapper_refuses_unlisted_pull_request() -> None:
    inspect = _scope(RepoPRAction.INSPECT)
    context = _auth(inspect)

    with pytest.raises(ToolException, match="not authorized for this turn"):
        repo_status.func(
            repository="owner/repo", pull_request=8,
            runtime=SimpleNamespace(context=context),
        )


def test_repo_cleanup_refuses_without_active_lease() -> None:
    context = _auth(_scope(RepoPRAction.CHECKOUT))

    with pytest.raises(ToolException, match="no active checkout lease"):
        repo_cleanup.func(
            repository="owner/repo", pull_request=7,
            runtime=SimpleNamespace(context=context),
        )


def test_checkout_proof_is_isolated_to_named_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _scope(RepoPRAction.CHECKOUT, RepoPRAction.INSPECT)
    second = _scope(RepoPRAction.CHECKOUT, RepoPRAction.INSPECT, number=8)
    context = _auth(first, second)
    registry = context.repo_pr_scope_registry
    assert registry is not None

    def fake_checkout(scope, *, owner, review_state):
        lease = SimpleNamespace(
            path=tmp_path / str(scope.pr_number), scope_id=scope.scope_id,
            head_sha=scope.observed_head_sha, owner=owner, is_active=True,
        )
        review_state.attach_checkout_lease(lease)
        return lease

    monkeypatch.setattr("mimir.tools.repo.create_pr_checkout_lease", fake_checkout)
    runtime = SimpleNamespace(context=context)
    repo_checkout.func(repository="owner/repo", pull_request=7, runtime=runtime)

    first_state = registry.resolve("owner/repo", 7)
    second_state = registry.resolve("owner/repo", 8)
    assert first_state is not None and first_state.checked_out is True
    assert second_state is not None and second_state.checked_out is False
    with pytest.raises(ToolException, match="inactive_checkout"):
        repo_status.func(repository="owner/repo", pull_request=8, runtime=runtime)


def test_repo_source_label_is_exact_and_survives_to_bound_forge_sink() -> None:
    scope = _scope(RepoPRAction.INSPECT, RepoPRAction.PR_COMMENT)
    auth = _auth(scope)
    authorization = ToolAuthorization(
        tool_name="pr_diff",
        decision="resource_scoped",
        allowed=True,
        enforcement_enabled=True,
        flow_direction=ToolFlowDirection.SOURCE,
        repo_pr_action_scope=scope,
    )

    added = classify_protected_result("pr_diff", {}, auth, authorization)
    assert added is not None
    source = next(source for source in added.sources if source.domain == "repository")
    assert source.resource_id == f"owner/repo#pull/7@{'a' * 40}"
    assert source.integrity_effect == "informational"

    merged = auth.ifc_labels
    assert merged is not None
    for item in added.sources:
        merged = merged.with_source(item)
    target = f"owner/repo#pull/7@{'a' * 40}:{scope.scope_id}"
    allowed = SinkGate.check_sink_flow(
        "pr_comment", target, merged, auth, enforce=True,
    )
    assert allowed.allowed is True


def test_mixed_repo_input_denies_bound_forge_sink() -> None:
    scope = _scope(RepoPRAction.PR_COMMENT)
    auth = _auth(scope)
    labels = auth.ifc_labels
    assert labels is not None
    mixed = labels.with_source(SourceLabel(
        principal="service:other",
        domain="channel",
        resource_id="scheduler:other",
        bridge_instance="service:other",
        sensitivity="internal",
        authorized_principals=frozenset({"service:other"}),
    )).with_channel("scheduler:other")
    target = f"owner/repo#pull/7@{'a' * 40}:{scope.scope_id}"

    denied = SinkGate.check_sink_flow(
        "pr_comment", target, mixed, auth, enforce=True,
    )
    assert denied.allowed is False
    assert denied.reason == "ifc_label_blocked:forge"
