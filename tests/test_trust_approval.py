"""Operator trust replies, taxonomy boundaries and setter closure."""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mimir import approval_requests, trust_approval
from mimir.identities import IdentityResolver
from mimir.models import (
    AgentEvent, AuthContext, InformationFlowLabels, InformationFlowState,
    SourceKind, SourceLabel, TurnContext, TurnInteractivity,
)


@pytest.fixture
def operator(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(yaml.safe_dump({
        "people": [
            {"canonical": "operator", "aliases": ["discord-99"],
             "access": {"roles": ["admin"]}},
            {"canonical": "stranger", "aliases": ["discord-98"],
             "access": {"roles": ["user"]}},
            {"canonical": "other-admin", "aliases": ["discord-97"],
             "access": {"roles": ["admin"]}},
        ],
    }))
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    return resolver


def _turn(resolver, *, channel="discord-room", **auth_changes):
    source = SourceLabel(
        principal="external", domain="web", domain_qualifier=None,
        resource_id="https://hostile.example/secret?url=ATTACK", bridge_instance="web",
        sensitivity="public", authorized_principals=frozenset({"operator"}),
        source_kind="protected_tool", integrity="untrusted",
        integrity_effect="active_ingest",
    )
    labels = InformationFlowLabels().with_source(source)
    auth = replace(AuthContext(
        principal="discord-99", canonical_principal="operator", roles=("admin",),
        trigger="user_message", event_ingress=None, channel_id=channel,
        interactivity=TurnInteractivity.INTERACTIVE, ifc_labels=labels,
        ifc_state=InformationFlowState(labels=labels),
    ), **auth_changes)
    event = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=channel, content="please help")
    ctx = TurnContext(turn_id="live", session_id=channel, trigger="user_message",
                      channel_id=channel, started_at=0, channel_source="discord",
                      identity_resolver=resolver, auth_context=auth,
                      operator_ingress_event=event,
                      trust_owner_loop=asyncio.get_running_loop())
    return ctx, auth


@pytest.mark.parametrize("decision", ["trust", "keep"])
async def test_chat_operator_reply_clears_live_state_only_after_audited_reply(
    operator, monkeypatch, decision,
):
    audits = []
    monkeypatch.setattr(trust_approval, "log_durable_event_sync",
                        lambda name, **fields: audits.append((name, fields)))
    ctx, auth = _turn(operator)
    notices = []
    delivered = asyncio.get_running_loop().create_future()

    async def send(channel, notice):
        notices.append((channel, notice))
        delivered.set_result(None)

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)
    assert request_id and request_id.startswith("trust-")
    assert trust_approval.register_chat_request(ctx, auth) == request_id
    await asyncio.wait_for(delivered, 2)
    assert len(notices) == 1
    assert "ATTACK" not in notices[0][1] and "hostile.example" not in notices[0][1]
    assert auth.ifc_state.has_untrusted_active_ingest()
    reply = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=ctx.channel_id, content=f"{decision} {request_id}")
    resolution = approval_requests.resolve(reply, operator)
    assert resolution.entry.approval_id == request_id
    text = await trust_approval.complete_reply(reply, resolution, operator)
    assert request_id in text
    assert auth.ifc_state.has_untrusted_active_ingest() is (decision == "keep")
    assert auth.ifc_state.current().sources == auth.ifc_labels.sources
    assert len(audits) == 1
    assert audits[0][0] == ("ifc_ingest_taint_cleared" if decision == "trust"
                            else "ifc_ingest_taint_clear_declined")
    assert audits[0][1]["surface"] == "chat"
    assert audits[0][1]["source_groups"] == [
        {"domain": "web", "source_kind": "protected_tool", "count": 1},
    ]
    assert "ATTACK" not in json.dumps(audits) and "hostile.example" not in json.dumps(audits)
    assert approval_requests.resolve(reply, operator).status == "already_resolved"
    if decision == "trust":
        from mimir._context import reset_current_turn, set_current_turn
        from mimir.tools.budget_gate import _request_with_ingest_trust_notice

        assert ctx.trust_cleared_notice
        model_request = SimpleNamespace(
            system_message=None,
            override=lambda **values: SimpleNamespace(**values),
        )
        token = set_current_turn(ctx)
        try:
            updated = _request_with_ingest_trust_notice(model_request)
            assert "may retry it explicitly" in updated.system_message.content
            assert not ctx.trust_cleared_notice
            assert _request_with_ingest_trust_notice(model_request) is model_request
        finally:
            reset_current_turn(token)
        auth.ifc_state.merge(InformationFlowLabels().with_source(replace(
            auth.ifc_labels.sources[0], resource_id="https://other.example/new",
        )))
        assert auth.ifc_state.has_untrusted_active_ingest()
    approval_requests.cancel(request_id)


