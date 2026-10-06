"""Typed, authenticated approval routing across request kinds."""

from __future__ import annotations

import re

import pytest

from mimir import approval_requests as requests
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent
from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_KEY, HTTP_EVENT_INGRESS_EXTRA_VALUE


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "_PENDING", {})
    monkeypatch.setattr(requests, "_RECENT", {})
    monkeypatch.setattr(requests, "_EXPIRED", set())
    (tmp_path / "state").mkdir()
    state = tmp_path / "state" / "identities.yaml"
    state.write_text("""people:
  - canonical: operator
    aliases: [discord-99]
    access: {roles: [admin]}
  - canonical: user
    aliases: [discord-98]
    access: {roles: [user]}
  - canonical: service
    aliases: [discord-97]
    access: {roles: [admin], is_service: true}
""", encoding="utf-8")
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    calls = []

    def callback(decision, edit, event, identity, now, approval_event, reply_source):
        calls.append((decision, edit))
        return "granted" if decision == "approve" else "declined"

    def add(kind, *, now=100.0, expires_at=400.0, supports_edits=False):
        return requests.register(
            kind=kind, channel_id="discord-1", description=f"{kind} request",
            now=now, expires_at=expires_at, resolver=callback,
            supports_edits=supports_edits,
        )

    return resolver, calls, add


def reply(text, *, reference=None, author="discord-99", source="discord", trigger="user_message", extra=None):
    return AgentEvent(
        trigger=trigger, channel_id="discord-1", content=text, author=author,
        source=source, extra={**({"reply_to_message_id": reference} if reference else {}), **(extra or {})},
    )


def test_typed_ids_bare_ambiguity_and_named_resolution(registry):
    identity, calls, add = registry
    op, mp = add("op"), add("mp", supports_edits=True)
    assert re.fullmatch(r"op-[a-z2-7]{4}", op.approval_id)
    assert re.fullmatch(r"mp-[a-z2-7]{4}", mp.approval_id)
    ambiguous = requests.resolve(reply("approve"), identity, now=101)
    assert ambiguous.status == "ambiguous"
    assert op.approval_id in ambiguous.message and mp.approval_id in ambiguous.message
    assert "op request" in ambiguous.message and "mp request" in ambiguous.message
    assert calls == []
    assert requests.resolve(reply(f"approve {mp.approval_id}: revised"), identity, now=102).status == "granted"
    assert calls == [("approve", "revised")]
    assert [e.approval_id for e in requests.pending("discord-1", now=102)] == [op.approval_id]
    assert requests.resolve(reply("approve"), identity, now=103).status == "granted"
    again = requests.resolve(reply(f"approve {mp.approval_id}"), identity, now=104)
    assert again.status == "already_resolved" and "already resolved" in again.message
    assert calls == [("approve", "revised"), ("approve", None)]


def test_unknown_expired_wrong_channel_and_no_id_reuse(registry):
    identity, calls, add = registry
    op = add("op", expires_at=101)
    mp = add("mp", expires_at=500)
    assert requests.resolve(reply("approve op-aaaa"), identity, now=100).message == "no pending request op-aaaa"
    expired = requests.resolve(reply(f"approve {op.approval_id}"), identity, now=102)
    assert expired.status == "no_pending_request" and expired.message == f"no pending request {op.approval_id}"
    other = reply(f"approve {mp.approval_id}")
    other.channel_id = "discord-2"
    assert requests.resolve(other, identity, now=102).status == "no_pending_request"
    assert calls == []
    # A recently expired ID is reserved even on explicit re-registration.
    with pytest.raises(ValueError, match="unavailable"):
        requests.register(kind="op", channel_id="discord-1", description="retry", now=103,
                          expires_at=400, resolver=lambda *args: "granted", approval_id=op.approval_id)


def test_discord_reply_reference_selects_only_matching_prompt(registry):
    identity, calls, add = registry
    op, mp = add("op"), add("mp")
    requests.set_prompt_message_id(op.approval_id, "111")
    requests.set_prompt_message_id(mp.approval_id, "222")
    assert requests.pending("discord-1", now=101)[0].prompt_message_id == "111"
    unrelated = requests.resolve(reply("approve", reference="333"), identity, now=101)
    assert unrelated.status == "ambiguous" and calls == []
    assert requests.resolve(reply("decline", reference="222"), identity, now=102).status == "declined"
    assert calls == [("decline", None)]
    assert requests.pending("discord-1", now=102)[0].approval_id == op.approval_id


def test_named_id_beats_newer_request_and_unrelated_reference(registry):
    identity, calls, add = registry
    op, mp = add("op"), add("mp")
    requests.set_prompt_message_id(op.approval_id, "111")
    requests.set_prompt_message_id(mp.approval_id, "222")
    assert requests.resolve(reply(f"approve {op.approval_id}", reference="222"), identity,
                            now=101).entry.approval_id == op.approval_id
    assert requests.pending("discord-1", now=101)[0].approval_id == mp.approval_id
    assert calls == [("approve", None)]


