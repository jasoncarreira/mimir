from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime

from mimir import _context
from mimir.access_control import (
    CapabilityTier, SAGA_TAINT_REFUSAL, build_trigger_service_principal,
    create_auth_context, get_tool_registry, saga_mutation_taint_refusal,
)
from mimir.memory_proposals import proposal_path
from mimir.models import AgentEvent, InformationFlowLabels, SourceLabel, TurnContext
from mimir.tools.memory_propose import memory_propose


@pytest.fixture
def proposal_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    service = build_trigger_service_principal(
        canonical="poller:papers", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("memory_propose", "read_file", "write_file", "edit_file", "ls"),
        roots=(tmp_path / "state" / "pollers" / "papers",), creation_path="test",
    )
    source = SourceLabel.from_record(dict(
        principal=None, domain="public", resource_id="feed:item:42",
        bridge_instance=None, sensitivity="public", source_kind="channel",
        integrity="untrusted", integrity_effect="active_ingest",
    ))
    labels = InformationFlowLabels(sources=(source,))
    auth = create_auth_context(AgentEvent(
        trigger="poller", channel_id=service.canonical, source="poller",
        source_id="feed:item:42", service_principal=service.canonical,
        service_authority=service,
    ), enforce=True, ifc_labels=labels)
    turn = TurnContext(turn_id="papers-turn-42", session_id=service.canonical,
                       trigger="poller", channel_id=service.canonical,
                       started_at=0, auth_context=auth)
    monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    runtime = ToolRuntime(state={}, context=auth, config={}, stream_writer=lambda _: None,
                          tool_call_id="proposal-test", store=None)

    def call(content="A useful fact", stream="semantic", rationale="It matters"):
        return asyncio.run(memory_propose.coroutine(
            content=content, stream=stream, rationale=rationale, runtime=runtime,
        ))

    return SimpleNamespace(home=tmp_path, auth=auth, turn=turn, call=call, labels=labels)


def _records(home):
    path = proposal_path(home)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_tainted_proposal_records_every_field_without_mutating_saga(proposal_turn, monkeypatch):
    env = proposal_turn
    from mimir.saga.client import SagaStore
    from mimir.tools.memory import _MEMORY_STATE

    saga_db = env.home / "saga.db"
    saga = SagaStore(db_path=saga_db, embedding_dim=4)
    monkeypatch.setitem(_MEMORY_STATE, "client", saga)
    conn = saga._ensure_conn()
    before_atoms = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    before_tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    before = saga_db.read_bytes()
    content = "Fact with newline\nkept verbatim"
    result = env.call(content=content)
    record, = _records(env.home)
    assert re.search(r"Proposed memory mp-[0-9a-f]{8} queued", result)
    assert "nothing is stored yet" in result and "do not notify the operator" in result
    assert record == {
        "id": record["id"], "content": content, "stream": "semantic",
        "rationale": "It matters", "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "proposed_by": env.auth.canonical_principal or env.auth.principal,
        "turn_id": "papers-turn-42", "origin_trigger": "poller",
        "origin_ref": env.auth.origin_ref,
        "ifc_sources": [{
            "source_kind": env.labels.sources[0].source_kind,
            "integrity": "untrusted", "integrity_effect": "active_ingest",
            "resource_id": "feed:item:42",
        }],
        "created_at": record["created_at"], "expires_at": record["expires_at"],
        "status": "pending",
    }
    assert record["id"] in result
    assert datetime.fromisoformat(record["expires_at"]) - datetime.fromisoformat(record["created_at"]) == timedelta(days=7)
    assert saga_db.read_bytes() == before
    assert conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == before_atoms
    assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == before_tables


