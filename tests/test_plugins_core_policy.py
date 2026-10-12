"""Core descriptor declarations and the public projection share one registry."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from mimir import access_control as policy
from mimir.plugins.core import core_policy_registry, register_core_tools
from mimir.plugins.registry import PolicyRegistrationError, PolicyRegistry
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


def test_authority_profile_views_are_frozen_registry_projections() -> None:
    registry = core_policy_registry()
    assert policy.TRIGGER_AUTHORITY_PROFILES == registry.authority_profiles
    assert policy.TRIGGER_CAPABILITY_TIERS == registry.tiers
    assert policy.SCHEDULER_AUTHORITY_PROFILES == frozenset(
        name for name, profile in registry.profiles.items() if profile.scheduler
    )
    assert policy.BOUNDED_PROFILE_CAPABILITIES["session-boundary"] == frozenset({"fetch_url"})
    assert "fetch_url" not in policy.TRIGGER_AUTHORITY_PROFILES["session-boundary"]
    assert policy._SHELL_PROFILE_BY_AUTHORITY_PROFILE == registry.shell_profile_mappings
    assert policy._SHELL_PROFILE_BY_AUTHORITY_PROFILE["github"] == "repo_review"
    assert policy._FETCH_URL_POLICY_BY_AUTHORITY_PROFILE["heartbeat"] == (
        "approved_urls", "MIMIR_HEARTBEAT_APPROVED_URLS"
    )
    assert policy._CAPABILITY_COMPANIONS == registry.capability_companions
    assert policy._LEGACY_SERVICE_SINK_TIERS == registry.legacy_service_sink_tiers
    with pytest.raises(TypeError):
        policy.TRIGGER_CAPABILITY_TIERS["new"] = policy.CapabilityTier.UNBOUNDED
    with pytest.raises(TypeError):
        policy.TRIGGER_AUTHORITY_PROFILES["custom"] = frozenset()


def test_plugin_profile_cannot_bound_unbacked_capability() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    registrar = registry.registrar("example")
    registrar.register_capability("plugin_fetch")
    registrar.register_authority_profile("plugin_profile", bounded_capabilities={"plugin_fetch"})
    with pytest.raises(
        PolicyRegistrationError,
        match="plugin_profile: plugin profile cannot add foreign capability",
    ):
        registry.freeze()


def test_legacy_bound_exception_does_not_apply_to_other_core_profiles() -> None:
    registry = PolicyRegistry()
    register_core_tools(registry)
    registry.register_authority_profile("other", bounded_capabilities={"fetch_url"})
    with pytest.raises(PolicyRegistrationError, match="other: bounded capability outside profile"):
        registry.freeze()