@pytest.mark.parametrize("change", [
    {"author": "discord-98"}, {"channel_id": "discord-elsewhere"},
    {"content": "trust trust-aaaa"}, {"source": "api"},
    {"trigger": "scheduled_tick"},
])
async def test_chat_reply_does_not_clear_for_wrong_identity_channel_or_id(operator, change):
    ctx, auth = _turn(operator)

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)
    reply = replace(AgentEvent(trigger="user_message", author="discord-99",
                               source="discord", channel_id=ctx.channel_id,
                               content=f"trust {request_id}"), **change)
    assert approval_requests.resolve(reply, operator).entry is None
    assert auth.ifc_state.has_untrusted_active_ingest()
    approval_requests.cancel(request_id)


async def test_completion_itself_rechecks_authenticated_operator(operator, monkeypatch):
    ctx, auth = _turn(operator)
    monkeypatch.setattr(trust_approval, "log_durable_event_sync", lambda *_a, **_kw: None)

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)
    valid = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=ctx.channel_id, content=f"trust {request_id}")
    resolution = approval_requests.resolve(valid, operator)
    assert resolution.entry.approval_id == request_id
    forged = replace(valid, source="api", extra=dict(valid.extra))
    assert await trust_approval.complete_reply(forged, resolution, operator) == "no pending request"
    assert auth.ifc_state.has_untrusted_active_ingest()
    assert request_id in {entry.approval_id for entry in approval_requests.pending(ctx.channel_id)}
    approval_requests.cancel(request_id)


async def test_authenticated_web_operator_can_clear_only_own_channel(operator, monkeypatch):
    from mimir.bridges.web_chat import _web_channel_for
    from mimir.operator_approval import _is_authenticated_operator

    channel = _web_channel_for("operator")
    ctx, original = _turn(operator, channel=channel, principal="operator")
    ctx.channel_source = "web"
    ctx.operator_ingress_event = AgentEvent(
        trigger="user_message", author="operator", source="web", channel_id=channel,
    )
    assert not _is_authenticated_operator(
        replace(ctx.operator_ingress_event, channel_id="web-other"), operator,
        allow_web=True,
    )
    audits = []
    monkeypatch.setattr(trust_approval, "log_durable_event_sync",
                        lambda name, **fields: audits.append(name))

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, original)
    assert request_id
    reply = replace(ctx.operator_ingress_event, content=f"trust {request_id}")
    resolution = approval_requests.resolve(reply, operator)
    assert resolution.entry.approval_id == request_id
    assert "trusted" in await trust_approval.complete_reply(reply, resolution, operator)
    assert not original.ifc_state.has_untrusted_active_ingest()
    assert audits == ["ifc_ingest_taint_cleared"]


async def test_chat_audit_failure_does_not_acknowledge(operator, monkeypatch):
    ctx, auth = _turn(operator)

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)

    def unavailable(*_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(trust_approval, "log_durable_event_sync", unavailable)
    reply = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=ctx.channel_id, content=f"trust {request_id}")
    resolution = approval_requests.resolve(reply, operator)
    assert "expired or new content" in await trust_approval.complete_reply(reply, resolution, operator)
    assert auth.ifc_state.has_untrusted_active_ingest()


async def test_expired_trust_id_is_ignored(operator):
    ctx, auth = _turn(operator)

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)
    entry = next(e for e in approval_requests.pending(ctx.channel_id)
                 if e.approval_id == request_id)
    reply = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=ctx.channel_id, content=f"trust {request_id}")
    resolution = approval_requests.resolve(reply, operator, now=entry.expires_at + 1)
    assert resolution.entry is None and resolution.status == "no_pending_request"
    assert auth.ifc_state.has_untrusted_active_ingest()


@pytest.mark.parametrize("change", [
    {"trigger": "poller"}, {"trigger": "scheduled_tick"},
    {"event_ingress": "http_event"}, {"is_service": True},
    {"roles": ("user",)},
])
async def test_no_notice_without_live_authenticated_operator_ingress(operator, change):
    ctx, auth = _turn(operator, **change)
    notices = []

    async def send(*args):
        notices.append(args)

    ctx.trust_notice_sender = send
    assert trust_approval.register_chat_request(ctx, auth) is None
    assert notices == []


