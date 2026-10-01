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


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_single_digest_records_reply_anchor_and_resolves_by_reference(setup, chunked):
    env = setup
    first = env.queue("digest fact")

    async def send(channel, text, *, final):
        assert channel == "discord-1"
        return SimpleNamespace(sent=True, message_id="last" if chunked else "first",
                               first_message_id="first" if chunked else None)

    assert await memory_proposals.post_review_digest(env.home, "discord-1", send)
    second = env.queue("later fact")
    entries = {e.approval_id: e for e in approval_requests.pending("discord-1")}
    assert entries[first].prompt_message_id == "first"
    assert entries[second].prompt_message_id is None
    result = await env.agent.run_turn(env.event("approve", extra={"reply_to_message_id": "first"}))
    assert result.kind == "memory_proposal_approval"
    assert env.atoms()[0][1] == "digest fact"
    assert [e.approval_id for e in approval_requests.pending("discord-1")] == [second]


@pytest.mark.asyncio
async def test_multi_digest_records_no_anchor_and_ambiguity_content_is_inert(setup):
    env = setup
    first = env.queue("@everyone <@123> https://example.org")
    second = env.queue("another fact")

    async def send(channel, text, *, final):
        return SimpleNamespace(sent=True, message_id="digest", first_message_id="header")

    assert await memory_proposals.post_review_digest(env.home, "discord-1", send)
    assert all(e.prompt_message_id is None for e in approval_requests.pending("discord-1"))
    resolution = approval_requests.resolve(
        env.event("approve", extra={"reply_to_message_id": "header"}), env.resolver,
    )
    assert resolution.status == "ambiguous"
    assert first in resolution.message and second in resolution.message
    assert "Pending approvals:" in resolution.message
    assert "@everyone" not in resolution.message and "<@123>" not in resolution.message
    assert "https://example.org" not in resolution.message
    assert "@\u200beveryone" in resolution.message and "https:\u200b//" in resolution.message
    assert env.atoms() == []
    assert len(approval_requests.pending("discord-1")) == 2


@pytest.mark.asyncio
async def test_failed_digest_records_no_reply_anchor(setup):
    env = setup
    env.queue()

    async def send(channel, text, *, final):
        return SimpleNamespace(sent=False, message_id="partial", first_message_id="header")

    assert not await memory_proposals.post_review_digest(env.home, "discord-1", send)
    entry, = approval_requests.pending("discord-1")
    assert entry.prompt_message_id is None


def test_mint_skips_durable_and_registry_reserved_ids(setup, monkeypatch):
    env = setup
    monkeypatch.setattr(approval_requests.secrets, "choice", lambda alphabet: "a")
    first = env.queue("first")
    assert first == "mp-aaaa"
    memory_proposals._update(env.home, first, "declined")
    approval_requests._PENDING.clear()
    approval_requests._RECENT.clear()
    # Only the durable decided record now excludes this ID.
    choices = iter("aaaa" + "bbbb")
    monkeypatch.setattr(approval_requests.secrets, "choice", lambda alphabet: next(choices))
    second = env.queue("second")
    assert second == "mp-bbbb"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_line", ["{not json", "{}", "[]"])
async def test_corrupt_store_does_not_crash_boot_and_refuses_approval(setup, caplog, bad_line, monkeypatch):
    from mimir import event_logger

    env = setup
    events_path = env.home / "events.jsonl"
    monkeypatch.setattr(event_logger, "_logger", event_logger.EventLogger(events_path, "corrupt-store-test"))
    proposal_id = env.queue()
    path = memory_proposals.proposal_path(env.home)
    with path.open("a") as file:
        file.write(bad_line + "\n")
    original = path.read_bytes()
    approval_requests._PENDING.clear()
    approval_requests._RECENT.clear()
    memory_proposals.configure_approvals(env.home, "discord-1", env.saga)
    assert "registration skipped" in caplog.text
    logged = [json.loads(line) for line in events_path.read_text().splitlines()]
    corrupt, = [event for event in logged if event["type"] == "memory_proposal_store_corrupt"]
    assert corrupt["line_number"] == 2
    assert corrupt["channel_id"] == "discord-1"
    assert "some fact" not in events_path.read_text()
    assert approval_requests.pending("discord-1") == ()
    result = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert "malformed proposal store record at line 2" in result.output
    assert path.read_bytes() == original
    assert env.atoms() == []
    with pytest.raises(memory_proposals.ProposalRefusal, match="line 2"):
        memory_proposals._update(env.home, proposal_id, "approved")
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "decline"])
@pytest.mark.parametrize("midturn", [False, True])
async def test_ambiguous_bare_mp_reply_lists_requests_without_model(setup, monkeypatch, decision, midturn):
    env = setup
    first = env.queue("first")
    second = env.queue("second")
    if midturn:
        auth = create_auth_context(env.event("running"), env.resolver, enforce=True)
        mid_turn_injection.register_inflight("discord-1", emitter=TurnEventEmitter(
            None, turn_id="running", channel_id="discord-1", auth_context=auth,
        ))
        env.dispatcher._in_flight.add("discord-1")
        monkeypatch.setattr(env.dispatcher, "_injection_enabled", lambda channel: True)
        async def accepted(event):
            return True
        monkeypatch.setattr(env.dispatcher, "_authorize_bridge_event", accepted)
        assert await env.dispatcher.enqueue(env.event(decision))
        assert mid_turn_injection._REGISTRY["discord-1"].queue == []
    else:
        def unexpected(*args, **kwargs):
            pytest.fail("ambiguous approval entered the model turn")
        monkeypatch.setattr("mimir.agent._initialize_ifc_labels", unexpected)
        result = await env.agent.run_turn(env.event(decision))
        assert result.kind == "memory_proposal_approval"
    assert env.notices == [f"Pending approvals:\n{first}: first\n{second}: second"]
    assert len(approval_requests.pending("discord-1")) == 2
    assert env.atoms() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "nonstored", "duplicate_without_id", "hash", "empty_edit"])
