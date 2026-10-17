"""Cultivate's selected turn callbacks are carried by the frozen host policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir.agent import _selected_turn_hooks, _turn_tool_surface_ready, _unready_tool_owners
from mimir.models import AgentEvent
from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry
from mimir_cultivate import forge
from mimir_cultivate.registration import provider


def test_selected_provider_registers_owner_bound_turn_callbacks(monkeypatch) -> None:
    registry = PolicyRegistry()
    registry.select_plugin_candidates((SimpleNamespace(
        name="cultivate", enabled=True, diagnostic=None,
    ),))
    provider.register(registry.registrar("cultivate"))
    registry.freeze()

    assert set(registry.turn_hooks) == {
        ("cultivate", "tool_readiness"),
        ("cultivate", "failure_intervention"),
        ("cultivate", "budget_continuation"),
    }
    selected_policy = SimpleNamespace(
        turn_hooks=registry.turn_hooks,
        tools={"selected_tool": SimpleNamespace(owner="cultivate")},
    )
    config = SimpleNamespace(plugin_manager=SimpleNamespace(policy=selected_policy), coding_enabled=False)
    monkeypatch.setattr(forge, "_github_identity_degraded", False)
    assert _turn_tool_surface_ready(config) is False
    assert _unready_tool_owners(selected_policy) == frozenset()
    monkeypatch.setattr(forge, "_github_identity_degraded", True)
    assert _turn_tool_surface_ready(config) is False
    assert _unready_tool_owners(selected_policy) == frozenset({"cultivate"})

    event = AgentEvent(trigger="user_message", channel_id="web:one", content="hello")
    assert _selected_turn_hooks(registry, "failure_intervention")[0](event) is False
    assert _selected_turn_hooks(registry, "budget_continuation") == (
        registry.turn_hooks["cultivate", "budget_continuation"],
    )


def test_turn_callbacks_are_isolated_to_their_owner_and_frozen() -> None:
    registry = PolicyRegistry()
    registrar = registry.registrar("example")
    registrar.register_turn_hook("failure_intervention", lambda event: False)
    with pytest.raises(PolicyRegistrationError, match="duplicate"):
        registrar.register_turn_hook("failure_intervention", lambda event: True)
    registry.registrar("other").register_turn_hook("failure_intervention", lambda event: True)
    registry.freeze()
    assert len(_selected_turn_hooks(registry, "failure_intervention")) == 2
    with pytest.raises(TypeError):
        registry.turn_hooks["example", "failure_intervention"] = lambda event: True
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        registrar.register_turn_hook("another", lambda: True)


def test_unselected_policy_has_no_turn_callbacks() -> None:
    registry = PolicyRegistry()
    registry.freeze()
    assert _selected_turn_hooks(registry, "budget_continuation") == ()
    assert _selected_turn_hooks(registry, "failure_intervention") == ()
