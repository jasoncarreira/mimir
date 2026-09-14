"""Evidence integrity propagation through both consolidation entry points."""

from __future__ import annotations

import struct

import pytest

from mimir.saga.client import SagaStore
from mimir.saga.consolidate import _compute_intersected_acl, consolidate
from mimir.saga.ownership import Ownership
from mimir.saga.store import store


def _embed(text):
    return struct.pack("4f", 1, 0, 0, 0), "test", "test", 4


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("mimir.saga.client._embed_text_sync", _embed)
    monkeypatch.setattr(
        "mimir.saga._config_io.get_config",
        lambda: lambda section, key, default=None: default,
    )
    return SagaStore(db_path=tmp_path / "saga.db", embedding_dim=4)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["standalone", "client"])
@pytest.mark.parametrize("labels, expected", [
    (["trusted", "trusted", "trusted"], "trusted"),
    (["trusted", "untrusted", "trusted"], "untrusted"),
    (["untrusted", "untrusted", "untrusted"], "untrusted"),
])
async def test_consolidation_inherits_evidence_integrity(client, path, labels, expected):
    conn = client.connection()
    try:
        evidence_ids = [
            store(conn, f"evidence {i}", embed_fn=_embed, integrity=label).atom_id
            for i, label in enumerate(labels)
        ]

        async def synth(cluster, **kwargs):
            return {"content": "derived observation", "topics": []}

        if path == "client":
            client._rich_synth_fn = synth
            result = await client.consolidate(dedup_first=False)
            emitted = result["observations_emitted"]
        else:
            result = consolidate(
                conn, embed_fn=_embed, cluster_fn=lambda raws: [raws],
                observation_synth_fn=lambda cluster: ("derived observation", []),
            )
            emitted = result.observations_emitted

        assert len(emitted) == 1
        assert conn.execute(
            "SELECT integrity FROM atoms WHERE id = ?", (emitted[0],),
        ).fetchone()[0] == expected
        assert {row[0] for row in conn.execute(
            "SELECT target_id FROM atom_relations "
            "WHERE source_id = ? AND relation_type = 'evidenced_by'", (emitted[0],),
        )} == set(evidence_ids)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["empty", "missing", "tombstoned", "duplicates"])
async def test_evidence_integrity_requires_complete_live_inputs(client, state):
    conn = client.connection()
    try:
        atom_id = store(conn, "evidence", embed_fn=_embed, integrity="trusted").atom_id
        ids = [atom_id]
        if state == "empty":
            ids = []
        elif state == "missing":
            ids.append("missing")
        elif state == "tombstoned":
            conn.execute("UPDATE atoms SET tombstoned = 1 WHERE id = ?", (atom_id,))
            conn.commit()
        else:
            ids.append(atom_id)
        acl, integrity = _compute_intersected_acl(conn, ids)
        assert integrity == ("trusted" if state == "duplicates" else "untrusted")
        if state != "duplicates":
            assert acl == Ownership()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_store_without_integrity_does_not_grant_trust(client):
    conn = client.connection()
    try:
        atom_id = store(conn, "unclassified input", embed_fn=_embed).atom_id
        assert conn.execute(
            "SELECT integrity FROM atoms WHERE id = ?", (atom_id,),
        ).fetchone()[0] == "untrusted"
    finally:
        await client.close()