def test_clean_turn_refuses_directs_to_memory_store(proposal_turn):
    env = proposal_turn
    clean_labels = InformationFlowLabels(sources=(
        SourceLabel.from_record(dict(
            principal=None, domain="public", resource_id="trusted:item",
            bridge_instance=None, sensitivity="public", source_kind="channel",
            integrity="trusted", integrity_effect="active_ingest",
        )),
    ))
    clean = create_auth_context(AgentEvent(
        trigger="poller", channel_id=env.auth.channel_id, source="poller",
        source_id="trusted:item", service_principal=env.auth.channel_id,
        service_authority=env.auth.service_authority,
    ), enforce=True, ifc_labels=clean_labels)
    env.turn.auth_context = clean
    runtime = ToolRuntime(state={}, context=clean, config={}, stream_writer=lambda _: None,
                          tool_call_id="clean-test", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "this turn can store directly; use memory_store" in result
    assert _records(env.home) == []


@pytest.mark.parametrize("content,reason", [
    ("ghp_" + "a" * 36, "credential"),
    ("x" * 501, "500"),
])
def test_invalid_content_writes_nothing(proposal_turn, content, reason):
    assert reason in proposal_turn.call(content=content)
    assert _records(proposal_turn.home) == []


def test_duplicate_pending_proposal_is_refused(proposal_turn):
    env = proposal_turn
    assert "queued" in env.call()
    assert "duplicate" in env.call(rationale="different why")
    assert len(_records(env.home)) == 1


def test_21st_pending_per_principal_is_refused(proposal_turn):
    env = proposal_turn
    for index in range(20):
        assert "queued" in env.call(content=f"Fact {index}")
    assert "20 pending proposals" in env.call(content="Fact 20")
    assert len(_records(env.home)) == 20


def test_file_tools_cannot_access_proposal_path(proposal_turn):
    env = proposal_turn
    env.call()
    path = str(proposal_path(env.home))
    registry = get_tool_registry()
    human = create_auth_context(AgentEvent(
        trigger="user_message", channel_id="web-operator", source="web",
        source_id="message-1", author="viewer",
    ), enforce=True, ifc_labels=env.labels)
    for auth in (env.auth, replace(env.auth, roles=("admin",)), human):
        for tool in ("read_file", "write_file", "edit_file", "ls"):
            args = {"file_path": path} if tool == "read_file" else {"path": path}
            if tool in {"write_file", "edit_file"}:
                args["file_path"] = path
            decision = registry.authorize_tool(tool, auth, enforce=True,
                                                target_channel=path, arguments=args,
                                                ifc_labels=env.labels)
            assert not decision.allowed, (tool, decision)
            assert decision.reason == "protected_memory_proposal_path"
        assert not registry.authorize_tool(
            "ls", auth, enforce=True, arguments={"path": str(proposal_path(env.home).parent)},
        ).allowed


def test_taint_refusal_hint_only_when_tool_granted(proposal_turn):
    env = proposal_turn
    assert "memory_propose" in saga_mutation_taint_refusal(env.auth)
    service = build_trigger_service_principal(
        canonical="poller:other", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("memory_store",), roots=(env.home / "state",), creation_path="test",
    )
    other = create_auth_context(AgentEvent(
        trigger="poller", channel_id=service.canonical, source="poller",
        source_id="other", service_principal=service.canonical,
        service_authority=service,
    ), enforce=True, ifc_labels=env.labels)
    assert saga_mutation_taint_refusal(other) == SAGA_TAINT_REFUSAL


def test_missing_ifc_sources_cannot_queue(proposal_turn):
    env = proposal_turn
    from mimir.models import InformationFlowState

    empty = replace(env.auth, ifc_labels=InformationFlowLabels(),
                    ifc_state=InformationFlowState(labels=InformationFlowLabels()))
    env.turn.auth_context = empty
    runtime = ToolRuntime(state={}, context=empty, config={}, stream_writer=lambda _: None,
                          tool_call_id="missing-ifc", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "missing IFC source provenance" in result
    assert _records(env.home) == []


def test_authority_and_turn_id_are_required(proposal_turn):
    env = proposal_turn

    service = build_trigger_service_principal(
        canonical="poller:other", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE, capabilities=("memory_store",),
        roots=(env.home / "state",), creation_path="test",
    )
    unauthorized = create_auth_context(AgentEvent(
        trigger="poller", channel_id=service.canonical, source="poller",
        source_id="other", service_principal=service.canonical,
        service_authority=service,
    ), enforce=True, ifc_labels=env.labels)
    env.turn.auth_context = unauthorized
    runtime = ToolRuntime(state={}, context=unauthorized, config={}, stream_writer=lambda _: None,
                          tool_call_id="unauthorized", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "write access denied" in result
    # The authorized context is not the context of this live turn.
    runtime = ToolRuntime(state={}, context=env.auth, config={}, stream_writer=lambda _: None,
                          tool_call_id="wrong-turn", store=None)
    result = asyncio.run(memory_propose.coroutine(
        content="fact", stream="semantic", rationale="why", runtime=runtime,
    ))
    assert "missing authoritative turn id" in result
    assert _records(env.home) == []
