"""Server-only attestation of durable memory proposals."""

from __future__ import annotations

import hashlib
import json
import struct
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mimir import approval_requests, memory_proposals, mid_turn_injection
from mimir.access_control import create_auth_context
from mimir.agent import Agent
from mimir.config import Config
from mimir.dispatcher import Dispatcher
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent
from mimir.saga.client import SagaStore
from mimir.turn_event_bus import TurnEventEmitter
from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_KEY, HTTP_EVENT_INGRESS_EXTRA_VALUE


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(approval_requests, "_PENDING", {})
    monkeypatch.setattr(approval_requests, "_RECENT", {})
    monkeypatch.setattr(approval_requests, "_EXPIRED", set())
    monkeypatch.setattr(memory_proposals, "_APPROVAL_BACKENDS", {})
    (tmp_path / "state").mkdir()
    (tmp_path / "state/identities.yaml").write_text("""people:
  - canonical: operator
    aliases: [discord-99]
    access: {roles: [admin]}
  - canonical: user
    aliases: [discord-98]
    access: {roles: [user]}
  - canonical: service
    aliases: [discord-97]
    access: {roles: [admin], is_service: true}
""")
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    saga = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=4)
    monkeypatch.setattr("mimir.saga.client._embed_text_sync",
                        lambda text: (struct.pack("4f", 1, 0, 0, 0), "stub", "stub", 4))
    cfg = replace(Config.from_env(), home=tmp_path, operator_alert_channel="discord-1",
                  midturn_injection_channels=("discord-",))
    notices = []
    dispatcher = Dispatcher(cfg, resolver=resolver)

    async def send_notice(channel, text):
        notices.append(text)
        return SimpleNamespace(sent=True)

    dispatcher.set_notice_sender(send_notice)
    agent = Agent.__new__(Agent)
    agent._config = cfg
    agent._identity_resolver = resolver
    agent._dispatcher = dispatcher
    agent._channels = None
    memory_proposals.configure_approvals(tmp_path, "discord-1", saga)

    def queue(content="  some fact\n"):
        return memory_proposals.queue_proposal(
            tmp_path, content=content, stream="semantic", rationale="reason",
            proposed_by="poller:papers", turn_id="poller-turn", origin_trigger="poller",
            origin_ref="feed:item:1", sources=(SimpleNamespace(
                source_kind="channel", integrity="untrusted", integrity_effect="active_ingest",
                resource_id="feed:item:1",
            ),),
        )

    def event(text, **overrides):
        values = dict(trigger="user_message", channel_id="discord-1", content=text,
                      author="discord-99", source="discord", source_id="message-99")
        values.update(overrides)
        return AgentEvent(**values)

    def record():
        return json.loads(memory_proposals.proposal_path(tmp_path).read_text().splitlines()[0])

    def atoms():
        return saga._ensure_conn().execute("SELECT id, content, source_type, provenance FROM atoms").fetchall()

    yield SimpleNamespace(home=tmp_path, resolver=resolver, saga=saga, agent=agent,
                          dispatcher=dispatcher, notices=notices, queue=queue,
                          event=event, record=record, atoms=atoms)
    mid_turn_injection.deactivate("discord-1")
    # SagaStore is owned by the fixture; pytest closes its temporary database.


@pytest.mark.asyncio
async def test_authenticated_preturn_stores_queued_bytes_once_without_model(setup, monkeypatch):
    env = setup
    async def unexpected_model(*args, **kwargs):
        pytest.fail("an approval must never invoke the model")
    monkeypatch.setattr(Agent, "_run_turn_body", unexpected_model)
    def unexpected_turn_setup(*args, **kwargs):
        pytest.fail("an approval must not enter ordinary turn setup")
    monkeypatch.setattr("mimir.agent._initialize_ifc_labels", unexpected_turn_setup)
    proposal_id = env.queue()
    record = env.record()
    assert record["content"] == "some fact"
    assert record["content_sha256"] == hashlib.sha256(b"some fact").hexdigest()
    result = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert result.kind == "memory_proposal_approval"
    assert len(env.atoms()) == 1
    atom = env.atoms()[0]
    assert atom[1] == record["content"]
    assert hashlib.sha256(atom[1].encode()).hexdigest() == record["content_sha256"]
    assert atom[2] == "operator_approved_proposal"
    assert json.loads(atom[3])["approved_by"] == "operator"
    assert env.record()["atom_id"] == atom[0]
    assert env.notices == [f"Stored {proposal_id} as {atom[0]}"]
    again = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert "already decided" in again.output
    assert len(env.atoms()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,content,expected", [
    ("approve:   edited text  ", "edited text", "approved"),
    ("decline", None, "declined"),
])
async def test_edit_and_decline(setup, decision, content, expected):
    env = setup
    proposal_id = env.queue()
    text = f"approve {proposal_id}:   edited text  " if decision.startswith("approve") else f"decline {proposal_id}"
    await env.agent.run_turn(env.event(text))
    assert env.record()["status"] == expected
    if content:
        assert env.atoms()[0][1] == content
        provenance = json.loads(env.atoms()[0][3])
        assert provenance["edited"] is True
        assert provenance["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    else:
        assert env.atoms() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "expired", "decided", "tampered"])
