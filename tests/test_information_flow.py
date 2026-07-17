"""Information-flow initialization, propagation, and final-egress coverage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir.access_control import SinkGate, ToolRegistry, audit_declassification
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
    )


def _labels(
    channel: str = "slack-C1",
    *,
    labels: frozenset[str] = frozenset({"private"}),
    sources: frozenset[str] | None = None,
) -> InformationFlowLabels:
    return InformationFlowLabels(
        labels=labels,
        source_channels=sources if sources is not None else frozenset({channel}),
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
        ("mcp_slack_send", "ifc_label_blocked:external_mcp"),
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
        after_transform, non_declassification, _auth(),
    )

    assert after_ordinary_admin.labels == ALL_LABELS


def test_only_explicit_audited_admin_declassification_erases_labels():
    labels = _labels(labels=ALL_LABELS)
    admin = audit_declassification(
        labels, "operator-approved destination", _auth(roles=("admin",)),
    )

    assert admin.labels == frozenset()
    assert admin.source_channels == labels.source_channels


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
    ctx = SimpleNamespace(
        ifc_labels=_labels(sources=frozenset({"slack-C-private"})),
        auth_context=_auth("slack-C-public"),
        delivered_channel_ids=set(),
        send_message_count=0,
        turn_event_emitter=None,
        last_assistant_message_id=None,
    )
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

    assert len(bridge.sends) == 1
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