async def test_no_request_from_a_different_live_turn_or_channel(operator):
    ctx, auth = _turn(operator)

    async def send(*_args):
        pytest.fail("cross-channel trust notice")

    ctx.trust_notice_sender = send
    ctx.channel_id = "discord-other"
    assert trust_approval.register_chat_request(ctx, auth) is None
    ctx.channel_id = auth.channel_id
    ctx.auth_context = replace(auth, ifc_state=InformationFlowState(labels=auth.ifc_labels))
    assert trust_approval.register_chat_request(ctx, auth) is None


def test_only_operator_completion_sites_can_set_ingest_acknowledgement():
    """Inventory every syntactic setter call, including future model tool code."""
    root = Path(__file__).resolve().parents[1] / "mimir"
    calls = set()
    assignments = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Attribute)
                       and target.attr == "_acknowledged_ingest" for target in targets):
                    assignments.add(path.relative_to(root).as_posix())
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "acknowledge_ingest_snapshot"):
                continue
            functions = [parent for parent in ast.walk(tree)
                         if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node in ast.walk(parent)]
            calls.add((path.relative_to(root).as_posix(), functions[-1].name))
    assert calls == {("acp/agent.py", "offer_ingest_trust"),
                     ("trust_approval.py", "complete_reply")}
    assert assignments == {"models.py"}


@pytest.mark.parametrize("kind", [*(kind.value for kind in SourceKind), "ATTACK-kind"])
def test_audit_and_notice_taxonomy_reject_untrusted_source_metadata(kind):
    source = SourceLabel.from_record({
        "principal": "hostile", "domain": "ATTACK-domain",
        "resource_id": "https://hostile.example/ATTACK-url",
        "bridge_instance": "hostile", "sensitivity": "public",
        "source_kind": kind, "integrity": "untrusted",
        "integrity_effect": "active_ingest",
    })
    snapshot = InformationFlowLabels().with_source(source)
    groups = trust_approval.source_groups(snapshot)
    assert groups == [{"domain": "other", "source_kind": (
        kind if kind in SourceKind._value2member_map_ else "other"
    ), "count": 1}]
    assert "ATTACK" not in trust_approval.summary(snapshot)
    assert "ATTACK" not in json.dumps(groups)


def test_covered_channel_and_turn_history_do_not_retaint_but_new_read_does():
    source = SourceLabel(
        principal="other", domain="turn_history", resource_id="hostile-record",
        bridge_instance="discord", sensitivity="private", source_kind="protected_tool",
        integrity="untrusted", integrity_effect="active_ingest",
    )
    previous = InformationFlowLabels().with_source(source)
    channel_state = InformationFlowState(labels=previous)
    other_channel = InformationFlowState(labels=previous)
    assert channel_state.acknowledge_ingest_snapshot(
        expected=previous, durable_audit=lambda _sources: True,
    )
    channel_state.merge(previous)  # Re-injecting historical provenance isn't a new read.
    assert not channel_state.has_untrusted_active_ingest()
    # An actual new read of the same URL/record is a fresh ingest, even though
    # the append-only label carrier deduplicates its source identity.
    channel_state.merge_with_receipt(previous, fresh_untrusted_ingest=True)
    assert channel_state.has_untrusted_active_ingest()
    snapshot, ordinal = channel_state.ingest_snapshot()
    assert not channel_state.acknowledge_ingest_snapshot(
        expected=snapshot, expected_ordinal=ordinal - 1,
        durable_audit=lambda _sources: True,
    )
    assert channel_state.acknowledge_ingest_snapshot(
        expected=snapshot, expected_ordinal=ordinal,
        durable_audit=lambda _sources: True,
    )
    assert not channel_state.has_untrusted_active_ingest()
    assert other_channel.has_untrusted_active_ingest()
    assert previous.sources == channel_state.current().sources
    channel_state.merge(InformationFlowLabels().with_source(replace(source, resource_id="new")))
    assert channel_state.has_untrusted_active_ingest()
    assert not channel_state.acknowledge_ingest_snapshot(
        expected=previous, durable_audit=lambda _sources: True,
    )
    assert channel_state.has_untrusted_active_ingest()


@pytest.mark.parametrize("trusted", [True, False])
async def test_chat_pending_trust_allows_trusted_arrival_rejects_new_untrusted(
    operator, monkeypatch, trusted,
):
    monkeypatch.setattr(trust_approval, "log_durable_event_sync", lambda *_a, **_kw: None)
    ctx, auth = _turn(operator)

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)
    auth.ifc_state.merge(InformationFlowLabels().with_source(replace(
        auth.ifc_labels.sources[0], domain="memory", resource_id="intervening",
        integrity="trusted" if trusted else "untrusted",
    )))
    reply = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=ctx.channel_id, content=f"trust {request_id}")
    resolution = approval_requests.resolve(reply, operator)
    text = await trust_approval.complete_reply(reply, resolution, operator)
    assert text.startswith("trusted") is trusted
    assert auth.ifc_state.has_untrusted_active_ingest() is (not trusted)


