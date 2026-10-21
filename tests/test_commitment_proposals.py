"""Queued closures cross the trust boundary only at authenticated ingress."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mimir import _context, access_control as ac, commitment_proposals as cp
from mimir.agent import Agent
from mimir.commitments import CommitmentRecord, CommitmentsStore, make_commitment_id
from mimir.config import Config
from mimir.dispatcher import Dispatcher
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, InformationFlowLabels, SourceLabel, TurnContext
from mimir.tools import registry
from mimir.tools.refusals import ToolPolicyRefusal


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from mimir.event_logger import init_logger
    init_logger(tmp_path / "events.jsonl", "test-closures")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    (tmp_path / "state").mkdir()
    (tmp_path / "state/identities.yaml").write_text('''people:
  - canonical: operator
    aliases: [discord-99]
    access: {roles: [admin]}
  - canonical: viewer
    aliases: [discord-98]
    access: {roles: [user]}
''')
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    store = CommitmentsStore(path=tmp_path / ".mimir/commitments.jsonl")
    monkeypatch.setitem(registry._STATE, "commitments_store", store)
    monkeypatch.setitem(cp._BACKENDS, tmp_path, ("discord-1", store))
    service = ac.build_scheduled_tick_service_principal("commitments-review", tmp_path)
    labels = InformationFlowLabels(sources=(SourceLabel.from_record(dict(
        principal=None, domain="public", resource_id="evidence:42", bridge_instance=None,
        sensitivity="public", source_kind="channel", integrity="untrusted",
        integrity_effect="active_ingest",
    )),))
    auth = ac.create_auth_context(AgentEvent(
        trigger="scheduled_tick", channel_id="scheduler:commitments-review", source="scheduler",
        service_principal=service.canonical, service_authority=service,
    ), enforce=True, ifc_labels=labels)
    turn = TurnContext(turn_id="review-turn", session_id="review", trigger="scheduled_tick",
                       channel_id="scheduler:commitments-review", started_at=0, auth_context=auth)
    monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    runtime = SimpleNamespace(context=auth)
    notices = []
    cfg = replace(Config.from_env(), home=tmp_path, operator_alert_channel="discord-1")
    dispatcher = Dispatcher(cfg, resolver=resolver)

    async def notice(channel, text):
        notices.append(text)
        return SimpleNamespace(sent=True)

    dispatcher.set_notice_sender(notice)
    agent = Agent.__new__(Agent)
    agent._config, agent._identity_resolver = cfg, resolver
    agent._dispatcher, agent._channels = dispatcher, None

    async def add(**kw):
        return await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="discord-1",
                                               text="Ship fix", owner_principal="operator", **kw))

    async def queue(record, action="complete", evidence="PR #42 merged"):
        result = await registry.commitment_propose_closure.coroutine(
            commitment_id=record.id, action=action, evidence=evidence, runtime=runtime)
        return re.search(r"cp-[a-z2-7]{4}", result).group()

    def event(text, **kw):
        values = dict(trigger="user_message", channel_id="discord-1", content=text,
                      author="discord-99", source="discord", source_id="message-99")
        values.update(kw)
        return AgentEvent(**values)

    messages = []

    async def send(channel, text, *, final):
        messages.append(text)
        return SimpleNamespace(sent=True)

    async def digest():
        cp.seal_review(tmp_path, turn.turn_id)
        assert await cp.post_review_digest(tmp_path, "discord-1", send)
        return re.search(r"cb-[a-z2-7]{4}", messages[-1]).group()

    return SimpleNamespace(home=tmp_path, resolver=resolver, store=store, auth=auth, runtime=runtime,
                           turn=turn, agent=agent, dispatcher=dispatcher, notices=notices,
                           add=add, queue=queue, event=event, digest=digest, messages=messages)


@pytest.mark.asyncio
async def test_tainted_scheduled_review_to_authenticated_operator_reply(env, monkeypatch):
    rec = await env.add()
    listed = await registry.commitment_list.coroutine(due_within_days=0, runtime=env.runtime)
    assert rec.id in listed and '"sensitivity": "routine"' in listed
    assert ac.can_review_commitments(env.auth)
    gate = ac.ToolRegistry().authorize_tool(
        "commitment_propose_closure", env.auth, enforce=True, arguments={
            "commitment_id": rec.id, "action": "complete", "evidence": "merged"},
        ifc_labels=env.auth.ifc_labels,
    )
    assert gate.allowed, gate
    pid = await env.queue(rec)
    assert env.store.current_state()[rec.id].status == "pending"  # unapproved is inert
    with pytest.raises(ToolPolicyRefusal, match="durable memory mutation refused"):
        await registry.commitment_complete.coroutine(commitment_id=rec.id, runtime=env.runtime)
    batch = await env.digest()
    assert pid in env.messages[0] and "PR #42 merged" in env.messages[0]

    def no_model(*a, **kw):
        pytest.fail("approval must not enter model/turn setup")
    monkeypatch.setattr("mimir.agent._initialize_ifc_labels", no_model)
    result = await env.agent.run_turn(env.event(f"approve {batch}"))
    assert result.kind == "commitment_proposal_approval"
    assert f"Applied {pid}" in result.output
    assert env.store.current_state()[rec.id].status == "completed"
    events = [json.loads(line) for line in env.store.path.read_text().splitlines()]
    assert events[-1]["approved_by"] == "operator" and events[-1]["proposal_id"] == pid
    again = await cp.handle_reply(env.home, env.event(f"approve {pid}"), env.resolver)
    assert "Skipped" in again
    assert len(env.store.path.read_text().splitlines()) == len(events)


@pytest.mark.asyncio
async def test_dispatcher_consumes_approval_before_inflight_injection(env):
    rec = await env.add()
    pid = await env.queue(rec)
    # Busy or not, dispatch handles this before queue/injection/model execution.
    env.dispatcher._in_flight.add("discord-1")
    assert await env.dispatcher.enqueue(env.event(f"approve {pid}"))
    assert env.store.current_state()[rec.id].status == "completed"
    assert any(f"Applied {pid}" in notice for notice in env.notices)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["terminal", "status", "owner", "care", "text"])
async def test_changed_or_terminal_commitment_skipped_and_reported(env, change):
    rec = await env.add()
    pid = await env.queue(rec)
    if change == "terminal":
        await env.store.complete(rec.id, actor_principal="operator")
    elif change == "status":
        await env.store.snooze(rec.id, until_unix=9999999999, actor_principal="operator")
    else:
        # An independently changed current record (migration/ownership handoff).
        rows = [json.loads(line) for line in env.store.path.read_text().splitlines()]
        rows[0]["record"][{"owner": "owner_principal", "care": "sensitivity", "text": "text"}[change]] = {
            "owner": "other", "care": "care", "text": "different obligation"}[change]
        env.store.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    before = env.store.path.read_bytes()
    notice = await cp.handle_reply(env.home, env.event(f"approve {pid}"), env.resolver)
    assert f"Skipped {pid}" in notice
    assert env.store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_bulk_completes_only_explicit_id_required_for_dismissal(env):
    first, second = await env.add(), await env.add()
    done, dismiss = await env.queue(first), await env.queue(second, action="dismiss")
    batch = await env.digest()
    notice = await cp.handle_reply(env.home, env.event(f"approve {batch}"), env.resolver)
    assert f"Applied {done}" in notice and f"Skipped {dismiss}" in notice
    assert env.store.current_state()[second.id].status == "pending"
    assert "Applied" in await cp.handle_reply(env.home, env.event(f"approve {dismiss}"), env.resolver)
    assert env.store.current_state()[second.id].status == "dismissed"


@pytest.mark.asyncio
async def test_individual_multi_approval_decline_and_batch_snapshot(env):
    a, b, c = await env.add(), await env.add(), await env.add()
    pa, pb = await env.queue(a), await env.queue(b)
    batch = await env.digest()
    env.turn.turn_id = "next-review"
    pc = await env.queue(c)
    await cp.handle_reply(env.home, env.event(f"approve {batch}"), env.resolver)
    assert env.store.current_state()[c.id].status == "pending"
    notice = await cp.handle_reply(env.home, env.event(f"decline {pc}"), env.resolver)
    assert f"Declined {pc}" in notice and env.store.current_state()[c.id].status == "pending"
    d, e = await env.add(), await env.add()
    pd, pe = await env.queue(d), await env.queue(e, action="dismiss")
    notice = await cp.handle_reply(env.home, env.event(f"approve {pd} {pe}"), env.resolver)
    assert f"Applied {pd}" in notice and f"Applied {pe}" in notice


@pytest.mark.asyncio
async def test_care_cannot_be_proposed(env):
    rec = await env.add(sensitivity="care")
    with pytest.raises(ToolPolicyRefusal, match="CARE"):
        await env.queue(rec)
    assert env.store.current_state()[rec.id].status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"author": "discord-98"}, {"author": None}, {"source": "api"}, {"source": "stdin"},
    {"trigger": "poller"}, {"channel_id": "discord-2"},
    {"extra": {"_mimir_event_ingress": "http_api"}},
])
async def test_forged_or_wrong_channel_approval_applies_nothing(env, override):
    from mimir.http_ingress import HTTP_EVENT_INGRESS_EXTRA_KEY, HTTP_EVENT_INGRESS_EXTRA_VALUE
    if "extra" in override:
        override = {"extra": {HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE}}
    rec = await env.add()
    pid = await env.queue(rec)
    notice = await cp.handle_reply(env.home, env.event(f"approve {pid}", **override), env.resolver)
    assert "refused" in notice
    assert env.store.current_state()[rec.id].status == "pending"


@pytest.mark.asyncio
async def test_expiry_prevents_apply(env):
    rec = await env.add()
    pid = await env.queue(rec)
    with cp._db(env.home) as db:
        db.execute("UPDATE proposals SET expires=0 WHERE id=?", (pid,))
    notice = await cp.handle_reply(env.home, env.event(f"approve {pid}"), env.resolver)
    assert f"Skipped {pid}: expired" in notice
    assert env.store.current_state()[rec.id].status == "pending"


@pytest.mark.asyncio
async def test_redaction_bounds_and_failed_digest_retry(env):
    rec = await env.add()
    fixture_value = "ghp_" + "a" * 36
    pid = await env.queue(rec, evidence="@everyone " + fixture_value + " x" * 1000)
    with cp._db(env.home) as db:
        payload = json.loads(db.execute("SELECT payload FROM proposals WHERE id=?", (pid,)).fetchone()[0])
    assert len(payload["evidence"]) <= 1000 and fixture_value not in payload["evidence"]
    assert payload["turn_id"] == "review-turn" and payload["current_status"] == "pending"

    cp.seal_review(env.home, env.turn.turn_id)
    async def refused(*a, **kw):
        return SimpleNamespace(sent=False)
    with pytest.raises(ValueError, match="delivery refused"):
        await cp.post_review_digest(env.home, "discord-1", refused)
    await env.digest()
    assert fixture_value not in env.messages[0] and "@everyone" not in env.messages[0]


@pytest.mark.asyncio
async def test_review_grant_is_read_and_propose_only_and_not_shared(env):
    rec = await env.add()
    assert rec.id in await registry.commitment_list.coroutine(due_within_days=0, runtime=env.runtime)
    other_service = ac.build_scheduled_tick_service_principal("issues-audit", env.home)
    other = replace(env.auth, service_authority=other_service)
    assert not ac.can_review_commitments(other)
    assert rec.id not in await registry.commitment_list.coroutine(
        due_within_days=0, runtime=SimpleNamespace(context=other))
    env.turn.auth_context = other
    with pytest.raises(ToolPolicyRefusal, match="scheduled review authority"):
        await registry.commitment_propose_closure.coroutine(
            commitment_id=rec.id, action="complete", evidence="merged", runtime=SimpleNamespace(context=other))
    assert not await env.store.complete(rec.id, actor_principal="service:scheduler")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["trigger", "scheduler_channel"])
@pytest.mark.parametrize("consumer", ["proposal", "tool_list", "store_list"])
async def test_review_identity_required_even_with_both_capabilities(env, identity, consumer):
    rec = await env.add()
    # Positive control: the real job can read this operator-owned record.
    assert rec.id in await registry.commitment_list.coroutine(
        due_within_days=0, runtime=env.runtime)
    review_service = ac.get_trusted_service_from_auth_context(env.auth)
    assert review_service is not None
    # Construct a trusted service with the same grants through the production
    # builder. Capability absence must not mask the exact-job identity guard.
    trigger = "shell_job_complete" if identity == "trigger" else "scheduled_tick"
    channel = ("scheduler:issues-audit" if identity == "scheduler_channel"
               else "scheduler:commitments-review")
    service = ac.build_trigger_service_principal(
        canonical=review_service.canonical,
        trigger=trigger,
        profile=review_service.authority_profile,
        tier=review_service.capability_tier,
        capabilities=review_service.capabilities,
        channel_memory_directory=channel,
        creation_path="test:misrouted-commitments-review-grant",
    )
    auth = ac.create_auth_context(AgentEvent(
        trigger=trigger, channel_id=channel, source="scheduler",
        service_principal=service.canonical, service_authority=service,
    ), enforce=True, ifc_labels=env.auth.ifc_labels)
    assert ac.get_trusted_service_from_auth_context(auth) is service
    for operation in ("commitment_list", "commitment_propose_closure"):
        assert ac.service_can_invoke_operation(service, operation)
    env.turn.auth_context = auth
    env.turn.trigger, env.turn.channel_id = trigger, channel
    runtime = SimpleNamespace(context=auth)
    before = env.store.path.read_bytes()
    if consumer == "proposal":
        with pytest.raises(ToolPolicyRefusal, match="scheduled review authority"):
            await registry.commitment_propose_closure.coroutine(
                commitment_id=rec.id, action="complete", evidence="merged", runtime=runtime)
        assert not cp.proposal_path(env.home).exists()
    elif consumer == "tool_list":
        assert rec.id not in await registry.commitment_list.coroutine(
            due_within_days=0, runtime=runtime)
    else:
        assert rec.id not in {item.id for item in env.store.list(auth_context=auth)}
    assert env.store.path.read_bytes() == before
    assert not ac.can_review_commitments(auth)


@pytest.mark.asyncio
async def test_sealed_review_has_one_digest_over_twenty_items_and_recovers(env):
    import asyncio
    ids = [await env.queue(await env.add()) for _ in range(21)]
    async def no_send(*args, **kwargs):
        pytest.fail("mid-review digest must not deliver")
    assert not await cp.post_review_digest(env.home, "discord-1", no_send)
    cp.seal_review(env.home, env.turn.turn_id)
    with pytest.raises(ToolPolicyRefusal, match="sealed"):
        await env.queue(await env.add())
    batch, _ = cp._prepare_digest(env.home)  # process dies before delivery
    assert cp._prepare_digest(env.home) is None  # lease prevents concurrent send
    with cp._db(env.home) as db:
        db.execute("UPDATE batches SET retry_at=0")
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await cp.post_review_digest(env.home, "discord-1", cancelled)
    assert await env.digest() == batch
    assert all(pid in env.messages[-1] for pid in ids)
    assert len(env.messages) == 1
    assert not await cp.post_review_digest(env.home, "discord-1", no_send)


@pytest.mark.asyncio
async def test_review_evidence_is_discoverable_through_real_turn_reader(env, monkeypatch):
    from mimir.tools import extra
    rec = await env.add(source_turn_id="source-proof", saga_session_id="source-session")
    log = env.home / "turns.jsonl"
    log.write_text(json.dumps({"turn_id": "source-proof", "output": "PR #42 merged", "input": "private prompt"}) + "\n")
    monkeypatch.setitem(extra._TURN_STATE, "turns_log_path", log)
    listed = json.loads(await registry.commitment_list.coroutine(due_within_days=0, runtime=env.runtime))
    assert listed[0]["saga_session_id"] == "source-session"
    assert ac.ToolRegistry().authorize_tool("get_turn", env.auth, enforce=True,
        arguments={"turn_id": listed[0]["source_turn_id"]}, ifc_labels=env.auth.ifc_labels).allowed
    proof = extra.get_turn.func(turn_id=listed[0]["source_turn_id"])
    assert "PR #42 merged" in proof and "private prompt" not in proof
    assert await env.queue(rec, evidence=json.loads(proof)["output"])


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["terminal", "care", "owner"])
async def test_store_guards_are_not_masked_by_fingerprint(env, guard):
    from mimir.commitments.store import commitment_fingerprint
    rec = await env.add(sensitivity="care" if guard == "care" else "routine")
    if guard == "terminal":
        await env.store.complete(rec.id, actor_principal="operator")
    rec = env.store.current_state()[rec.id]
    before = env.store.path.read_bytes()
    assert not env.store.apply_proposed_closure_sync(rec.id, action="complete",
        expected_fingerprint=commitment_fingerprint(rec),
        expected_owner="other" if guard == "owner" else rec.owner_principal,
        actor_principal="operator", approval_message_id="approved", proposal_id="cp-test")
    assert env.store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_queue_store_is_protected_including_alias_and_sidecars(env):
    from mimir.memory_proposals import is_protected_model_path
    await env.queue(await env.add())
    path = cp.proposal_path(env.home)
    alias = env.home / "alias.db"
    alias.hardlink_to(path)
    for candidate in (path, alias, path.with_name(path.name + "-journal")):
        assert is_protected_model_path(candidate)
        gate = ac.ToolRegistry().authorize_tool(
            "write_file", env.auth, enforce=True, arguments={"file_path": str(candidate)},
            target_channel=str(candidate), ifc_labels=env.auth.ifc_labels)
        assert not gate.allowed and gate.reason == "protected_memory_proposal_path"
