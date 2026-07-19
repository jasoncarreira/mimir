"""Server-owned SAGA write provenance and concurrent-call isolation."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain.tools import ToolRuntime

from mimir.access_control import CapabilityTier, build_trigger_service_principal, create_auth_context
from mimir.identities import IdentityResolver
from mimir.models import (
    AgentEvent, AuthContext, InformationFlowLabels, SessionACL, SourceLabel,
)
from mimir.saga.client import SagaStore
from mimir.tools import saga_ops
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.store import memory_store


class _WriteStore:
    def __init__(self) -> None:
        self.atom_calls: list[dict[str, Any]] = []
        self.session_calls: list[dict[str, Any]] = []

    async def store(self, content: str, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        self.atom_calls.append({"content": content, **kwargs})
        return {"atom_id": f"atom-{len(self.atom_calls)}", "stored": True}

    async def end_session(self, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        self.session_calls.append(kwargs)
        return {"session_summary_written": True}


@pytest.fixture
def write_store() -> _WriteStore:
    store = _WriteStore()
    previous = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = store
    yield store
    _MEMORY_STATE["client"] = previous


def _runtime(context: AuthContext, call_id: str) -> ToolRuntime[AuthContext]:
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id=call_id, store=None,
    )


def _resolver(tmp_path: Path) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    path = state / "identities.yaml"
    path.write_text(
        """people:
  - canonical: alice
    aliases: [discord-alice]
    access: {roles: [user, admin]}
  - canonical: bob
    aliases: [slack-bob]
    access: {roles: [user, admin]}
  - canonical: regular
    aliases: [discord-regular]
    access: {roles: [user]}
