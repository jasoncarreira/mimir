"""Information-flow initialization, propagation, and final-egress coverage."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.access_control import (
    SinkGate,
    ToolRegistry,
    audit_declassification,
    create_auth_context,
)
from mimir.agent import (
    Agent,
    _initialize_ifc_labels,
    _merge_ifc_labels,
    _propagate_ifc_labels,
)
from mimir.bridges._activity_panel import ActivityPanel
from mimir.bridges.base import Bridge, MessageUpdate, SendResult
from mimir.channel_registry import ChannelRegistry
from mimir.models import (
    AgentEvent,
    AuthContext,
    InformationFlowLabels,
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
    ("trigger", "service_principal", "channel_id", "source"),
    [
        ("scheduled_tick", "scheduler", "scheduler:heartbeat", None),
        ("poller", "poller", "poller:github-activity", "poller"),
    ],
)
def test_trusted_authorless_service_can_egress_to_triggering_channel_under_enforce(
    trigger: str,
    service_principal: str,
    channel_id: str,
    source: str | None,
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
        ("fetch_url", "ifc_label_blocked:network"),
        ("web_search", "ifc_label_blocked:network"),
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
        ("write_file", "/tmp/untrusted", "file"),
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
    )

    assert decision.allowed is False
    assert decision.reason == f"ifc_label_blocked:{sink_category}"


@pytest.mark.parametrize(
    ("trigger", "canonical", "tool_name"),
    [
        ("scheduled_tick", "scheduler", "write_file"),
        ("saga_session_end", "synthesis", "edit_file"),
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


def test_only_explicit_audited_admin_declassification_erases_labels(
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

    assert admin.labels == frozenset()
    assert admin.source_channels == labels.source_channels
    record = json.loads(events_path.read_text(encoding="utf-8"))
    assert record["type"] == "ifc_declassification"
    assert record["labels"] == sorted(ALL_LABELS)
    assert record["source_labels"]
    assert record["authenticated_admin"]["canonical_principal"] == "user-1"
    assert record["reason"] == "operator-approved destination"
    assert record["destination"] == "slack-C-public"
    assert record["policy_version"] == "ifc-test-v2"
    assert record["outcome"] == "approved"


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
            "_ifc_labels": _labels(sources=frozenset({"slack-C-private"})),
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
