from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from mimir import access_control
from mimir._context import reset_current_turn, set_current_turn
from mimir.models import (
    AgentEvent, AuthContext, InformationFlowLabels, InformationFlowState,
    SourceLabel, TurnContext, TurnInteractivity,
)


def _labels() -> InformationFlowLabels:
    return InformationFlowLabels().with_source(SourceLabel(
        principal="external", domain="filesystem", resource_id="/private/secret.txt",
        bridge_instance="local", sensitivity="private",
        authorized_principals=frozenset({"operator"}), source_kind="protected_tool",
        integrity="untrusted", integrity_effect="active_ingest",
    ))


def _auth(**changes) -> AuthContext:
    labels = _labels()
    return replace(AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="user_message", channel_id="web:operator",
        interactivity=TurnInteractivity.INTERACTIVE, enforcement_enabled=True,
        ifc_labels=labels, ifc_state=InformationFlowState(labels),
    ), **changes)


@pytest.fixture
def live_turn(middleware_event_logger):
    auth = _auth()
    turn = TurnContext(
        turn_id="untaint-turn", session_id="session", trigger="user_message",
        channel_id=auth.channel_id, started_at=time.monotonic(), auth_context=auth,
    )
    token = set_current_turn(turn)
    try:
        yield turn
    finally:
        reset_current_turn(token)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"roles": ("user",)}, "admin_required"),
    ({"roles": (), "trigger": "poller"}, "admin_required"),
    ({"principal": ""}, "missing_authenticated_admin"),
    ({"canonical_principal": None}, "missing_authenticated_admin"),
    ({"is_service": True}, "service_identity_forbidden"),
    ({"roles": ("admin", "service")}, "service_identity_forbidden"),
    ({"service_authority": object()}, "service_identity_forbidden"),
    ({"principal": "service:poller"}, "service_identity_forbidden"),
    ({"trigger": "poller"}, "user_origin_required"),
    ({"origin_trigger": "scheduled_tick"}, "user_origin_required"),
    ({"event_ingress": "http_event"}, "user_origin_required"),
    ({"ifc_state": None}, "missing_ifc_state"),
])
def test_clear_refuses_non_admin_service_and_poller(live_turn, changes, reason):
    auth = replace(live_turn.auth_context, **changes)
    live_turn.auth_context = auth
    assert access_control.clear_live_ingest_taint(auth, turn_id=live_turn.turn_id) == (False, reason)
    if isinstance(auth.ifc_state, InformationFlowState):
        assert auth.ifc_state.permission_has_untrusted_active_ingest()


@pytest.mark.parametrize("case", ["auth", "turn", "id", "blank_id", "carrier", "turn_auth"])
def test_clear_requires_matching_live_turn(live_turn, case, monkeypatch):
    auth = live_turn.auth_context
    turn_id = live_turn.turn_id
    if case == "auth":
        auth = None
    elif case == "id":
        turn_id = "other"
    elif case == "blank_id":
        turn_id = ""
    elif case == "carrier":
        auth = _auth()
    elif case == "turn_auth":
        live_turn.auth_context = None
    if case == "turn":
        monkeypatch.setattr("mimir._context.get_current_turn", lambda: None)
    assert access_control.clear_live_ingest_taint(auth, turn_id=turn_id)[0] is False