"""
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def _user_context(tmp_path: Path, author: str, channel: str) -> AuthContext:
    return create_auth_context(
        AgentEvent(
            trigger="user_message", channel_id=channel, author=author,
            source="discord" if channel.startswith("discord") else "slack",
        ),
        _resolver(tmp_path),
        enforce=True,
    )


def _service_context(trigger: str, channel: str) -> AuthContext:
    principals = {
        "scheduled_tick": "scheduler",
        "poller": "poller",
        "saga_session_end": "synthesis",
        "upgrade": "system",
    }
    return create_auth_context(
        AgentEvent(
            trigger=trigger,
            channel_id=channel,
            service_principal=principals[trigger],
        ),
        enforce=True,
    )


@pytest.mark.asyncio
async def test_memory_store_regular_user_is_rejected(
    tmp_path: Path, write_store: _WriteStore,
) -> None:
    context = _user_context(tmp_path, "discord-regular", "discord-private")
    out = await memory_store.ainvoke({
        "content": "not allowed", "stream": "semantic",
        "runtime": _runtime(context, "regular-store"),
    })
    assert "write access denied" in out
    assert write_store.atom_calls == []


@pytest.mark.asyncio
async def test_concurrent_memory_writes_keep_runtime_owned_provenance(
    tmp_path: Path, write_store: _WriteStore,
) -> None:
    alice = _user_context(tmp_path, "discord-alice", "discord-a")
    bob = _user_context(tmp_path, "slack-bob", "slack-b")

    await asyncio.gather(
        memory_store.ainvoke({
            "content": "alice fact", "stream": "semantic",
            "session_id": "model-label-a",
            "runtime": _runtime(alice, "alice-store"),
        }),
        memory_store.ainvoke({
            "content": "bob fact", "stream": "semantic",
            "session_id": "model-label-b",
            "runtime": _runtime(bob, "bob-store"),
        }),
    )

    by_content = {call["content"]: call for call in write_store.atom_calls}
    assert by_content["alice fact"]["owner_principal"] == "alice"
    assert by_content["alice fact"]["origin_channel"] == "discord-a"
    assert by_content["alice fact"]["session_id"] == "model-label-a"
    assert by_content["bob fact"]["owner_principal"] == "bob"
    assert by_content["bob fact"]["origin_channel"] == "slack-b"
    assert by_content["bob fact"]["session_id"] == "model-label-b"


@pytest.mark.parametrize(
    "trigger",
    ["scheduled_tick", "poller", "upgrade"],
)
@pytest.mark.asyncio
async def test_service_without_memory_store_capability_is_denied(
    trigger: str, write_store: _WriteStore,
) -> None:
    context = _service_context(trigger, f"{trigger}:owned")
    out = await memory_store.ainvoke({
        "content": trigger, "stream": "episodic",
        "runtime": _runtime(context, f"{trigger}-store"),
    })

    assert "write access denied" in out
    assert write_store.atom_calls == []


@pytest.mark.asyncio
async def test_synthesis_memory_store_preserves_service_provenance(
    write_store: _WriteStore,
) -> None:
    trigger = "saga_session_end"
    context = _service_context(trigger, f"{trigger}:owned")
    out = await memory_store.ainvoke({
        "content": trigger, "stream": "episodic",
        "runtime": _runtime(context, f"{trigger}-store"),
    })

    assert "stored" in out
    call = write_store.atom_calls[-1]
    assert call["owner_principal"] == "service:synthesis"
    assert call["origin_channel"] == f"{trigger}:owned"
    assert call["visibility"] == "service"


@pytest.mark.asyncio
async def test_research_poller_write_stamps_frozen_integrity_and_origin(
    write_store: _WriteStore,
) -> None:
    authority = build_trigger_service_principal(
        canonical="poller:hn-ai",
        trigger="poller",
        profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("memory_store", "saga_feedback", "saga_mark_contributions"),
        creation_path="test",
    )
    principal = "service:poller:hn-ai"
    labels = InformationFlowLabels().with_source(SourceLabel(
        principal=principal, domain="channel", resource_id="poller:hn-ai",
        bridge_instance="poller", sensitivity="internal",
        authorized_principals=frozenset({principal}), source_kind="service",
        integrity="untrusted", integrity_effect="active_ingest",
    ))
    context = create_auth_context(AgentEvent(
        trigger="poller", channel_id="poller:hn-ai", source="poller",
        source_id="poller:hn-ai:123:batch:0", service_principal=authority.canonical,
        service_authority=authority, ifc_labels=labels,
        extra={"poller_name": "hn-ai"},
    ), enforce=True, ifc_labels=labels)

    out = await memory_store.coroutine(
        content="untrusted finding", stream="semantic",
        runtime=_runtime(context, "poller-store"),
    )

    assert "stored" in out
    call = write_store.atom_calls[-1]
    assert call["integrity"] == "untrusted"
    assert call["origin_trigger"] == "research-poller:hn-ai"
    assert call["origin_ref"] == "poller:hn-ai:123:batch:0"
    assert {"integrity", "origin_trigger", "origin_ref"}.isdisjoint(
        memory_store.args_schema.model_fields
    )


@pytest.mark.asyncio
async def test_saga_end_session_uses_runtime_not_model_session_for_authority(
    write_store: _WriteStore,
) -> None:
    source_acl = SessionACL(
        owner_principal="regular",
        origin_channel="discord-synthesis",
        origin_domain="discord",
        visibility="private",
        provenance_complete=True,
    )
    context = create_auth_context(
        AgentEvent(
            trigger="saga_session_end",
            channel_id="discord-synthesis",
            service_principal="synthesis",
            source_session_acl=source_acl,
        ),
        enforce=True,
    )
    context = replace(context, saga_session_id="active-synthesis-session")
    assert context.source_session_acl == source_acl
    out = await saga_ops.saga_end_session.ainvoke({
        "session_id": "active-synthesis-session", "summary": "done",
        "runtime": _runtime(context, "synthesis-end"),
    })
    assert "ok" in out
    call = write_store.session_calls[-1]
    assert call["session_id"] == "active-synthesis-session"
    assert call["owner_principal"] == "regular"
    assert call["origin_channel"] == "discord-synthesis"
    assert call["origin_domain"] == "discord"
    assert call["visibility"] == "private"
    assert call["provenance"]["created_by"] == "regular"
    assert call["provenance"]["derived_by"] == "service:synthesis"


@pytest.mark.parametrize("origin_domain", ["discord", "slack"])
@pytest.mark.asyncio
async def test_saga_end_session_real_store_accepts_inherited_ingress_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin_domain: str,
) -> None:
    class StubProvider:
        def embed(self, text: str, *, input_type: str = "passage") -> list[float]:
            return [1.0, 2.0, 3.0, 4.0]

        def dimensions(self) -> int:
            return 4

    monkeypatch.setattr("mimir.saga.embeddings.get_provider", lambda: StubProvider())
    store = SagaStore(db_path=tmp_path / "session-provenance.saga.db", embedding_dim=4)
    previous = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = store
    try:
        channel_id = f"{origin_domain}-synthesis"
        session_id = f"{origin_domain}-active-session"
        source_acl = SessionACL(
            owner_principal="regular",
            origin_channel=channel_id,
            origin_domain=origin_domain,
            visibility="private",
            provenance_complete=True,
        )
        context = create_auth_context(
            AgentEvent(
                trigger="saga_session_end",
                channel_id=channel_id,
                service_principal="synthesis",
                source_session_acl=source_acl,
            ),
            enforce=True,
        )
        context = replace(context, saga_session_id=session_id)

        out = await saga_ops.saga_end_session.ainvoke({
            "session_id": session_id,
            "summary": "done",
            "runtime": _runtime(context, f"{origin_domain}-real-end"),
        })

        assert "saga_end_session ok" in out
        row = store._ensure_conn().execute(
            "SELECT owner_principal, origin_channel, origin_domain, visibility "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        assert tuple(row) == ("regular", channel_id, origin_domain, "private")
        visible = await store.recent_session_boundaries(
            channel_id=channel_id,
            auth_context=AuthContext(
                principal="regular",
                canonical_principal="regular",
                roles=("user",),
                event_ingress=origin_domain,
                trigger="user_message",
                channel_id=channel_id,
                interactivity=None,
            ),
        )
        assert [boundary["session_id"] for boundary in visible] == [session_id]
    finally:
        _MEMORY_STATE["client"] = previous
        await store.close()


@pytest.mark.asyncio
async def test_saga_end_session_missing_or_mixed_source_acl_fails_closed(
    write_store: _WriteStore,
) -> None:
    context = replace(
        _service_context("saga_session_end", "discord-synthesis"),
        saga_session_id="mixed-session",
    )
    out = await saga_ops.saga_end_session.ainvoke({
        "session_id": "mixed-session", "summary": "done",
        "runtime": _runtime(context, "mixed-end"),
    })
    assert "ok" in out
    call = write_store.session_calls[-1]
    assert call["owner_principal"] == "legacy_admin"
    assert call["origin_domain"] is None
    assert call["visibility"] == "legacy_admin"
    assert call["provenance"] == {}


@pytest.mark.asyncio
async def test_http_trigger_spoof_does_not_gain_service_authority(
    write_store: _WriteStore,
) -> None:
    context = create_auth_context(
        AgentEvent(
            trigger="scheduled_tick", channel_id="http-spoof",
            extra={"event_ingress": "http_event"},
        ),
        enforce=True,
    )
    out = await memory_store.ainvoke({
        "content": "spoof", "stream": "semantic",
        "runtime": _runtime(context, "spoof-store"),
    })
    assert "write access denied" in out
    assert write_store.atom_calls == []
