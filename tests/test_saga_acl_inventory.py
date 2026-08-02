from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime

from mimir.access_control import create_auth_context
from mimir.models import AgentEvent, AuthContext
from mimir.saga.acl_inventory import inventory_acl, inventory_path
from mimir.saga.client import SagaStore
from mimir.saga.consolidate import consolidate
from mimir.saga.store import store as primitive_store
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.store import memory_store


def _embedding(_content: str):
    return struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "test", "test", 4


def _runtime(auth: AuthContext, call_id: str) -> ToolRuntime[AuthContext]:
    return ToolRuntime(
        state={}, context=auth, config={}, stream_writer=lambda _: None,
        tool_call_id=call_id, store=None,
    )


@pytest.mark.asyncio
async def test_inventory_classifies_atoms_seeded_through_real_write_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "saga.db"
    saga = SagaStore(db_path=db_path, embedding_dim=4)
    monkeypatch.setattr("mimir.saga.client._embed_text_sync", _embedding)

    conn = saga.connection()
    for index in range(3):
        primitive_store(
            conn, f"legacy evidence {index}", embed_fn=_embedding,
            source_type="agent_authored" if index == 0 else "conversation",
        )
    result = consolidate(
        conn,
        embed_fn=_embedding,
        cluster_fn=lambda raws: [raws],
        observation_synth_fn=lambda _raws: ("legacy-derived observation", []),
    )
    assert len(result.observations_emitted) == 1

    previous = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = saga
    try:
        user = AuthContext(
            principal="jason", canonical_principal="jason", roles=("admin",),
            trigger="user_message", channel_id="discord:private",
            event_ingress="discord", interactivity=None,
        )
        service = create_auth_context(
            AgentEvent(
                trigger="saga_session_end", channel_id="saga:synthesis",
                service_principal="synthesis",
            ),
            enforce=True,
        )
        assert "stored" in await memory_store.ainvoke({
            "content": "owned user atom", "stream": "semantic",
            "runtime": _runtime(user, "user-write"),
        })
        assert "stored" in await memory_store.ainvoke({
            "content": "owned service atom", "stream": "semantic",
            "runtime": _runtime(service, "service-write"),
        })
    finally:
        _MEMORY_STATE["client"] = previous

    report = inventory_acl(conn)
    assert report["total"] == 6
    assert report["counts"] == {
        "direct_write_missing_owner": 1,
        "derived_legacy_inheritance": 1,
        "service_owned": 1,
        "other_owned": 1,
        "legacy_unattributed": 2,
        "unclassified": 0,
    }
    assert report["direct_legacy_detail"] == [
        {
            "source_type": "conversation",
            "origin_channel": "<null>",
            "origin_domain": "<null>",
            "provenance": "empty",
            "count": 2,
        },
        {
            "source_type": "agent_authored",
            "origin_channel": "<null>",
            "origin_domain": "<null>",
            "provenance": "empty",
            "count": 1,
        },
    ]
    assert report["derived_legacy_detail"] == [{
        "source_type": "conversation", "count": 1,
        "distinct_evidence_atoms": 3,
    }]

    disk_report = inventory_path(db_path)
    assert json.dumps(disk_report, sort_keys=True) == json.dumps(report, sort_keys=True)
    await saga.close()


def test_inventory_excludes_tombstones_by_default(tmp_path: Path) -> None:
    saga = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=4)
    conn = saga.connection()
    atom_id = primitive_store(conn, "old raw", embed_fn=_embedding).atom_id
    conn.execute("UPDATE atoms SET tombstoned = 1 WHERE id = ?", (atom_id,))
    conn.commit()

    assert inventory_acl(conn)["total"] == 0
    assert inventory_acl(conn, include_tombstoned=True)["total"] == 1
