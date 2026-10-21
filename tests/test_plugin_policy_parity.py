"""Frozen pre-plugin core policy oracle from bc19c6220617e8b69d458b879fb081881f501c69."""

from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

import pytest

from mimir import access_control as policy
from mimir import read_policy
from mimir.plugins.core import core_policy_registry, policy_membership, policy_scope
from mimir.plugins.manager import load_plugins
from mimir.models import AuthContext, InformationFlowLabels, InformationFlowState, SourceLabel, TurnInteractivity
from mimir.tool_descriptors import (
    TOOL_DESCRIPTORS,
    _PAYLOAD_EXTRACTOR_UNDECLARED,
    _TARGET_EXTRACTOR_UNDECLARED,
)
from mimir.tools import budget_gate


BASELINE = "bc19c6220617e8b69d458b879fb081881f501c69"
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
# Explicitly pin the entire coding-enabled runtime inventory, including the
# Worklink tools exposed on the default surface rather than the coding surface.
CODING_TOOLS = frozenset("""spawn_open_code pr_list pr_metadata pr_spec pr_files pr_diff
    pr_file_content pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews
    pr_comments pr_review_requests pr_submit_review pr_inline_review_comment
    pr_comment pr_edit_body issue_comment pr_rerequest_review unsupported_operation
    repo_checkout repo_cleanup repo_fetch repo_status repo_test repo_diff
    repo_unmerged repo_stage repo_commit repo_merge repo_merge_abort repo_rebase
    repo_rebase_abort repo_revert repo_revert_abort repo_push worklink_run
    worklink_resume""".split())
