"""Cultivate's action vocabulary is registered only for its owner."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry
from mimir_cultivate.policy import RepoPRAction, register_action_protocol


def test_cultivate_action_registration():
    registry = PolicyRegistry()
    entry_point = SimpleNamespace(
        group="mimir.plugins", name="cultivate", value="mimir_cultivate:provider",
        dist=SimpleNamespace(metadata={"Name": "mimir-cultivate"}),
    )
    registry.select_plugin_candidates((SimpleNamespace(
        name="cultivate", distribution="mimir-cultivate", entry_point=entry_point,
        enabled=True, diagnostic=None,
    ),))
    validate = lambda *_: True
    register_action_protocol(registry.registrar("cultivate"), validate)
    frozen = registry.freeze()
    assert frozen.scoped_actions["cultivate"].action_type is RepoPRAction
    assert frozen.scoped_actions["cultivate"].validate_scope is validate
    assert frozen.scoped_actions["cultivate"].trusted_scope is not None
    with pytest.raises(TypeError):
        frozen.scoped_actions["foreign"] = frozen.scoped_actions["cultivate"]
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        register_action_protocol(registry.registrar("cultivate"), validate)


def test_cultivate_constructors_reject_unselected_registration():
    with pytest.raises(PolicyRegistrationError, match="scoped action protocol"):
        register_action_protocol(PolicyRegistry().registrar("cultivate"), lambda *_: True)
