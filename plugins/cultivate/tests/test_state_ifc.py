"""Cultivate state keeps core-only imports isolated during the migration."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from mimir import models
from mimir.commitments import CommitmentRecord, CommitmentsStore, make_commitment_id
from mimir._context import (
    reset_current_turn, set_current_turn,
)
from mimir_cultivate.scheduler import active_pr_checkout_lease_paths
from mimir_cultivate import policy
from mimir.plugins.manager import load_plugins
from mimir.access_control import (
    CapabilityTier, ToolFlowDirection, ToolRegistry, authorize_repo_pr_tool,
    build_scheduled_tick_service_principal, build_trigger_service_principal,
    can_review_commitments, create_auth_context,
)
from mimir.plugins.core import policy_scope
from mimir.plugins.registry import PolicyRegistry
from mimir_cultivate.repository import create_pr_checkout_lease
from mimir.tools import registry as core_tools
from tests.test_agent import _FakeAgent, _build_agent
from tests.test_access_control import _github_scope_test_setup
from tests.test_pr_checkout_lease import _repo_and_scope
from tests.test_retained_checkout_readers import exercise_real_worklink_failure_turn


@pytest.mark.parametrize("name", (
    "RepoPRAction", "RepoPRActionScope", "RepoPRScopeProvenance",
    "RepoPRScopeRegistry", "RepoReviewState", "RetainedFactoryScope",
    "ServerDiscoveredPRStates", "ServerDiscoveredPRScopeStore",
))
def test_provider_coding_state_is_defined_in_provider(name: str) -> None:
    value = getattr(policy, name)
    assert value.__module__ == "mimir_cultivate.policy"


def test_provider_scope_refuses_unsupported_actions() -> None:
    scope_fields = dict(
        provenance="poller_payload", canonical_repo="owner/repo",
        canonical_root="/tmp/repo", canonical_origin="https://github.com/owner/repo.git",
        principal="owner", event_type="pr_review", pr_number=3,
        head_repo="owner/repo", head_remote="origin", destination_ref="refs/heads/main",
        observed_head_sha="a" * 40, base_ref="main", observed_base_sha="b" * 40,
    )
    with pytest.raises(ValueError, match="unsupported repo/PR action"):
        policy.RepoPRActionScope(allowed_operations=frozenset({"unknown.action"}), **scope_fields)
    scope = policy.RepoPRActionScope(
        allowed_operations=frozenset({policy.RepoPRAction.INSPECT.value}), **scope_fields,
    )
    assert scope.scope_id == policy.RepoPRActionScope(
        allowed_operations=frozenset({policy.RepoPRAction.INSPECT.value}), **scope_fields,
    ).scope_id
    state = policy.RepoReviewState(scope)
    assert policy.RepoPRScopeRegistry((state,)).resolve("owner/repo", 3) is state
    with pytest.raises(ValueError, match="duplicate pull-request scope target"):
        policy.RepoPRScopeRegistry((state, state))


def test_disabled_core_import_does_not_load_cultivate() -> None:
    program = """\
import sys
import mimir.models
import mimir.access_control
from mimir.plugins.manager import load_plugins
assert not load_plugins(enable='').plugins
assert not any(name == 'mimir_cultivate' or name.startswith('mimir_cultivate.')
               for name in sys.modules)