# This flag is a separate declaration from the target-aware tainted-write
# membership: both must survive migration to the selected plugin registry.
CODING_WRITES_CODE_OR_STATE = frozenset("""spawn_open_code worklink_run
    worklink_resume repo_checkout repo_cleanup repo_fetch repo_test repo_stage
    repo_commit repo_merge repo_merge_abort repo_rebase repo_rebase_abort repo_revert
    repo_revert_abort repo_push pr_submit_review pr_inline_review_comment pr_comment
    pr_edit_body issue_comment pr_rerequest_review""".split())


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
    from mimir.tools import mcp, registry

    result = {}
    with monkeypatch.context() as controlled:
        controlled.setattr(mcp, "get_mcp_tools", lambda: ())
        controlled.setenv("TAVILY_API_KEY", "oracle-key")
        controlled.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
        for model_spec in ("openai:oracle", "claude_code:oracle"):
            for coding in (False, True):
                names = {
                    tool.name for tool in registry.all_mimir_tools(
                        model_spec, policy=core_policy_registry(), require_provider_available=False,
                    )
                }
                result[f"{model_spec}:coding={coding}"] = sorted(names)
        controlled.setenv("MIMIR_FETCH_URL_DISABLED", "1")
        controlled.delenv("TAVILY_API_KEY", raising=False)
        result["openai:oracle:web_disabled"] = sorted(
            tool.name for tool in registry.all_mimir_tools(
                "openai:oracle", policy=core_policy_registry(),
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
            "repo_publish_tools": sorted(policy_membership("repo_publish_tools")),
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
    assert len(expected["descriptors"]) == 103
    assert _canonical(expected, home) == fixture_bytes
    retired = CODING_TOOLS | {"pr_review_others"}

    def without_retired(value):
        if isinstance(value, dict):
            return {key: without_retired(item) for key, item in value.items()
                    if key not in retired}
        if isinstance(value, list):
            return [without_retired(item) for item in value
                    if not (isinstance(item, str) and item in retired)]
        return value

    observed = without_retired(json.loads(_canonical(_snapshot(home, monkeypatch), home)))
    projected = without_retired(expected)
    projected["tables"]["profile_capability_companions"] = {
        profile: companions for profile, companions in
        projected["tables"]["profile_capability_companions"].items() if companions
    }
    scheduler = projected["tables"]["trusted_service_principals"]["scheduled_tick"]
    scheduler["sink_destinations"] = [
        destination for destination in scheduler["sink_destinations"]
        if destination not in {"spawn_process", "worklink"}
    ]
    scheduler["sink_policies"] = [
        item for item in scheduler["sink_policies"] if len(item) == 3
    ]
    for key, value in scheduler.items():
        assert observed["tables"]["trusted_service_principals"]["scheduled_tick"][key] == value, key
    assert observed.keys() == projected.keys()
    for category, value in observed.items():
        if isinstance(value, dict):
            assert value.keys() == projected[category].keys(), category
            for name, item in value.items():
                assert item == projected[category][name], (category, name)
        else:
            assert value == projected[category], category


def test_pre_extraction_coding_policy_is_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pytest.importorskip("mimir_cultivate")
    home = tmp_path / "oracle-home"
    _prepare_inputs(home, monkeypatch)
    frozen = json.loads(FIXTURE.read_bytes())
    tables = frozen["tables"]
    registry = core_policy_registry()
    retired = CODING_TOOLS

    assert len(CODING_TOOLS) == 40
    assert CODING_TOOLS <= frozen["descriptors"].keys()
    assert CODING_TOOLS <= frozen["authorization"].keys()
    assert CODING_TOOLS <= frozen["extractor_results"].keys()
    assert retired.isdisjoint(registry.tools)
    required_memberships = (
        "retained_repo_tools", "worklink_build_tools", "repo_publish_tools",
        "tainted_writes",
    )
    assert set(required_memberships) <= tables.keys()
    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        observed = _outcomes(home)
    for name in sorted(CODING_TOOLS):
        tool = selected.tools[name]
        descriptor = frozen["descriptors"][name]
        assert tool.flow == tables["flow"].get(name)
        assert tool.tier == tables["tier"].get(name)
        assert tool.operation_class == tables["catalog"][name]
        assert tool.result_domain == tables["protected_domains"].get(name)
        assert tool.readable_domain == tables["readable_domains"].get(name)
        assert tool.descriptor is not None
        assert tool.descriptor.sink_category == descriptor["sink_category"]
        assert tool.descriptor.sink_destination == descriptor["sink_destination"]
        assert tool.writes_code_or_state == (name in CODING_WRITES_CODE_OR_STATE)
        assert tool.profiles == frozenset(
            profile for profile, names in tables["profiles"].items() if name in names
        )
        for membership in required_memberships:
            assert (membership in tool.memberships) == (name in tables[membership])
        assert len(frozen["authorization"][name]) == 18
    for name in sorted(CODING_TOOLS):
        assert observed[name].keys() == frozen["authorization"][name].keys()
        for case, original in frozen["authorization"][name].items():
            current = observed[name][case]
            tool = selected.tools[name]
            if current == original:
                continue
            enforced = case.endswith(":enforced")
            tainted = "_tainted_" in case
            # These oracle contexts have neither a server-issued PR grant nor
            # a review state. Previously they stopped at the legacy scope gate;
            # selected tools must now fail at the more specific owner grant or
            # fail to extract a target from that missing state.
            if (tool.resource_scope == "repo_pr" and original[3] == "repo_pr_scope_denied"
                    and current[3] == "repo_pr_grant_denied"):
                assert tool.typed_action is not None and tool.flow == "source"
                expected = [*original[:3], "repo_pr_grant_denied", *original[4:]]
            elif (tool.resource_scope == "repo_pr" and original[3] == "repo_pr_scope_denied"
                  and current[3] == "sink_target_required"):
                assert tool.typed_action is not None and tool.flow in {"both", "sink"}
                expected = [*original[:3], "sink_target_required", "admin", *original[5:]]
            elif name == "unsupported_operation" and current[3] == "channel_target_required":
                assert tool.resource_scope == "channel" and original[3] == "repo_pr_scope_denied"
                expected = [*original[:3], "channel_target_required", "admin", *original[5:]]
            elif (tool.writes_code_or_state and "worklink_build_tools" not in tool.memberships
                  and tainted and current[3] == "write_blocked_by_untrusted_ingest"):
                assert original[3] in {
                    "repo_pr_scope_denied", "ifc_label_blocked:forge", "ifc_label_blocked:spawn",
                    "issue_repository_source_required", "admin_required", None,
                }
                expected = ["admin_required", False, True,
                            "write_blocked_by_untrusted_ingest", "admin", "unknown", False]
            elif name in {"worklink_run", "worklink_resume"}:
                assert tool.resource_scope == "worklink"
                assert not os.environ.get("WORKLINK_REPO") and not os.environ.get("MIMIR_WORKLINK_REPO")
                expected = ["resource_scoped", not enforced, True,
                            "sink_target_required", "admin", "both", not enforced]
            elif name == "spawn_open_code":
                assert not tainted and tool.descriptor.sink_destination == "spawn_process"
                expected = ["admin_required", not enforced, True,
                            "sink_target_required", "admin", "both", not enforced]
            else:
                pytest.fail(f"unreviewed coding authorization change: {name} {case}: {original} -> {current}")
            assert current == expected, (name, case, original, current)


def test_selected_coding_tools_reach_a_permitted_production_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pytest.importorskip("mimir_cultivate")
    from tests.test_repo_tools import _selected_checkout_and_follow_on_with_live_grants

    selected, scope, _auth, _follow_on, checkout, fetched = (
        _selected_checkout_and_follow_on_with_live_grants(tmp_path, monkeypatch)
    )
    assert selected.tools["repo_checkout"].owner == "cultivate"
    assert selected.tools["repo_fetch"].owner == "cultivate"
    assert scope.observed_head_sha
    assert checkout.status != "error"
    assert fetched.status != "error"


def test_selected_spawn_and_build_admit_clean_bound_destinations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pytest.importorskip("mimir_cultivate")
    from mimir.plugins.core import policy_scope
    from mimir.plugins.manager import load_plugins

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    selected = load_plugins(enable="cultivate").policy
    with policy_scope(selected):
        for tool, target, arguments in (
            ("spawn_open_code", str(home), {"cwd": str(home)}),
            ("worklink_run", str(repo), {}),
        ):
            for enforce in (False, True):
                decision = policy.ToolRegistry().authorize_tool(
                    tool, _context("admin", False), enforce=enforce,
                    target_channel=target, arguments=arguments,
                )
                assert decision.allowed and not decision.would_block, (tool, enforce, decision.reason)
                assert decision.flow_direction.value == "both"
