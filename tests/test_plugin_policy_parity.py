"""Frozen pre-plugin core policy oracle from 6daf3879fd0e4eb6ed1b28f871c29096126cbac4."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

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
SERVICE_CASES = {
    "research": ("memory_query", "fetch_url", "read_file", "send_message", "memory_store"),
    "github": ("pr_metadata", "repo_status", "repo_commit", "read_file", "send_message"),
    "heartbeat": ("list_schedules", "read_file", "send_message", "worklink_run"),
}
SERVICE_READABLE = ("filesystem", "repository", "schedule_metadata")
SERVICE_SINKS = ("message", "filesystem", "network", "bound_pull_request")
HOME_TOKEN = "<oracle-home>"


def _canonical(value: object, home: Path) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"))
            .replace(str(home), HOME_TOKEN) + "\n").encode("utf-8")


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


def _extractor_results(home: Path) -> dict[str, dict[str, object]]:
    arguments = {
        "target": "web:other", "destination": "web:other", "channel_id": "web:other",
        "file_path": str(home / "docs" / "safe.txt"), "path": str(home / "docs" / "safe.txt"),
        "url": "https://approved.example/data", "query": "ordinary query",
        "command": "true", "cwd": str(home / "scratch"), "artifact_root": str(home / "docs"),
        "scope": "state", "name": "oracle-job", "commitment_id": "42",
        "message_id": "43", "text": "message-body", "content": "content-body",
        "rationale": "reason-body", "body": "body-text", "message": "commit-message",
        "repository": "oracle/repo", "pull_request": 42,
    }
    auth = _context("user", False)
    result = {}
    for name, descriptor in sorted(TOOL_DESCRIPTORS.items()):
        row = {}
        for key, extractor, undeclared in (
            ("targets", descriptor.sink_target_extractor, _TARGET_EXTRACTOR_UNDECLARED),
            ("payloads", descriptor.sink_payload_extractor, _PAYLOAD_EXTRACTOR_UNDECLARED),
        ):
            row[key] = (
                "undeclared" if extractor is undeclared else
                None if extractor is None else
                list(extractor(name, arguments, auth))
            )
            row[f"{key}_empty"] = (
                "undeclared" if extractor is undeclared else
                None if extractor is None else
                list(extractor(name, {}, auth))
            )
        result[name] = row
    return result


def _boundary_results(home: Path) -> dict[str, object]:
    registry = policy.ToolRegistry()
    results = {}
    cases = {
        "read_safe": ("read_file", {"file_path": str(home / "docs" / "safe.txt")}, None),
        "read_protected": ("read_file", {"file_path": str(home / ".env")}, None),
        "aread_safe": ("aread", {"file_path": str(home / "docs" / "safe.txt")}, None),
        "aread_protected": ("aread", {"file_path": str(home / ".env")}, None),
        "send_own": ("send_message", {"channel_id": "web:origin", "text": "hello"}, "web:origin"),
        "send_other": ("send_message", {"channel_id": "web:other", "text": "hello"}, "web:other"),
        "fetch_approved": ("fetch_url", {"url": "https://approved.example/data"}, "https://approved.example/data"),
        "fetch_denied": ("fetch_url", {"url": "https://unapproved.example/data"}, "https://unapproved.example/data"),
        "fetch_verbatim": ("fetch_url", {"url": "https://verbatim.example/read"}, "https://verbatim.example/read"),
        "web_search": ("web_search", {"query": "ordinary query"}, policy._fixed_web_search_url()),
        "write_live": ("write_file", {"file_path": str(home / "state" / "live.txt"), "content": "new"}, str(home / "state" / "live.txt")),
        "write_scratch": ("write_file", {"file_path": str(home / "scratch" / "draft.txt"), "content": "new"}, str(home / "scratch" / "draft.txt")),
    }
    for case, (tool, arguments, target) in cases.items():
        for tainted in (False, True):
            for enforce in (False, True):
                auth = _context("user", tainted)
                auth.egress_state.approve_url("https://approved.example/data")
                auth.ingested_url_state.add("https://verbatim.example/read")
                outcome = registry.authorize_tool(
                    tool, auth, enforce=enforce, target_channel=target,
                    arguments=arguments,
                )
                results[f"{case}:{'tainted' if tainted else 'clean'}:{'enforced' if enforce else 'shadow'}"] = [
                    outcome.decision.value, outcome.allowed, outcome.would_block,
                    outcome.reason, outcome.required_tier.value,
                ]
    return results


def _middleware_results(home: Path) -> dict[str, object]:
    registry = policy.get_tool_registry()
    shadow_was_enabled = registry.is_shadow_logging_enabled
    results = {}
    try:
        for name, tool, args in (
            ("safe_read", "read_file", {"file_path": str(home / "docs" / "safe.txt")}),
            ("protected_read", "read_file", {"file_path": str(home / ".env")}),
            ("cross_channel", "send_message", {"channel_id": "web:other", "text": "hello"}),
        ):
            for enforce in (False, True):
                auth = replace(_context("user", False), enforcement_enabled=enforce)
                calls = []
                request = ToolCallRequest(
                    tool_call={"name": tool, "args": args, "id": "oracle-call", "type": "tool_call"},
                    tool=None, state=None, runtime=Runtime(context=auth),
                )

                def handler(_request: ToolCallRequest) -> ToolMessage:
                    calls.append(True)
                    return ToolMessage(content="oracle result", tool_call_id="oracle-call")

                result = budget_gate.BudgetGateMiddleware().wrap_tool_call(request, handler)
                results[f"{name}:{'enforced' if enforce else 'shadow'}"] = {
                    "handled": bool(calls), "status": result.status,
                    "sources": sorted((source.domain, source.integrity) for source in auth.ifc_state.labels.sources),
                }
    finally:
        if not shadow_was_enabled:
            registry.disable_shadow_logging()
    return results


def _conditional_surface(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    from mimir.tools import forge, mcp, registry

    result = {}
    with monkeypatch.context() as controlled:
        controlled.setattr(forge, "github_identity_is_degraded", lambda: False)
        controlled.setattr(mcp, "get_mcp_tools", lambda: ())
        controlled.setenv("TAVILY_API_KEY", "oracle-key")
        controlled.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
        for model_spec in ("openai:oracle", "claude_code:oracle"):
            for coding in (False, True):
                names = {
                    tool.name for tool in registry.all_mimir_tools(
                        model_spec, coding_enabled=coding, require_coding_available=False,
                    )
                }
                result[f"{model_spec}:coding={coding}"] = sorted(names)
        controlled.setenv("MIMIR_FETCH_URL_DISABLED", "1")
        controlled.delenv("TAVILY_API_KEY", raising=False)
        result["openai:oracle:web_disabled"] = sorted(
            tool.name for tool in registry.all_mimir_tools(
                "openai:oracle", coding_enabled=False,
            )
        )
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
        service = policy.ServicePrincipal(
            canonical="oracle-service", trigger="poller",
            capabilities=SERVICE_CASES[profile], readable_domains=SERVICE_READABLE,
            sink_destinations=SERVICE_SINKS, authority_profile=profile,
            capability_tier=policy.CapabilityTier.SCOPED_WITH_PROVENANCE,
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


def _outcomes(home: Path) -> dict[str, dict[str, list[object]]]:
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
                    arguments={"file_path": str(home / "missing.txt"),
                               "path": str(home / "missing.txt"),
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


def _principal(service: policy.ServicePrincipal) -> dict[str, object]:
    return {
        "canonical": service.canonical, "trigger": service.trigger,
        "capabilities": list(service.capabilities),
        "readable_domains": list(service.readable_domains),
        "sink_destinations": list(service.sink_destinations),
        "sink_policies": [list((p.operation, p.adapter, p.destination)) for p in service.sink_policies],
        "filesystem_read_roots": list(service.filesystem_read_roots),
        "owned_skill_directory": service.owned_skill_directory,
        "channel_memory_directory": service.channel_memory_directory,
        "saga_full_corpus_read": service.saga_full_corpus_read,
        "creation_path": service.creation_path,
        "authority_profile": service.authority_profile,
        "capability_tier": service.capability_tier,
        "declared_shell_commands": [str(command) for command in service.declared_shell_commands],
        "configured_delivery_channel": service.configured_delivery_channel,
        "proposal_surface": service.proposal_surface,
    }


def _snapshot(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    catalog = policy.get_operation_catalog()
    return {
        "baseline_sha": BASELINE,
        "authorization_fields": list(AUTHORIZATION_FIELDS),
        "descriptors": _descriptors(),
        "extractor_results": _extractor_results(home),
        "boundary_results": _boundary_results(home),
        "middleware_results": _middleware_results(home),
        "conditional_surface": _conditional_surface(monkeypatch),
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
            "read_resource_operations": sorted(read_policy.READ_RESOURCE_OPERATIONS),
            "read_backend": sorted(policy._READ_BACKEND_RESULT_TOOLS),
            "acp_hands_result_tools": sorted(policy._ACP_HANDS_RESULT_TOOLS),
            "channel_operations": sorted(policy.ChannelResourceAdapter._CHANNEL_OPERATIONS),
            "write_operations": sorted(policy.WriteResourceAdapter._WRITE_OPERATIONS),
            "write_resource_operations": sorted(policy.WriteResourceAdapter._RESOURCE_OPERATIONS),
            "fixed_service_sinks": sorted(policy._FIXED_SERVICE_SINK_OPERATIONS),
            "taint_independent_egress": sorted(policy._TAINT_INDEPENDENT_EGRESS_TOOLS),
            "worklink_build_tools": sorted(policy._WORKLINK_BUILD_TOOLS),
            "shell_process_tools": sorted(policy.SHELL_PROCESS_TOOL_NAMES),
            "protected_metadata_operations": sorted(policy.OperationCatalog._PROTECTED_METADATA_OPERATIONS),
            "pr_review_scope_profiles": sorted(policy._PR_REVIEW_SCOPE_AUTHORITY_PROFILES),
            "builtin_trigger_profile_config": {
                name: {key: (list(value) if isinstance(value, tuple) else value)
                       for key, value in sorted(config.items())}
                for name, config in sorted(policy._BUILTIN_TRIGGER_PROFILE_CONFIG.items())
            },
            "trusted_service_principals": {
                name: _principal(service)
                for name, service in sorted(policy._TRUSTED_SERVICE_PRINCIPALS.items())
            },
            "required_service_principals": sorted(policy._REQUIRED_SERVICE_PRINCIPALS),
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
        "authorization": _outcomes(home),
    }


def _prepare_inputs(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.mkdir()
    (home / "docs").mkdir()
    (home / "docs" / "safe.txt").write_text("ordinary text", encoding="utf-8")
    (home / ".env").write_text("ORACLE_SECRET=private", encoding="utf-8")
    (home / "state").mkdir()
    (home / "scratch").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    for variable in (
        "MIMIR_GITHUB_SELF_LOGIN", "GITHUB_REPOS", "MIMIR_EGRESS_APPROVED_URLS",
        "MIMIR_FILE_TOOL_ROOTS", "MIMIR_OPERATOR_ALERT_CHANNEL", "WORKLINK_REPO",
        "MIMIR_WORKLINK_REPO", "MIMIR_PR_CHECKOUT_LEASE_ROOT", "MIMIR_WEB_SEARCH_URL",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_original_core_policy_matches_frozen_oracle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = tmp_path / "oracle-home"
    _prepare_inputs(home, monkeypatch)
    fixture_bytes = FIXTURE.read_bytes()
    expected = json.loads(fixture_bytes)
    assert expected["baseline_sha"] == BASELINE
    assert len(expected["descriptors"]) == 100
    assert _canonical(expected, home) == fixture_bytes
    assert _canonical(_snapshot(home, monkeypatch), home) == fixture_bytes