def test_reply_reference_is_discord_only_and_named_id_is_channel_bound(registry):
    identity, calls, add = registry
    op, mp = add("op"), add("mp")
    requests.set_prompt_message_id(op.approval_id, "111")
    slack = reply("approve", reference="111", source="slack")
    assert requests.resolve(slack, identity, now=101).status == "ambiguous"
    other_channel = reply(f"approve {mp.approval_id}")
    other_channel.channel_id = "discord-2"
    assert requests.resolve(other_channel, identity, now=101).status == "no_pending_request"
    assert calls == []


def test_missing_resolver_cannot_authorize_named_reply(registry):
    _, calls, add = registry
    mp = add("mp")
    assert requests.resolve(reply(f"approve {mp.approval_id}"), None, now=101).status == "unauthenticated_operator"
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"author": "discord-98"}, {"author": "discord-97"},
    {"author": None}, {"source": "api"}, {"source": "web"},
    {"source": "stdin"}, {"source": ""}, {"trigger": "scheduled_tick"},
    {"extra": {HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE}},
])
def test_unauthenticated_ingress_cannot_resolve_named_request(registry, changes):
    identity, calls, add = registry
    mp = add("mp")
    event = reply(f"approve {mp.approval_id}", **changes)
    assert requests.resolve(event, identity, now=101).status == "unauthenticated_operator"
    assert requests.pending("discord-1", now=101) == (mp,)
    assert calls == []


def test_edit_requires_kind_support_and_cancel_preserves_recent_id(registry):
    identity, calls, add = registry
    op = add("op")
    assert requests.resolve(reply(f"approve {op.approval_id}: edit"), identity, now=101).status == "not_an_approval_response"
    assert calls == []
    requests.cancel(op.approval_id)
    assert requests.resolve(reply(f"approve {op.approval_id}"), identity, now=102).status == "already_resolved"


@pytest.mark.parametrize("action", ["approve", "decline", "cancel", "expire"])
def test_recent_id_responses_do_not_disclose_other_channels(registry, action):
    identity, calls, add = registry
    entry = add("op", expires_at=102)
    if action in {"approve", "decline"}:
        requests.resolve(reply(f"{action} {entry.approval_id}"), identity, now=101)
    elif action == "cancel":
        requests.cancel(entry.approval_id)
    else:
        assert requests.pending("discord-1", now=103) == ()
    original_calls = list(calls)
    other = reply(f"approve {entry.approval_id}")
    other.channel_id = "discord-2"
    resolution = requests.resolve(other, identity, now=103)
    assert resolution.status == "no_pending_request"
    assert resolution.message == f"no pending request {entry.approval_id}"
    same_channel = requests.resolve(reply(f"approve {entry.approval_id}"), identity, now=103)
    expected = "no_pending_request" if action == "expire" else "already_resolved"
    assert same_channel.status == expected
    assert calls == original_calls


def test_recent_ids_are_reusable_only_after_retention_expires(registry):
    identity, _, add = registry
    entry = add("op")
    requests.resolve(reply(f"decline {entry.approval_id}"), identity, now=101)
    assert requests.pending("discord-1", now=101 + requests._RECENT_SECONDS) == ()
    replacement = requests.register(
        kind="op", channel_id="discord-2", description="replacement",
        now=102 + requests._RECENT_SECONDS, expires_at=200 + requests._RECENT_SECONDS,
        resolver=lambda *args: "declined", approval_id=entry.approval_id,
    )
    assert replacement.channel_id == "discord-2"


@pytest.mark.parametrize("kind", ["mp", "upd", "custom"])
@pytest.mark.parametrize("inject_into_turn", [False, True])
def test_preturn_routing_uses_entry_policy_not_kind(registry, kind, inject_into_turn):
    entry = requests.register(
        kind=kind, channel_id="discord-1", description="request", now=100,
        expires_at=400, resolver=lambda *args: "granted", inject_into_turn=inject_into_turn,
    )
    assert requests.is_non_turn_bound_reply(reply(f"approve {entry.approval_id}")) is (not inject_into_turn)
    assert requests.is_non_turn_bound_reply(reply(f"DECLINE {entry.approval_id.upper()}")) is (not inject_into_turn)
    assert requests.is_non_turn_bound_reply(reply("approve")) is False
    assert requests.is_non_turn_bound_reply(reply("approve upd-aaaa extra text")) is False
    assert requests.is_non_turn_bound_reply(reply("approve unknown-aaaa")) is True
    requests.cancel(entry.approval_id)
    assert requests.is_non_turn_bound_reply(reply(f"approve {entry.approval_id}")) is True


def test_pending_id_cannot_be_registered_twice(registry):
    _, _, add = registry
    op = add("op")
    with pytest.raises(ValueError, match="unavailable"):
        requests.register(kind="op", channel_id="discord-2", description="other", now=101,
                          expires_at=400, resolver=lambda *args: "granted",
                          approval_id=op.approval_id)