"""
    env = os.environ.copy()
    env.pop("MIMIR_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True,
        text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("enforce", [False, True])
def test_unrelated_scheduled_review_keeps_core_authority_and_no_coding_slot(
    tmp_path, enforce: bool,
) -> None:
    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        service = build_scheduled_tick_service_principal("commitments-review", tmp_path)
        event = models.AgentEvent(
            trigger="scheduled_tick", source="scheduler",
            channel_id="scheduler:commitments-review",
            service_principal=service.canonical, service_authority=service,
        )
        auth = create_auth_context(event, policy=selected, enforce=enforce)
        auth = replace(
            auth, server_discovered_pr_scope_store=policy.ServerDiscoveredPRScopeStore(),
        )
        assert auth.plugin_state("cultivate") is None
        assert can_review_commitments(auth)
        turn = models.TurnContext(
            "review", event.channel_id, event.trigger, event.channel_id, 0,
            auth_context=auth,
        )
        assert turn.auth_with_plugin_state() is auth


@pytest.mark.asyncio
async def test_selected_scheduled_review_turn_can_propose_closure(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    store = CommitmentsStore(path=home / ".mimir/commitments.jsonl")
    record = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="channel", text="Ship fix",
        owner_principal="operator",
    ))
    monkeypatch.setitem(core_tools._STATE, "commitments_store", store)

    class Review(_FakeAgent):
        async def astream(self, state, *, config, context=None, stream_mode="values"):
            from types import SimpleNamespace

            assert context.plugin_state("cultivate") is None
            await core_tools.commitment_propose_closure.coroutine(
                commitment_id=record.id, action="complete", evidence="PR merged",
                runtime=SimpleNamespace(context=context),
            )
            async for chunk in super().astream(
                state, config=config, context=context, stream_mode=stream_mode,
            ):
                yield chunk

    agent = _build_agent(tmp_path, fake_agent=Review([AIMessage(content="review done")]))
    agent._config.access_control_enforced = True
    assert "cultivate" in agent._config.plugin_manager.policy.state_slots
    service = build_scheduled_tick_service_principal("commitments-review", home)
    result = await agent.run_turn(models.AgentEvent(
        trigger="scheduled_tick", source="scheduler",
        channel_id="scheduler:commitments-review", content="Review commitments",
        service_principal="scheduler", service_authority=service,
    ), turn_id="selected-review")
    assert result.error is None


def test_selected_owner_registers_frozen_state_without_granting_authority() -> None:
    selected = load_plugins(enable="cultivate").policy
    assert selected.state_slots["cultivate"] is policy.CultivateState

    registry = PolicyRegistry()
    registrar = registry.registrar("cultivate")
    registrar.register_state_slot(policy.CultivateState)
    registry.freeze()
    auth = create_auth_context(
        models.AgentEvent(trigger="user_message", channel_id="channel", author="user"),
        policy=registry,
    )
    state = policy.CultivateState(ci_run_targets=frozenset({("owner/repo", 42)}))
    bound = auth.with_plugin_state(registrar, state)
    assert auth.plugin_state("cultivate") is None
    assert bound.plugin_state("cultivate") is state
    assert bound.repo_pr_action_scope is None
    assert bound.ci_run_targets == frozenset()
    with pytest.raises(TypeError, match="registered slot type"):
        auth.with_plugin_state(registrar, object())
    with pytest.raises(ValueError, match="matching owner registrar"):
        auth.with_plugin_state(PolicyRegistry().registrar("cultivate"), state)


@pytest.mark.parametrize("enforce", [False, True])
def test_selected_trusted_pr_ingress_uses_owner_state_and_refuses_untrusted_ingress(
    tmp_path, monkeypatch, enforce: bool,
) -> None:
    selected = load_plugins(enable="cultivate").policy
    _root, authority, item = _github_scope_test_setup(
        tmp_path, monkeypatch, selected_policy=selected,
    )
    event = models.AgentEvent(
        trigger="poller", channel_id="poller:github-activity",
        service_principal=authority.canonical, service_authority=authority,
        extra={"poller_name": "github-activity", "items": [item]},
    )
    with policy_scope(selected):
        trusted = create_auth_context(event, policy=selected, enforce=enforce)
        owner_state = trusted.plugin_state("cultivate")
        assert type(owner_state) is policy.CultivateState
        assert owner_state.repo_pr_scope_registry is trusted.repo_pr_scope_registry
        assert owner_state.repo_review_state is trusted.repo_review_state
        assert owner_state.repo_pr_action_scope is trusted.repo_pr_action_scope
        assert owner_state.server_discovered_pr_states is trusted.server_discovered_pr_states
        assert trusted.scoped_pr_grants is not None
        scope_store = policy.ServerDiscoveredPRScopeStore()
        continued = replace(trusted, server_discovered_pr_scope_store=scope_store)
        assert continued.plugin_state("cultivate").server_discovered_pr_scope_store is scope_store
        assert continued.server_discovered_pr_scope_store is scope_store

        refused = create_auth_context(
            event, event_ingress="http_event", policy=selected, enforce=enforce,
        )
        assert refused.plugin_state("cultivate").repo_pr_scope_registry is None
        assert refused.repo_pr_action_scope is None
        assert refused.scoped_pr_grants is None


@pytest.mark.parametrize("enforce", [False, True])
def test_selected_pr_grant_requires_owner_state_and_live_lease(
    tmp_path, monkeypatch, enforce: bool,
) -> None:
    from mimir.access_control import ScopedPRGrants, _SCOPED_PR_ISSUER

    _repo, original_scope = _repo_and_scope(tmp_path)
    scope = replace(original_scope, principal="poller:github")
    state = policy.RepoReviewState(scope)
    selected = load_plugins(enable="cultivate").policy
    protocol = selected.scoped_actions["cultivate"]
    registry = policy.RepoPRScopeRegistry((state,))
    grants = ScopedPRGrants(
        scope.principal, (scope,), protocols=selected.scoped_actions, _issuer=_SCOPED_PR_ISSUER,
    )
    auth = models.AuthContext(
        principal=scope.principal, canonical_principal=scope.principal, roles=(),
        event_ingress=None, trigger="poller", channel_id="poller:github",
        interactivity=models.TurnInteractivity.NON_INTERACTIVE,
        repo_pr_scope_registry=registry, repo_review_state=state,
        repo_pr_action_scope=scope, scoped_pr_grants=grants,
        _plugin_slot_policy=selected,
    )

    def decide() -> object:
        return authorize_repo_pr_tool(
            "repo_status", scope, service_principal=None, enforce=enforce,
            flow_direction=ToolFlowDirection.SOURCE,
            required_actions=(policy.RepoPRAction.INSPECT.value,), registered_plugin=True,
            auth_context=auth, owner="cultivate", action=policy.RepoPRAction.INSPECT,
            protocol=protocol, review_state=state,
        )

    assert decide().would_block  # Checkout has not issued a follow-on grant.
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    token = grants.consume_checkout("cultivate", scope)
    assert token is not None
    lease = create_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root, review_state=state,
    )
    assert grants.observe_checkout("cultivate", scope, lease, token)
    assert decide().allowed and not decide().would_block

    turn = models.TurnContext(
        "selected-pr", "poller:github", "poller", "poller:github", 0,
        auth_context=auth,
    )
    token = set_current_turn(turn)
    try:
        assert lease.path in active_pr_checkout_lease_paths()
        original_slots = auth.plugin_slots
        object.__setattr__(auth, "plugin_slots", (("cultivate", policy.CultivateState()),))
        assert lease.path not in active_pr_checkout_lease_paths()
        assert auth.repo_pr_scope_registry is None
        assert decide().would_block
        object.__setattr__(auth, "plugin_slots", original_slots)
        assert lease.path in active_pr_checkout_lease_paths()
    finally:
        reset_current_turn(token)

    assert decide().allowed
    discovered = policy.ServerDiscoveredPRStates()
    discovered.remember(state)
    auth = replace(
        auth, repo_pr_scope_registry=None, repo_review_state=None,
        repo_pr_action_scope=None, server_discovered_pr_states=discovered,
    )
    assert auth.plugin_state("cultivate").server_discovered_pr_states is discovered
    assert decide().allowed
    original_slots = auth.plugin_slots
    object.__setattr__(auth, "plugin_slots", (("cultivate", policy.CultivateState()),))
    assert decide().would_block
    object.__setattr__(auth, "plugin_slots", original_slots)
    state.revoke_checkout_lease(lease)
    assert decide().would_block


@pytest.mark.parametrize("enforce", [False, True])
def test_selected_ci_ingress_checks_exact_server_issued_target(
    tmp_path, monkeypatch, enforce: bool,
) -> None:
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        service = build_trigger_service_principal(
            canonical="poller:github-ci-watch", trigger="poller", profile="github",
            tier=CapabilityTier.CODE_EXECUTION,
            capabilities=("ci_run_jobs", "ci_recent_runs"), creation_path="test",
            policy=selected, poller_owner="cultivate",
        )
        event = models.AgentEvent(
            trigger="poller", source="poller", channel_id=service.canonical,
            service_principal=service.canonical, service_authority=service,
            extra={"poller_name": "github-ci-watch", "items": [
                {"event_type": "ci_failure", "repo": "owner/repo", "run_id": 42,
                 "branch": "main", "workflow_id": 7},
            ]},
        )
        auth = create_auth_context(event, policy=selected, enforce=enforce)
        assert auth.plugin_state("cultivate").ci_run_targets == frozenset({("owner/repo", 42)})
        registry = ToolRegistry()

        def check(context, run_id):
            return registry.authorize_tool(
                "ci_run_jobs", context, enforce=enforce,
                arguments={"repository": "owner/repo", "run_id": run_id},
            )

        assert check(auth, 42).allowed and not check(auth, 42).would_block
        assert check(auth, 43).would_block
        refused = create_auth_context(
            event, policy=selected, enforce=enforce, event_ingress="http_event",
        )
        assert refused.plugin_state("cultivate").ci_run_targets == frozenset()
        assert check(refused, 42).would_block

        empty = create_auth_context(
            replace(event, extra={"poller_name": "github-ci-watch", "items": []}),
            policy=selected, enforce=enforce,
        )
        object.__setattr__(empty, "plugin_slots", ((
            "cultivate", policy.CultivateState(ci_run_targets=frozenset({("owner/repo", 42)})),
        ),))
        assert check(empty, 42).would_block

        original_slots = auth.plugin_slots
        object.__setattr__(auth, "plugin_slots", (("cultivate", policy.CultivateState()),))
        assert check(auth, 42).would_block
        object.__setattr__(auth, "plugin_slots", original_slots)


@pytest.mark.asyncio
async def test_selected_retained_ingress_and_ifc_reader(tmp_path, monkeypatch) -> None:
    with policy_scope(load_plugins(enable="cultivate").policy):
        await exercise_real_worklink_failure_turn(
            tmp_path, monkeypatch, selected_provider=True,
        )


def test_rejected_action_cannot_widen_provider_scope() -> None:
    with pytest.raises(ValueError, match="unsupported repo/PR action"):
        policy.RepoPRActionScope(
            provenance=policy.RepoPRScopeProvenance.POLLER_PAYLOAD,
            canonical_repo="org/repo", canonical_root="/tmp/repo",
            canonical_origin="https://github.com/org/repo", principal="bot",
            event_type="pr_review", allowed_operations=frozenset({"admin"}),
            pr_number=1, head_repo="org/repo", head_remote="origin",
            destination_ref="refs/heads/main", observed_head_sha="a" * 40,
            base_ref="main", observed_base_sha="b" * 40,
        )


def test_retained_scope_rejects_relative_sandbox() -> None:
    with pytest.raises(ValueError, match="retained factory scope identity is invalid"):
        policy.RetainedFactoryScope(
            issue_id=1, signature="sig", occurrence_id="occ", run_id="run",
            attempt=1, session="session", repository="org/repo", branch="branch",
            sandbox="relative/path",
        )
