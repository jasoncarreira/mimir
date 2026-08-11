from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from mimir.access_control import (
    ServicePrincipal,
    SinkGate,
    ToolAuthorization,
    ToolFlowDirection,
    ToolRegistry,
    classify_protected_result,
)
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    NormalizedPullRequestSnapshot,
    RepoPRAction,
    RepoPRActionScope,
    RepoPRScopeRegistry,
    RepoReviewState,
    SourceLabel,
)
from mimir.forge import ReviewProjection
from mimir.tools.forge import set_forge_client
from mimir.tools.repo import (
    _enforcement_enabled,
    repo_checkout,
    repo_cleanup,
    repo_status,
    repo_test,
)


@pytest.fixture(autouse=True)
def _reset_forge_client() -> None:
    set_forge_client(None)
    yield
    set_forge_client(None)


def _scope(
    *actions: RepoPRAction,
    number: int = 7,
    head_sha: str = "a" * 40,
    provenance: str = "server_discovered",
) -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance=provenance,
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
        observed_head_sha=head_sha,
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


@pytest.mark.parametrize(
    "runtime",
    [None, SimpleNamespace(), SimpleNamespace(context=SimpleNamespace())],
)
def test_unknown_repo_enforcement_state_fails_closed_and_emits_event(
    monkeypatch: pytest.MonkeyPatch,
    runtime: object | None,
) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.event_logger.log_event_sync", capture)

    assert _enforcement_enabled(
        runtime,  # type: ignore[arg-type]
        repository="owner/repo",
        pull_request=7,
    ) is True

    assert captured == [(
        "repo_enforcement_state_unknown",
        {
            "repository": "owner/repo",
            "pull_request": 7,
            "fallback_enforcement": True,
        },
    )]


def test_repo_enforcement_state_preserves_explicit_flag() -> None:
    assert _enforcement_enabled(
        SimpleNamespace(context=SimpleNamespace(enforcement_enabled=False)),
        repository="owner/repo",
        pull_request=7,
    ) is False
    assert _enforcement_enabled(
        SimpleNamespace(context=SimpleNamespace(enforcement_enabled=True)),
        repository="owner/repo",
        pull_request=7,
    ) is True


@pytest.mark.asyncio
async def test_repo_wrapper_refuses_unconfigured_repository_without_scope() -> None:
    runtime = SimpleNamespace(context=AuthContext(
        principal=None, canonical_principal=None, roles=(), event_ingress=None,
        trigger="scheduled_tick", channel_id="scheduler:heartbeat",
        interactivity=None,
    ))

    with pytest.raises(ToolException, match="not configured in GITHUB_REPOS"):
        repo_status.func(
            repository="owner/repo", pull_request=7, runtime=runtime,
        )

    with pytest.raises(ToolException, match="not configured in GITHUB_REPOS"):
        await repo_test.coroutine(
            repository="owner/repo", pull_request=7, runtime=runtime,
        )


