"""Regression coverage for chainlink #365: SagaStore.query preserves read
concurrency without sharing one sqlite3 connection across worker threads.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from mimir.saga.client import SagaStore


def _install_minimal_atom(conn: sqlite3.Connection, *, atom_id: str = "atom1") -> None:
    """Insert enough data for FTS recall without invoking the embedding provider."""
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, stream, profile, "
        "memory_type, source_type, metadata, agent_id) "
        "VALUES (?, ?, ?, ?, 'semantic', 'standard', 'raw', 'test', '{}', 'default')",
        (atom_id, "concurrent query smoke term", atom_id, "2026-06-03T00:00:00+00:00"),
    )
    conn.commit()


@pytest.mark.asyncio
async def test_saga_query_uses_independent_connections_for_concurrent_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent query() calls should overlap, but not share sqlite handles.

    The live failure was worse than a flaky exception: simultaneous FTS5 reads
    on one ``check_same_thread=False`` sqlite connection can segfault. The fix is
    not a global read lock; production stores constructed from ``db_path`` open
    one short-lived sqlite connection per read-heavy operation, preserving read
    concurrency while avoiding shared-connection races.
    """
    monkeypatch.setattr("mimir.saga.client._query_embed_sync", lambda _q: [])

    store = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=None)
    conn = store._ensure_conn()
    _install_minimal_atom(conn)

    active = 0
    max_active = 0
    connections: list[sqlite3.Connection] = []
    original_operation_conn = store._operation_conn

    def observed_operation_conn():
        nonlocal active, max_active
        conn, should_close = original_operation_conn()
        connections.append(conn)
        active += 1
        max_active = max(max_active, active)

        class ObservedConnection:
            def __getattr__(self, name: str):
                return getattr(conn, name)

            def close(self) -> None:
                nonlocal active
                try:
                    conn.close()
                finally:
                    active -= 1

        return ObservedConnection(), should_close

    monkeypatch.setattr(store, "_operation_conn", observed_operation_conn)

    results = await asyncio.gather(
        *[
            store.query(
                "concurrent query smoke term",
                top_k=3,
                enable_session_boundary_rrf=False,
            )
            for _ in range(8)
        ]
    )

    assert max_active > 1
    assert len({id(conn) for conn in connections}) == 8
    assert all(result["items_returned"] >= 1 for result in results)
