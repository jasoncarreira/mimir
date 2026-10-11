"""Frozen pre-plugin core policy oracle from 6daf3879fd0e4eb6ed1b28f871c29096126cbac4."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from mimir import access_control as policy
from mimir import read_policy
from mimir.models import AuthContext, InformationFlowLabels, InformationFlowState, SourceLabel, TurnInteractivity
from mimir.tool_descriptors import (
    TOOL_DESCRIPTORS,
    _PAYLOAD_EXTRACTOR_UNDECLARED,
    _TARGET_EXTRACTOR_UNDECLARED,
)
from mimir.tools import budget_gate


BASELINE = "6daf3879fd0e4eb6ed1b28f871c29096126cbac4"
FIXTURE = Path(__file__).parent / "fixtures" / "plugin_core_policy_baseline.json"
AUTHORIZATION_FIELDS = (
    "decision", "allowed", "would_block", "reason", "required_tier", "flow", "shadow",
)


def _extractor(value: object, undeclared: object) -> str:
    if value is undeclared:
        return "undeclared"
    if value is None:
        return "none"
    # Factories carry the argument names or fixed target in their closure.
    closure = getattr(value, "__closure__", None)
    captured = [cell.cell_contents for cell in closure or ()]
    return f"{value.__module__}.{value.__qualname__}:{json.dumps(captured, sort_keys=True)}"


def _descriptors() -> dict[str, dict[str, object]]:
    result = {}
    for name, descriptor in sorted(TOOL_DESCRIPTORS.items()):
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
        result[name] = row
    return result


def _context(role: str, tainted: bool) -> AuthContext:
    labels = InformationFlowLabels()
    if tainted:
        labels = labels.with_source(SourceLabel(
            principal="oracle", domain="channel", resource_id="web:origin",
            bridge_instance="web", sensitivity="private",
            authorized_principals=frozenset({"oracle"}), source_kind="channel",
            integrity="untrusted", integrity_effect="active_ingest",
        ))
    profile = role.removeprefix("service:") if role.startswith("service:") else None
    service = None
    if profile is not None:
        capabilities = tuple(sorted(policy.TRIGGER_AUTHORITY_PROFILES[profile]))
        service = policy.ServicePrincipal(
            canonical="oracle-service", trigger="poller", capabilities=capabilities,
            readable_domains=tuple(sorted(set(policy._OPERATION_READABLE_DOMAIN.values()))),
            sink_destinations=tuple(sorted({
                descriptor.sink_destination for descriptor in TOOL_DESCRIPTORS.values()
                if descriptor.sink_destination is not None
            })), authority_profile=profile, capability_tier=policy.CapabilityTier.UNBOUNDED,
        )
    return AuthContext(
        principal="oracle-service" if service else "oracle",
        canonical_principal="oracle-service" if service else "oracle",
        roles=("service",) if service else (role,),
        event_ingress=None, trigger="poller" if service else "user_message",
        channel_id="web:origin", interactivity=TurnInteractivity.INTERACTIVE,
        is_service=service is not None, service_authority=service,
        ifc_labels=labels, ifc_state=InformationFlowState(labels),
    )


def _outcomes() -> dict[str, dict[str, list[object]]]:
    registry = policy.ToolRegistry()
    names = sorted(set(policy._TOOL_FLOW_MAP) | set(TOOL_DESCRIPTORS) | {
        "client_authorized_host_execution", "pr_review_others",
    })
    cases = {
        "user_clean_own": ("user", False, "web:origin"),
        "user_clean_other": ("user", False, "web:other"),
        "user_tainted_other": ("user", True, "web:other"),
        "admin_clean_own": ("admin", False, "web:origin"),
        "admin_tainted_other": ("admin", True, "web:other"),
        "research_clean_own": ("service:research", False, "web:origin"),
        "research_tainted_other": ("service:research", True, "web:other"),
        "github_clean_own": ("service:github", False, "web:origin"),
        "heartbeat_clean_other": ("service:heartbeat", False, "web:other"),
    }
    results = {}
    for name in names:
        rows = {}
        for case, (role, tainted, target) in cases.items():
            for enforce in (False, True):
                auth = _context(role, tainted)
                decision = registry.authorize_tool(
                    name, auth, enforce=enforce, target_channel=target,
                    arguments={"file_path": "/tmp/plugin-policy-oracle/missing.txt",
                               "path": "/tmp/plugin-policy-oracle/missing.txt",
                               "url": "https://unapproved.example/data",
                               "repository": "oracle/repo", "pull_request": 42,
                               "command": "true", "channel_id": target},
                )
                rows[f"{case}:{'enforced' if enforce else 'shadow'}"] = [
                    decision.decision.value, decision.allowed, decision.would_block,
                    decision.reason, decision.required_tier.value,
                    decision.flow_direction.value, decision.is_shadow_decision,
                ]
        results[name] = rows
    return results


def _snapshot() -> dict[str, object]:
    catalog = policy.get_operation_catalog()
    return {
        "baseline_sha": BASELINE,
        "authorization_fields": list(AUTHORIZATION_FIELDS),
        "descriptors": _descriptors(),
        "tables": {
            "flow": {name: policy.get_tool_flow_direction(name).value for name in sorted(policy._TOOL_FLOW_MAP)},
            "tier": {name: value.value for name, value in sorted(policy.TRIGGER_CAPABILITY_TIERS.items())},
            "profiles": {name: sorted(tools) for name, tools in sorted(policy.TRIGGER_AUTHORITY_PROFILES.items())},
            "bounded_profile_capabilities": {name: sorted(tools) for name, tools in sorted(policy.BOUNDED_PROFILE_CAPABILITIES.items())},
            "capability_companions": {name: sorted(tools) for name, tools in sorted(policy._CAPABILITY_COMPANIONS.items())},
            "profile_capability_companions": {profile: {name: sorted(tools) for name, tools in sorted(companions.items())} for profile, companions in sorted(policy._PROFILE_CAPABILITY_COMPANIONS.items())},
            "legacy_service_sink_tiers": {name: tier.value for name, tier in sorted(policy._LEGACY_SERVICE_SINK_TIERS.items())},
            "fetch_url_profiles": {name: list(value) for name, value in sorted(policy._FETCH_URL_POLICY_BY_AUTHORITY_PROFILE.items())},
            "scheduler_profiles": sorted(policy.SCHEDULER_AUTHORITY_PROFILES),
            "shell_profiles": dict(sorted(policy._SHELL_PROFILE_BY_AUTHORITY_PROFILE.items())),
            "operator_shell_profile": policy.OPERATOR_SHELL_PROFILE,
            "forge_actions": dict(sorted(policy._FORGE_TOOL_ACTIONS.items())),
            "repo_actions": dict(sorted(policy._REPO_TOOL_ACTIONS.items())),
            "retained_repo_tools": sorted(policy._RETAINED_REPO_TOOLS),
            "protected_domains": dict(sorted(policy._PROTECTED_RESULT_DOMAINS.items())),
            "readable_domains": dict(sorted(policy._OPERATION_READABLE_DOMAIN.items())),
            "read_backend": sorted(read_policy.READ_RESOURCE_OPERATIONS),
            "catalog": {name: catalog.get_decision(name).value for name in sorted(set(policy._TOOL_FLOW_MAP) | set(TOOL_DESCRIPTORS))},
            "catalog_open": sorted(policy.OperationCatalog._OPEN_OPERATIONS),
            "catalog_resource_scoped": sorted(policy.OperationCatalog._RESOURCE_SCOPED_OPERATIONS),
            "catalog_admin": sorted(policy.OperationCatalog._ADMIN_REQUIRED_OPERATIONS),
            "catalog_admin_builtin": sorted(policy.OperationCatalog._ADMIN_BUILTIN_TOOL_NAMES),
            "tainted_writes": sorted(policy._TAINTED_TURN_WRITE_TOOLS),
            "tainted_file_writes": sorted(policy._TAINTED_FILE_WRITE_TOOLS),
            "schedule_writes": sorted(policy._SCHEDULE_WRITE_TOOLS),
            "saga_mutations": sorted(policy._SAGA_MUTATION_OPERATIONS),
            "standing_review": sorted(budget_gate._STANDING_REVIEW_TOOLS),
            "remediation_effect": sorted(budget_gate._REMEDIATION_EFFECT_TOOLS),
            "typed_error_signal": sorted(budget_gate._TYPED_ERROR_SIGNAL_TOOLS),
        },
        "authorization": _outcomes(),
    }


def test_original_core_policy_matches_frozen_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_HOME", "/tmp/plugin-policy-oracle")
    monkeypatch.delenv("MIMIR_GITHUB_SELF_LOGIN", raising=False)
    monkeypatch.delenv("GITHUB_REPOS", raising=False)
    monkeypatch.delenv("MIMIR_EGRESS_APPROVED_URLS", raising=False)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert expected["baseline_sha"] == BASELINE
    assert len(expected["descriptors"]) == 100
    assert _snapshot() == expected
