from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from mimir.plugins.core import core_policy_registry, policy_membership, policy_scope
from mimir.plugins.manager import load_plugins
from mimir.tools import budget_gate
from mimir_cultivate import forge, policy as cultivate_policy


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

    monkeypatch.setattr(forge, "resolve_review_state_for_context", deny)
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

    monkeypatch.setattr(forge, "resolve_review_state_for_context", deny)
    auth = SimpleNamespace(retained_factory_scope=SimpleNamespace(repository="owner/repo", issue_id=7))
    with policy_scope(selected):
        assert budget_gate._resolve_standing_review(
            "repo_status", auth, {"repository": "owner/repo", "pull_request": 7}, policy=selected,
        ) == "review required"
    assert observed == [True]