async def test_nonstored_result_can_be_retried_immediately(setup, monkeypatch, failure):
    env = setup
    proposal_id = env.queue()
    path = memory_proposals.proposal_path(env.home)
    original = path.read_text()
    store = env.saga.store
    if failure == "exception":
        async def failed(*args, **kwargs):
            raise RuntimeError("embedding unavailable")
        monkeypatch.setattr(env.saga, "store", failed)
    elif failure in {"nonstored", "duplicate_without_id"}:
        async def nonstored(*args, **kwargs):
            if failure == "duplicate_without_id":
                return {"stored": False, "reason": "duplicate"}
            return {"stored": False, "reason": "session_near_duplicate", "atom_id": "existing-atom"}
        monkeypatch.setattr(env.saga, "store", nonstored)
    elif failure == "hash":
        record = env.record()
        record["content"] = "changed"
        path.write_text(json.dumps(record) + "\n")
    suffix = ":   " if failure == "empty_edit" else ""
    result = await env.agent.run_turn(env.event(f"approve {proposal_id}{suffix}"))
    expected = {"exception": "embedding unavailable", "nonstored": "session_near_duplicate",
                "duplicate_without_id": "duplicate", "hash": "hash mismatch",
                "empty_edit": "empty edit"}[failure]
    assert expected in result.output
    assert "Stored" not in result.output
    assert env.record()["status"] == "pending"
    assert env.atoms() == []
    assert [entry.approval_id for entry in approval_requests.pending("discord-1")] == [proposal_id]
    assert proposal_id not in approval_requests._RECENT
    path.write_text(original)
    monkeypatch.setattr(env.saga, "store", store)
    retry = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert "Stored" in retry.output
    assert env.record()["status"] == "approved"
    assert len(env.atoms()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"author": "discord-98"}, {"source": "api"}, {"author": "discord-97"},
])
async def test_completion_rechecks_authenticated_operator(setup, overrides):
    env = setup
    proposal_id = env.queue()
    event = env.event(f"approve {proposal_id}")
    resolution = approval_requests.resolve(event, env.resolver)
    event = replace(event, **overrides, extra=dict(event.extra))
    assert await memory_proposals.complete_reply(env.home, event, resolution, env.resolver) is None
    assert env.atoms() == []
    assert env.record()["status"] == "pending"


@pytest.mark.asyncio
async def test_completion_rechecks_durable_expiry_after_registry_selection(setup):
    env = setup
    proposal_id = env.queue()
    event = env.event(f"approve {proposal_id}")
    resolution = approval_requests.resolve(event, env.resolver)
    record = env.record()
    record["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    memory_proposals.proposal_path(env.home).write_text(json.dumps(record) + "\n")
    notice = await memory_proposals.complete_reply(env.home, event, resolution, env.resolver)
    assert notice == f"expired proposal {proposal_id}"
    assert env.record()["status"] == "expired"
    assert env.atoms() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "declined", "expired"])
async def test_unknown_id_lookup_does_not_disclose_other_channel_status(setup, status):
    env = setup
    proposal_id = env.queue()
    memory_proposals._update(env.home, proposal_id, status)
    approval_requests._PENDING.clear()
    approval_requests._RECENT.clear()
    event = env.event(f"approve {proposal_id}", channel_id="discord-2")
    resolution = approval_requests.resolve(event, env.resolver)
    notice = await memory_proposals.complete_reply(env.home, event, resolution, env.resolver)
    assert notice == f"no pending request {proposal_id}"
    assert env.atoms() == []


@pytest.mark.asyncio
async def test_completion_rejects_action_for_another_home(setup, tmp_path):
    env = setup
    proposal_id = env.queue()
    event = env.event(f"approve {proposal_id}")
    resolution = approval_requests.resolve(event, env.resolver)
    other_home = tmp_path / "other"
    memory_proposals.configure_approvals(other_home, "discord-1", env.saga)
    event.extra["_memory_proposal_action"] = (other_home, proposal_id, "approve", None)
    notice = await memory_proposals.complete_reply(env.home, event, resolution, env.resolver)
    assert notice == f"no pending request {proposal_id}"
    assert env.atoms() == []
    assert env.record()["status"] == "pending"


