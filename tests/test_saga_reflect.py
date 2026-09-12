from __future__ import annotations

import struct

import pytest

from mimir.models import AuthContext
from mimir.saga.client import SagaStore
from mimir.saga.vector_index import VectorIndex


@pytest.mark.asyncio
@pytest.mark.parametrize("with_index", [False, True])
@pytest.mark.parametrize(
    ("dim", "float_count"),
    [(3, 3), (3, 4), (4, 3), (4, 4)],
    ids=["wrong-dimension", "wrong-declared-dimension", "wrong-byte-length", "valid"],
)
async def test_session_embedding_dimension(tmp_path, monkeypatch, with_index, dim, float_count):
    blob = struct.pack(f"{float_count}f", *([1.0] * float_count))
    monkeypatch.setattr(
        "mimir.saga.client._embed_text_sync",
        lambda text: (blob, "stub", "stub", dim),
    )
    store = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=4)
    if with_index:
        store._sessions_index = VectorIndex(dimension=4)
        # The existing index takes precedence over the configured fallback.
        store._sessions_embedding_dim = 3
    auth = AuthContext(
        principal="test-admin", canonical_principal="test-admin", roles=("admin",),
        event_ingress="test", trigger="test", channel_id=None, interactivity=None,
        saga_session_id="session",
    )
    try:
        if dim != 4 or float_count != 4:
            with pytest.raises(ValueError, match="reflect embedding dimension mismatch: expected 4"):
                await store.end_session("session", "Summary", auth_context=auth)
            assert store._ensure_conn().execute(
                "SELECT COUNT(*) FROM sessions WHERE id = 'session'"
            ).fetchone()[0] == 0
        else:
            result = await store.end_session("session", "Summary", auth_context=auth)
            assert result["session_summary_written"] is True
            row = store._ensure_conn().execute(
                "SELECT summary, embedding, embedding_dim FROM sessions WHERE id = 'session'"
            ).fetchone()
            assert tuple(row) == ("Summary", blob, 4)
    finally:
        await store.close()