async def test_different_authenticated_admin_cannot_complete_request(operator, monkeypatch):
    ctx, auth = _turn(operator)

    async def send(*_args):
        pass

    ctx.trust_notice_sender = send
    request_id = trust_approval.register_chat_request(ctx, auth)
    valid = AgentEvent(trigger="user_message", author="discord-99", source="discord",
                       channel_id=ctx.channel_id, content=f"trust {request_id}")
    resolution = approval_requests.resolve(valid, operator)
    other = replace(valid, author="discord-97", extra=dict(valid.extra))
    assert trust_approval._is_authenticated_operator(other, operator, allow_web=True)
    monkeypatch.setattr(trust_approval, "log_durable_event_sync", lambda *_a, **_kw: None)
    assert await trust_approval.complete_reply(other, resolution, operator) == "no pending request"
    assert auth.ifc_state.has_untrusted_active_ingest()
    approval_requests.cancel(request_id)


async def test_unsupported_chat_surface_cannot_register_request(operator):
    ctx, auth = _turn(operator)
    ctx.channel_source = "api"

    async def send(*_args):
        pytest.fail("unsupported surface posted a notice")

    ctx.trust_notice_sender = send
    assert trust_approval.register_chat_request(ctx, auth) is None


@pytest.mark.parametrize("origin,surface", [("acp_session", "chat"), (None, "acp")])
async def test_operator_ingest_authority_is_bound_to_surface(operator, origin, surface):
    _ctx, auth = _turn(operator, origin_trigger=origin)
    assert not trust_approval.operator_ingest_allowed(auth, surface=surface)


def test_acknowledgement_only_persistence_does_not_carry_privacy_or_capabilities():
    from mimir.models import IngestAcknowledgement

    untrusted = SourceLabel(principal="external", domain="web", resource_id="read",
                            bridge_instance="web", sensitivity="private",
                            authorized_principals=frozenset({"operator"}),
                            source_kind="protected_tool", integrity="untrusted",
                            integrity_effect="active_ingest")
    old_labels = InformationFlowLabels().with_source(untrusted).with_label("old-private")
    ack = IngestAcknowledgement()
    first = InformationFlowState(labels=old_labels, ingest_acknowledgement=ack)
    snapshot, ordinal = first.ingest_snapshot()
    assert first.acknowledge_ingest_snapshot(expected=snapshot, expected_ordinal=ordinal,
                                            durable_audit=lambda _sources: True)
    later = InformationFlowState(ingest_acknowledgement=ack)
    baseline = InformationFlowState()
    trusted = replace(untrusted, resource_id="new-trusted", integrity="trusted",
                      authorized_principals=frozenset({"other"}))
    labels = InformationFlowLabels().with_source(trusted)
    assert later.merge(labels) == baseline.merge(labels)
    assert later.current() == labels
    assert later._sink_category_capabilities == baseline._sink_category_capabilities == {}
    assert not later.has_untrusted_active_ingest()
    later.merge(old_labels)
    assert not later.has_untrusted_active_ingest()
    later.merge_with_receipt(old_labels, fresh_untrusted_ingest=True)
    assert later.has_untrusted_active_ingest()
    assert ack._acknowledged_ingest == frozenset({ack.source_identity(untrusted)})
    assert all(isinstance(identity, str) and len(identity) == 64
               for identity in ack._acknowledged_ingest)


def test_acknowledgement_idle_expiry_and_source_cap_fail_closed(monkeypatch):
    from mimir.models import IngestAcknowledgement

    ack = IngestAcknowledgement()
    source = SourceLabel(principal="external", domain="web", resource_id="read",
                         bridge_instance="web", sensitivity="public",
                         source_kind="protected_tool", integrity="untrusted",
                         integrity_effect="active_ingest")
    state = InformationFlowState(labels=InformationFlowLabels().with_source(source),
                                 ingest_acknowledgement=ack)
    monkeypatch.setattr(IngestAcknowledgement, "MAX_SOURCES", 0)
    assert not state.acknowledge_ingest_snapshot(expected=state.current(), durable_audit=lambda _: True)
    monkeypatch.setattr(IngestAcknowledgement, "MAX_SOURCES", 1024)
    assert state.acknowledge_ingest_snapshot(expected=state.current(), durable_audit=lambda _: True)
    assert not state.has_untrusted_active_ingest()
    ack.last_used -= ack.IDLE_SECONDS + 1
    assert state.has_untrusted_active_ingest()
    assert not ack._acknowledged_ingest