async def test_fail_closed_replies(setup, case):
    env = setup
    proposal_id = env.queue()
    if case == "unknown":
        proposal_id = "mp-aaaa" if proposal_id != "mp-aaaa" else "mp-bbbb"
    elif case != "decided":
        path = memory_proposals.proposal_path(env.home)
        record = env.record()
        if case == "expired":
            record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        else:
            record["content"] = "tampered"
        path.write_text(json.dumps(record) + "\n")
    else:
        await env.agent.run_turn(env.event(f"decline {proposal_id}"))
    result = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert {"unknown": "no pending", "expired": "expired", "decided": "already decided",
            "tampered": "hash mismatch"}[case] in result.output
    assert env.atoms() == []
    if case == "expired":
        assert env.record()["status"] == "expired"


@pytest.mark.asyncio
async def test_stale_registry_entry_cannot_reapprove_decided_record(setup, monkeypatch):
    env = setup
    proposal_id = env.queue()
    await env.agent.run_turn(env.event(f"decline {proposal_id}"))
    approval_requests._RECENT.clear()  # stale cross-process registration

    def stale(decision, edit, event, identity, now, approval_event, reply_source):
        event.extra["_memory_proposal_action"] = (env.home, proposal_id, decision, edit)
        return "granted"

    approval_requests.register(
        kind="mp", approval_id=proposal_id, channel_id="discord-1",
        description="stale", expires_at=time.monotonic() + 60,
        resolver=stale, inject_into_turn=False,
    )
    async def unexpected_store(*args, **kwargs):
        pytest.fail("decided proposal must not be stored")
    monkeypatch.setattr(env.saga, "store", unexpected_store)
    result = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert "already decided" in result.output
    assert env.atoms() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"source": "api"}, {"source": "web"}, {"source": "stdin"},
    {"author": "discord-98"}, {"author": "discord-97"},
    {"extra": {HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE}},
])
async def test_unauthenticated_reply_runs_ordinary_turn_without_store(setup, overrides, monkeypatch):
    env = setup
    proposal_id = env.queue()
    called = []
    def ordinary_turn(event, *args, **kwargs):
        called.append(event.content)
        raise RuntimeError("ordinary turn reached")
    monkeypatch.setattr("mimir.agent._initialize_ifc_labels", ordinary_turn)
    with pytest.raises(RuntimeError, match="ordinary turn reached"):
        await env.agent.run_turn(env.event(f"approve {proposal_id}", **overrides))
    assert called == [f"approve {proposal_id}"]
    assert env.atoms() == []
    assert env.record()["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,expected", [
    ("", "approved"), (":   edited text  ", "approved"),
    ("decline", "declined"), ("unknown", "pending"),
])
async def test_midturn_mp_consumed_without_injection(setup, suffix, expected, monkeypatch):
    env = setup
    proposal_id = env.queue()
    if suffix == "decline":
        text = f"decline {proposal_id}"
    elif suffix == "unknown":
        text = "approve mp-aaaa" if proposal_id != "mp-aaaa" else "approve mp-bbbb"
    else:
        text = f"approve {proposal_id}{suffix}"
    auth = create_auth_context(env.event("running"), env.resolver, enforce=True)
    mid_turn_injection.register_inflight("discord-1", emitter=TurnEventEmitter(
        None, turn_id="running", channel_id="discord-1", auth_context=auth,
    ))
    env.dispatcher._in_flight.add("discord-1")
    monkeypatch.setattr(env.dispatcher, "_injection_enabled", lambda channel: True)
    async def accepted(event):
        return True
    monkeypatch.setattr(env.dispatcher, "_authorize_bridge_event", accepted)
    assert await env.dispatcher.enqueue(env.event(text))
    assert mid_turn_injection._REGISTRY["discord-1"].queue == []
    assert env.record()["status"] == expected
    assert len(env.atoms()) == int(expected == "approved")
    if suffix == "unknown":
        assert "no pending" in env.notices[-1]
    else:
        assert env.notices
    if suffix == ":   edited text  ":
        assert env.atoms()[0][1] == "edited text"


@pytest.mark.asyncio
async def test_boot_reregisters_only_live_requests_and_supports_bare_digest_reply(setup):
    env = setup
    first = env.queue("first")
    expired = env.queue("second")
    path = memory_proposals.proposal_path(env.home)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    approval_requests._PENDING.clear()  # emulate process restart
    approval_requests._RECENT.clear()
    memory_proposals.configure_approvals(env.home, "discord-1", env.saga)
    assert [entry.approval_id for entry in approval_requests.pending("discord-1")] == [first]
    assert [json.loads(line)["status"] for line in path.read_text().splitlines()] == ["pending", "expired"]
    approval_requests.set_prompt_message_id(first, "digest-1")
    result = await env.agent.run_turn(env.event("approve", extra={"reply_to_message_id": "digest-1"}))
    assert result.kind == "memory_proposal_approval"
    assert env.atoms()[0][1] == "first"
    assert expired not in [entry.approval_id for entry in approval_requests.pending("discord-1")]


def test_mint_skips_durable_and_registry_reserved_ids(setup, monkeypatch):
    env = setup
    monkeypatch.setattr(approval_requests.secrets, "choice", lambda alphabet: "a")
    first = env.queue("first")
    assert first == "mp-aaaa"
    # Force a distinct next candidate after the reserved one.
    choices = iter("aaaa" + "bbbb")
    monkeypatch.setattr(approval_requests.secrets, "choice", lambda alphabet: next(choices))
    second = env.queue("second")
    assert second == "mp-bbbb"
