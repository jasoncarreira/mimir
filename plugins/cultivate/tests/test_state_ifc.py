"""Cultivate state keeps core-only imports isolated during the migration."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mimir import models
from mimir_cultivate import policy
from mimir.plugins.manager import load_plugins
from mimir.access_control import create_auth_context
from mimir.plugins.registry import PolicyRegistry


@pytest.mark.parametrize("name", (
    "RepoPRAction", "RepoPRActionScope", "RepoPRScopeProvenance",
    "RepoPRScopeRegistry", "RepoReviewState", "RetainedFactoryScope",
    "ServerDiscoveredPRStates", "ServerDiscoveredPRScopeStore",
))
def test_provider_coding_state_reexport_is_core_identity(name: str) -> None:
    assert getattr(models, name) is getattr(policy, name)
    assert getattr(policy, name).__module__ == models.__name__


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
