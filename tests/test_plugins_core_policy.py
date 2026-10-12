"""Core descriptor declarations and the public projection share one registry."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from mimir.plugins.core import core_policy_registry, register_core_tools
from mimir.plugins.registry import PolicyRegistry
from mimir.tool_descriptors import (
    TOOL_DESCRIPTORS,
    _PAYLOAD_EXTRACTOR_UNDECLARED,
    _TARGET_EXTRACTOR_UNDECLARED,
)


def _extractor(value: object, undeclared: object) -> str:
    if value is undeclared:
        return "undeclared"
    if value is None:
        return "none"
    captured = [cell.cell_contents for cell in getattr(value, "__closure__", None) or ()]
    return f"{value.__module__}.{value.__qualname__}:{json.dumps(captured, sort_keys=True)}"


def test_all_frozen_core_descriptors_come_from_registrations() -> None:
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "plugin_core_policy_baseline.json").read_text()
    )["descriptors"]
    registry = core_policy_registry()
    assert registry.frozen
    assert len(registry.tools) == len(registry.descriptors) == len(expected) == 100
    assert set(registry.descriptors) == set(TOOL_DESCRIPTORS) == set(expected)
    for name, descriptor in TOOL_DESCRIPTORS.items():
        tool = registry.tools[name]
        assert tool.owner == "core"
        assert tool.policy_only and tool.factory is None
        assert tool.descriptor is descriptor
        row = {}
        for field in fields(descriptor):
            value = getattr(descriptor, field.name)
            if field.name == "sink_target_extractor":
                value = _extractor(value, _TARGET_EXTRACTOR_UNDECLARED)
            elif field.name == "sink_payload_extractor":
                value = _extractor(value, _PAYLOAD_EXTRACTOR_UNDECLARED)
            elif field.name == "result_origin":
                value = int(value)
            row[field.name] = value
        assert row == expected[name], name


def test_public_descriptors_are_the_core_registry_projection() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    assert registry.freeze().descriptors == TOOL_DESCRIPTORS
    with pytest.raises(TypeError):
        TOOL_DESCRIPTORS["other"] = TOOL_DESCRIPTORS["send_message"]