@pytest.mark.asyncio
async def test_repo_wrapper_refuses_unconfigured_unlisted_pull_request() -> None:
    inspect = _scope(RepoPRAction.INSPECT)
    context = _auth(inspect)

    with pytest.raises(ToolException, match="not configured in GITHUB_REPOS"):
        repo_status.func(
            repository="owner/repo", pull_request=8,
            runtime=SimpleNamespace(context=context),
        )
    with pytest.raises(ToolException, match="not configured in GITHUB_REPOS"):
        await repo_test.coroutine(
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
        return lease, ()

    monkeypatch.setattr("mimir.tools.repo.acquire_pr_checkout_lease", fake_checkout)
    runtime = SimpleNamespace(context=context)
    repo_checkout.func(repository="owner/repo", pull_request=7, runtime=runtime)

    first_state = registry.resolve("owner/repo", 7)
    second_state = registry.resolve("owner/repo", 8)
    assert first_state is not None and first_state.checked_out is True
    assert second_state is not None and second_state.checked_out is False
    with pytest.raises(ToolException, match="inactive_checkout"):
        repo_status.func(repository="owner/repo", pull_request=8, runtime=runtime)


class _RemediationForge:
    def __init__(self, *snapshots: NormalizedPullRequestSnapshot) -> None:
        self.snapshots = list(snapshots)
        self.snapshot_calls = 0
        self.review_calls = 0
        self.reviews = (
            ReviewProjection("1", "reviewer", "CHANGES_REQUESTED", "fix", "now", "a" * 40),
        )

    def get_pull_request_snapshot(self, repository, number):  # type: ignore[no-untyped-def]
        self.snapshot_calls += 1
        return self.snapshots.pop(0)

    def list_reviews(self, scope):  # type: ignore[no-untyped-def]
        self.review_calls += 1
        return self.reviews


def _snapshot(head_sha: str, *, state: str = "open") -> NormalizedPullRequestSnapshot:
    return NormalizedPullRequestSnapshot(
        state=state,
        number=7,
        author="mimir-bot",
        head_repo="owner/repo",
        head_remote="origin",
        head_ref="fix",
        head_sha=head_sha,
        base_ref="main",
        base_sha="b" * 40,
    )


def _remediation_runtime(monkeypatch: pytest.MonkeyPatch, client: _RemediationForge):
    old_scope = _scope(
        *RepoPRAction,
        provenance="poller_payload",
    )
    context = _auth(old_scope)
    set_forge_client(client)
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")

    def mint(repository, snapshot, *, event_type):  # type: ignore[no-untyped-def]
        assert event_type == "pr_changes_requested_stale"
        return replace(
            old_scope,
            provenance="server_discovered",
            observed_head_sha=snapshot.head_sha,
        )

    monkeypatch.setattr(
        "mimir.access_control.create_server_discovered_heartbeat_scope", mint,
    )
    return old_scope, context, SimpleNamespace(context=context)


def test_stale_own_remediation_head_is_reminted_and_checked_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_head = "c" * 40
    old_scope, context, runtime = _remediation_runtime(
        monkeypatch, _RemediationForge(_snapshot(fresh_head)),
    )
    monkeypatch.setattr("mimir.repo_tools.was_agent_push", lambda *_args: True)
    checked_out: list[RepoPRActionScope] = []

    def fake_checkout(scope, *, owner, review_state):  # type: ignore[no-untyped-def]
        checked_out.append(scope)
        lease = SimpleNamespace(
            path=tmp_path / "fresh", scope_id=scope.scope_id,
            head_sha=scope.observed_head_sha, owner=owner, is_active=True,
        )
        review_state.attach_checkout_lease(lease)
        return lease, ()

    monkeypatch.setattr("mimir.tools.repo.acquire_pr_checkout_lease", fake_checkout)

    result = repo_checkout.func(
        repository="owner/repo", pull_request=7, runtime=runtime,
    )

    assert result["head_sha"] == fresh_head
    assert checked_out[0].provenance.value == "server_discovered"
    assert context.repo_pr_scope_registry.resolve("owner/repo", 7).action_scope is old_scope
    assert context.server_discovered_pr_states.resolve(
        "owner/repo", 7,
    ).action_scope is checked_out[0]


def test_merged_remediation_pr_stops_before_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _old_scope, _context, runtime = _remediation_runtime(
        monkeypatch, _RemediationForge(_snapshot("c" * 40, state="closed")),
    )
    monkeypatch.setattr(
        "mimir.tools.repo.acquire_pr_checkout_lease",
        lambda *_args, **_kwargs: pytest.fail("closed PR reached checkout"),
    )

    result = repo_checkout.func(
        repository="owner/repo", pull_request=7, runtime=runtime,
    )

    assert result == {
        "status": "stopped",
        "message": "pull request is closed or merged",
    }


def test_remediation_head_advancing_twice_does_not_remint_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_head = "c" * 40
    third_head = "d" * 40
    client = _RemediationForge(_snapshot(second_head), _snapshot(third_head))
    old_scope, context, runtime = _remediation_runtime(monkeypatch, client)
    monkeypatch.setattr("mimir.repo_tools.was_agent_push", lambda *_args: True)
    reminted_heads: list[str] = []

    def mint(repository, snapshot, *, event_type):  # type: ignore[no-untyped-def]
        reminted_heads.append(snapshot.head_sha)
        return replace(
            old_scope,
            provenance="server_discovered",
            observed_head_sha=snapshot.head_sha,
        )

    monkeypatch.setattr(
        "mimir.access_control.create_server_discovered_heartbeat_scope", mint,
    )
    live_heads = iter((second_head, third_head))
    checked_out_heads: list[str] = []

    def fake_checkout(scope, *, owner, review_state):  # type: ignore[no-untyped-def]
        live_head = next(live_heads)
        checked_out_heads.append(scope.observed_head_sha)
        if scope.observed_head_sha != live_head:
            raise RuntimeError(
                f"PR head advanced: scoped head {scope.observed_head_sha} is stale; "
                f"fetched head is {live_head}"
            )
        lease = SimpleNamespace(
            path=tmp_path / "fresh", scope_id=scope.scope_id,
            head_sha=scope.observed_head_sha, owner=owner, is_active=True,
        )
        review_state.attach_checkout_lease(lease)
        return lease, ()

    monkeypatch.setattr("mimir.tools.repo.acquire_pr_checkout_lease", fake_checkout)
    repo_checkout.func(repository="owner/repo", pull_request=7, runtime=runtime)

    with pytest.raises(ToolException) as raised:
        repo_checkout.func(repository="owner/repo", pull_request=7, runtime=runtime)

    assert str(raised.value) == (
        f"repository checkout rejected: PR head advanced: scoped head {second_head} "
        f"is stale; fetched head is {third_head}"
    )
    assert client.snapshot_calls == 2
    assert reminted_heads == [second_head]
    assert checked_out_heads == [second_head, second_head]
    assert context.server_discovered_pr_states.resolve(
        "owner/repo", 7,
    ).action_scope.observed_head_sha == second_head


def test_failed_remint_preserves_existing_stale_head_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_head = "c" * 40
    old_scope, _context, runtime = _remediation_runtime(
        monkeypatch, _RemediationForge(_snapshot(fresh_head)),
    )
    monkeypatch.setattr(
        "mimir.access_control.create_server_discovered_heartbeat_scope",
        lambda *_args, **_kwargs: None,
    )

    def stale_checkout(scope, **_kwargs):  # type: ignore[no-untyped-def]
        assert scope is old_scope
        raise RuntimeError(
            f"PR head advanced: scoped head {scope.observed_head_sha} is stale; "
            f"fetched head is {fresh_head}"
        )

    monkeypatch.setattr("mimir.tools.repo.acquire_pr_checkout_lease", stale_checkout)

    with pytest.raises(ToolException) as raised:
        repo_checkout.func(repository="owner/repo", pull_request=7, runtime=runtime)

    assert str(raised.value) == (
        f"repository checkout rejected: PR head advanced: scoped head "
        f"{old_scope.observed_head_sha} is stale; fetched head is {fresh_head}"
    )


def test_external_head_advance_rereads_review_state_before_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RemediationForge(_snapshot("c" * 40))
    client.reviews = (
        ReviewProjection("2", "reviewer", "APPROVED", "ok", "later", "c" * 40),
    )
    _old_scope, _context, runtime = _remediation_runtime(monkeypatch, client)
    monkeypatch.setattr("mimir.repo_tools.was_agent_push", lambda *_args: False)
    monkeypatch.setattr(
        "mimir.tools.repo.acquire_pr_checkout_lease",
        lambda *_args, **_kwargs: pytest.fail("cleared review reached checkout"),
    )

    result = repo_checkout.func(
        repository="owner/repo", pull_request=7, runtime=runtime,
    )

    assert result["status"] == "stopped"
    assert "no longer has a blocking" in result["message"]
    assert client.review_calls == 1


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
        repo_pr_action_scope=scope,
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


@pytest.mark.asyncio
async def test_pr_scope_policy_shadows_then_enforces_with_the_same_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(RepoPRAction.PR_COMMENT)
    registry = ToolRegistry()
    registry.enable_shadow_logging()
    captured: list[tuple[str, dict[str, object]]] = []

    async def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.event_logger.log_event", capture)
    arguments = {"repository": "owner/repo", "pull_request": 7}
    shadow = registry.authorize_tool(
        "pr_metadata",
        replace(_auth(scope), enforcement_enabled=False),
        enforce=False,
        arguments=arguments,
    )
    await asyncio.sleep(0)
    enforced = registry.authorize_tool(
        "pr_metadata",
        _auth(scope),
        enforce=True,
        arguments=arguments,
    )

    assert shadow.allowed is True
    assert shadow.would_block is True
    assert enforced.allowed is False
    assert shadow.reason == enforced.reason == "repo_pr_scope_denied"
    events = [fields for kind, fields in captured if kind == "shadow_tool_decision"]
    assert len(events) == 1
    assert events[0]["reason"] == enforced.reason
    assert events[0]["would_block"] is True


@pytest.mark.asyncio
async def test_repo_scope_policy_shadows_then_enforces_with_the_same_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(RepoPRAction.INSPECT)
    registry = ToolRegistry()
    registry.enable_shadow_logging()
    captured: list[tuple[str, dict[str, object]]] = []

    async def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    monkeypatch.setattr("mimir.event_logger.log_event", capture)
    arguments = {"repository": "owner/repo", "pull_request": 7}
    shadow_context = replace(
        _auth(scope),
        enforcement_enabled=False,
        ifc_labels=InformationFlowLabels(),
    )
    enforced_context = replace(_auth(scope), ifc_labels=InformationFlowLabels())
    shadow = registry.authorize_tool(
        "repo_stage",
        shadow_context,
        enforce=False,
        arguments=arguments,
    )
    await asyncio.sleep(0)
    enforced = registry.authorize_tool(
        "repo_stage", enforced_context, enforce=True, arguments=arguments,
    )

    assert shadow.allowed is True
    assert shadow.would_block is True
    assert enforced.allowed is False
    assert shadow.reason == enforced.reason == "repo_pr_scope_denied"
    events = [fields for kind, fields in captured if kind == "shadow_tool_decision"]
    assert len(events) == 1
    assert events[0]["tool"] == "repo_stage"
    assert events[0]["reason"] == enforced.reason
    assert events[0]["would_block"] is True


def test_tool_modules_do_not_decide_pr_scope_policy_locally() -> None:
    tools_root = Path(__file__).parents[1] / "mimir" / "tools"
    offenders = {
        path.relative_to(tools_root).as_posix()
        for path in tools_root.rglob("*.py")
        if "allowed_operations" in path.read_text(encoding="utf-8")
    }

    assert offenders == set()


def test_repo_tools_does_not_decide_pr_scope_policy_locally() -> None:
    source = (Path(__file__).parents[1] / "mimir" / "repo_tools.py").read_text(
        encoding="utf-8",
    )

    assert "allowed_operations" not in source
    assert "authorize_repo_pr_tool(" in source