def test_update_failure_before_replace_preserves_store_and_stable_lock(setup, monkeypatch):
    env = setup
    proposal_id = env.queue()
    path = memory_proposals.proposal_path(env.home)
    lock = path.with_suffix(".lock")
    original = path.read_bytes()
    lock_inode = lock.stat().st_ino
    original_replace = memory_proposals.os.replace
    def fail_replace(source, target):
        assert path.read_bytes() == original
        assert memory_proposals.Path(source).read_text() != original.decode()
        raise OSError("simulated publication failure")
    monkeypatch.setattr(memory_proposals.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failure"):
        memory_proposals._update(env.home, proposal_id, "declined")
    assert path.read_bytes() == original
    assert list(path.parent.glob(".memory-proposals-*")) == []
    monkeypatch.setattr(memory_proposals.os, "replace", original_replace)
    memory_proposals._update(env.home, proposal_id, "declined")
    assert env.record()["status"] == "declined"
    assert lock.stat().st_ino == lock_inode


def test_queue_read_and_update_share_sidecar_lock(setup, monkeypatch):
    env = setup
    operations = []
    original = memory_proposals.fcntl.flock
    def observed(fd, mode):
        import os
        lock = memory_proposals.proposal_path(env.home).with_suffix(".lock")
        assert os.fstat(fd).st_ino == lock.stat().st_ino
        operations.append(mode)
        return original(fd, mode)
    monkeypatch.setattr(memory_proposals.fcntl, "flock", observed)
    proposal_id = env.queue()
    assert memory_proposals.fcntl.LOCK_EX in operations
    operations.clear()
    assert memory_proposals._records(env.home)[0]["id"] == proposal_id
    assert operations == [memory_proposals.fcntl.LOCK_SH]
    operations.clear()
    memory_proposals._update(env.home, proposal_id, "declined")
    assert operations == [memory_proposals.fcntl.LOCK_EX]


def test_restore_uncompleted_rejects_resolution_from_another_channel(setup):
    env = setup
    proposal_id = env.queue()
    resolution = approval_requests.resolve(env.event(f"approve {proposal_id}"), env.resolver)
    recent = approval_requests._RECENT[proposal_id]
    stale_entry = replace(resolution.entry, channel_id="discord-2")
    approval_requests.restore_uncompleted(stale_entry)
    assert proposal_id not in approval_requests._PENDING
    assert approval_requests._RECENT[proposal_id] == recent


def test_restore_uncompleted_rejects_passed_registry_deadline(setup, monkeypatch):
    env = setup
    proposal_id = env.queue()
    resolution = approval_requests.resolve(env.event(f"approve {proposal_id}"), env.resolver)
    recent = approval_requests._RECENT[proposal_id]
    # Inspect the raw map: pending() would sweep expiry and mask an invalid restore.
    monkeypatch.setattr(approval_requests, "time", SimpleNamespace(
        monotonic=lambda: resolution.entry.expires_at,
    ))
    approval_requests.restore_uncompleted(resolution.entry)
    assert proposal_id not in approval_requests._PENDING
    assert approval_requests._RECENT[proposal_id] == recent


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["preexisting", "update_failed"])
async def test_exact_duplicate_approval_completes_with_existing_atom(setup, monkeypatch, path):
    env = setup
    proposal_id = env.queue()
    original_update = memory_proposals._update
    if path == "preexisting":
        stored = await env.saga.store(env.record()["content"], stream="semantic")
        assert stored["stored"] is True
        atom_id = stored["atom_id"]
    else:
        def failed_update(*args, **kwargs):
            raise OSError("decision publication failed")
        monkeypatch.setattr(memory_proposals, "_update", failed_update)
        with pytest.raises(OSError, match="decision publication failed"):
            await env.agent.run_turn(env.event(f"approve {proposal_id}"))
        assert env.record()["status"] == "pending"
        assert len(env.atoms()) == 1
        atom_id = env.atoms()[0][0]
        assert [entry.approval_id for entry in approval_requests.pending("discord-1")] == [proposal_id]
        monkeypatch.setattr(memory_proposals, "_update", original_update)

    result = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert result.output == f"Already in memory as {atom_id}"
    assert env.notices[-1] == result.output
    assert "Stored" not in result.output
    record = env.record()
    assert record["status"] == "approved"
    assert record["atom_id"] == atom_id
    assert record["deduplicated"] is True
    assert len(env.atoms()) == 1
    assert approval_requests.pending("discord-1") == ()
    again = await env.agent.run_turn(env.event(f"approve {proposal_id}"))
    assert "already decided" in again.output
    assert len(env.atoms()) == 1