def test_clear_audit_is_private_and_failure_is_atomic(live_turn, tmp_path, monkeypatch):
    auth = live_turn.auth_context
    original = auth.ifc_state.current()
    hostile = replace(original.sources[0], domain="/private/domain", source_kind="secret contents")
    auth.ifc_state.merge(InformationFlowLabels().with_source(hostile))
    assert access_control.clear_live_ingest_taint(auth, turn_id=live_turn.turn_id) == (True, "cleared")
    events = [json.loads(line) for line in (tmp_path / "middleware-events.jsonl").read_text().splitlines()]
    audit = next(event for event in events if event["type"] == "ifc_ingest_taint_cleared")
    assert audit["turn_id"] == "untaint-turn"
    assert audit["source_count"] == 2
    assert audit["source_groups"] == [
        {"domain": "filesystem", "source_kind": "protected_tool", "count": 1},
        {"domain": "other", "source_kind": "other", "count": 1},
    ]
    assert "private/" not in json.dumps(audit)
    assert "secret" not in json.dumps(audit)
    assert "arguments" not in audit
    assert "sources" not in audit
    auth.ifc_state.merge(original)

    def unavailable(*args, **kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr("mimir.event_logger.log_durable_event_sync", unavailable)
    assert access_control.clear_live_ingest_taint(auth, turn_id=live_turn.turn_id) == (False, "clear_failed")
    assert auth.ifc_state.permission_has_untrusted_active_ingest()


def test_clear_only_affects_permission_taint_and_repeats(live_turn):
    auth = live_turn.auth_context
    state = auth.ifc_state
    original = state.current()
    for _ in range(3):
        assert access_control.clear_live_ingest_taint(auth, turn_id=live_turn.turn_id)[0]
        assert not state.permission_has_untrusted_active_ingest()
        assert state.current() is original
        assert state.has_untrusted_active_ingest()
        assert access_control.saga_mutation_taint_refusal(auth) is not None
        assert original.persisted_integrity == "untrusted"
        assert original.persisted_integrity_effect == "active_ingest"
        state.merge(original)
        assert state.permission_has_untrusted_active_ingest()
    state.merge(InformationFlowLabels().with_source(replace(original.sources[0], resource_id="new")))
    assert state.permission_has_untrusted_active_ingest()


def test_clear_requires_labels_and_snapshot_match():
    state = InformationFlowState()
    assert state.permission_has_untrusted_active_ingest()
    assert not state.clear_ingest_taint(fallback=None, durable_audit=lambda _: True)
    state.merge(_labels())
    assert state.clear_ingest_taint(fallback=None, durable_audit=lambda _: True)
    state.labels = state.labels.with_label("confidential")
    assert state.permission_has_untrusted_active_ingest()


def test_continuation_clear_does_not_mutate_bound_turn(live_turn):
    from mimir.agent import _initialize_ifc_labels

    bound = live_turn.auth_context
    labels = _initialize_ifc_labels(AgentEvent(
        trigger="user_message", channel_id=bound.channel_id, source="web",
        content="continue", ifc_labels=bound.ifc_state.current(),
        continuation_auth_context=bound,
    ))
    # Same fresh state construction as CoreAgent.run_turn explicit_session_binding.
    continued = replace(bound, ifc_labels=labels, ifc_state=InformationFlowState(labels))
    live_turn.auth_context = continued
    assert access_control.clear_live_ingest_taint(continued, turn_id=live_turn.turn_id)[0]
    assert not continued.ifc_state.permission_has_untrusted_active_ingest()
    assert bound.ifc_state.permission_has_untrusted_active_ingest()
    assert bound.ifc_state.current() is bound.ifc_labels


@pytest.mark.parametrize(("tool", "target"), [
    ("write_file", "/tmp/private-output.txt"),
    ("fetch_url", "https://example.com/unapproved"),
    ("http_request", "https://example.com/output"),
    ("send_message", "slack-other"),
    ("shell_exec", "curl https://example.com"),
])
async def test_egress_still_requires_declassification(live_turn, tool, target, tmp_path, monkeypatch):
    from langchain.agents.middleware import ToolCallRequest
    from langgraph.runtime import Runtime
    from mimir.tools.budget_gate import BudgetGateMiddleware

    owned_tasks = set()
    monkeypatch.setattr("mimir.tools.budget_gate._background_tasks", owned_tasks)

    auth = live_turn.auth_context
    before = access_control.SinkGate.check_sink_flow(tool, target, auth.ifc_labels, auth, enforce=True)
    assert not before.allowed
    assert access_control.clear_live_ingest_taint(auth, turn_id=live_turn.turn_id)[0]
    after = access_control.SinkGate.check_sink_flow(tool, target, auth.ifc_labels, auth, enforce=True)
    assert not after.allowed
    assert after.reason == before.reason
    assert not auth.ifc_state.consume_sink_approval(
        current=auth.ifc_labels, sink_category=access_control.get_sink_category(tool).value,
        destination=target, canonical_principal=auth.canonical_principal,
    )
    arguments = {
        "write_file": {"file_path": target, "content": "secret"},
        "fetch_url": {"url": target},
        "http_request": {"url": target, "method": "POST", "body": "secret"},
        "send_message": {"channel_id": target, "text": "secret"},
        "shell_exec": {"command": target},
    }[tool]
    request = ToolCallRequest(
        tool_call={"name": tool, "args": arguments, "id": "sink", "type": "tool_call"},
        tool=None, state=None, runtime=Runtime(context=auth),
    )
    def unreachable(request):
        pytest.fail("untaint must not admit egress")

    result = BudgetGateMiddleware().wrap_tool_call(request, unreachable)
    assert result.status == "error"
    await asyncio.gather(*tuple(owned_tasks))
    events = [json.loads(line) for line in (tmp_path / "middleware-events.jsonl").read_text().splitlines()]
    denied = [event for event in events if event["type"] == "tool_call" and event.get("denied")]
    assert denied and denied[-1]["tool"] == tool, (result.content, events)
    print(json.dumps({"tool": tool, "sink_reason": after.reason, "event": denied[-1]}))
    if tool in {"write_file", "http_request"}:
        assert access_control.approve_live_declassification(
            auth, sink_category=access_control.get_sink_category(tool).value,
            destination=target, reason="one output still needs its own approval",
        )[0]
        approved = access_control.SinkGate.check_sink_flow(tool, target, auth.ifc_labels, auth, enforce=True)
        assert approved.allowed and approved.reason == "ifc_declassification_approved"
        assert not access_control.SinkGate.check_sink_flow(tool, target, auth.ifc_labels, auth, enforce=True).allowed
