"""Cultivate's action vocabulary is registered only for its owner."""

from __future__ import annotations

import pytest

from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry
from mimir_cultivate.policy import RepoPRAction, register_action_protocol


def test_cultivate_action_registration():
    registry = PolicyRegistry()
    validate = lambda *_: True
    register_action_protocol(registry.registrar("cultivate"), validate)
    frozen = registry.freeze()
    assert frozen.scoped_actions["cultivate"].action_type is RepoPRAction
    assert frozen.scoped_actions["cultivate"].validate_scope is validate
    with pytest.raises(TypeError):
        frozen.scoped_actions["foreign"] = frozen.scoped_actions["cultivate"]
    with pytest.raises(PolicyRegistrationError, match="frozen"):
        register_action_protocol(registry.registrar("cultivate"), validate)
