"""Regression coverage for chainlink #365: SagaStore.query preserves read
concurrency without sharing one sqlite3 connection across worker threads.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from mimir.saga.client import SagaStore


def _install_minimal_atom(conn: sqlite3.Connection, *, atom_id: str = "atom1") -> None:
    """Insert enough data for FTS recall without invoking the embedding provider."""
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, stream, profile, "
        "memory_type, source_type, metadata, agent_id, owner_principal, visibility) "
        "VALUES (?, ?, ?, ?, 'semantic', 'standard', 'raw', 'test', '{}', 'default', "
        "'system', 'public')",
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

    # gather() schedules concurrent work but does not guarantee that tiny reads
    # overlap on a busy CI runner. Rendezvous pairs while both handles are live;
    # a serialized read implementation must fail rather than pass by scheduling
    # luck. Two parties avoid requiring eight default-executor worker threads.
    read_barrier = threading.Barrier(2, timeout=10)
    observation_lock = threading.Lock()
    active = 0
    max_active = 0
    connections: list[sqlite3.Connection] = []
    operation_wrappers: list[object] = []
    boundary_connections: list[object] = []
    original_operation_conn = store._operation_conn

    def observed_operation_conn():
        nonlocal active, max_active
        conn, should_close = original_operation_conn()
        with observation_lock:
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
                    with observation_lock:
                        active -= 1

        wrapper = ObservedConnection()
        with observation_lock:
            operation_wrappers.append(wrapper)
        return wrapper, should_close

    def observed_boundary_pathway(conn, *_args, **_kwargs):
        with observation_lock:
            boundary_connections.append(conn)
        read_barrier.wait()
        return []

    monkeypatch.setattr(store, "_operation_conn", observed_operation_conn)
    monkeypatch.setattr(
        store,
        "_session_boundary_atom_pathway_with_conn",
        observed_boundary_pathway,
    )

    results = await asyncio.gather(
        *[store.query("concurrent query smoke term", top_k=3) for _ in range(8)]
    )

    assert max_active > 1
    assert len({id(conn) for conn in connections}) == 8
    # The production default path includes session-boundary RRF. It must run on
    # each query's existing operation connection, not open a second connection.
    assert len(boundary_connections) == 8
    assert {id(conn) for conn in boundary_connections} == {
        id(conn) for conn in operation_wrappers
    }
    assert all(result["items_returned"] >= 1 for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [False, True])
async def test_cancelled_query_worker_never_writes_access(tmp_path, monkeypatch, injected):
    store = SagaStore(db_path=tmp_path / "cancel.db", embedding_dim=3)
    conn = store.connection()
    _install_minimal_atom(conn)
    if injected:
        store._db_path = None
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finalized = asyncio.Event()
    release = threading.Event()
    writes = []

    def embed(_query):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return []

    monkeypatch.setattr("mimir.saga.client._query_embed_sync", embed)
    monkeypatch.setattr(store, "_mark_retrieval_access_events", lambda *a, **kw: writes.append(a))
    monkeypatch.setattr(
        "mimir.saga.ownership.SagaReadAuthorization.finalize",
        lambda self: loop.call_soon_threadsafe(finalized.set),
    )
    task = asyncio.create_task(store.query("concurrent query smoke term"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finalized.wait(), 5)
        await store.close()
    assert writes == []


@pytest.mark.asyncio
async def test_cancelled_query_discards_queued_access_write(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-saga")
    monkeypatch.setattr("mimir.saga.client._SAGA_EXECUTOR", pool)
    monkeypatch.setattr("mimir.saga.client._query_embed_sync", lambda _query: [])
    store = SagaStore(db_path=tmp_path / "queued.db", embedding_dim=3)
    conn = store.connection()
    _install_minimal_atom(conn)
    changes = conn.total_changes
    loop = asyncio.get_running_loop()
    occupied = asyncio.Event()
    queued = asyncio.Event()
    release = threading.Event()
    read_finished = False
    access_futures = []
    submit = pool.submit
    run_worker = store._run_worker

    def blocked():
        loop.call_soon_threadsafe(occupied.set)
        assert release.wait(10)

    def observed_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        if read_finished:
            access_futures.append(future)
            queued.set()
        return future

    async def read_then_saturate(*args):
        nonlocal read_finished
        payload = await run_worker(*args)
        assert payload["items_returned"] > 0
        submit(blocked)
        await asyncio.wait_for(occupied.wait(), 5)
        read_finished = True
        return payload

    monkeypatch.setattr(pool, "submit", observed_submit)
    monkeypatch.setattr(store, "_run_worker", read_then_saturate)
    task = asyncio.create_task(store.query("concurrent query smoke term"))
    try:
        await asyncio.wait_for(queued.wait(), 5)
        assert len(access_futures) == 1
        assert not access_futures[0].running()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not release.is_set()
        assert access_futures[0].cancelled()
    finally:
        read_finished = False
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        # A subsequent shared operation proves the slot was released and drains
        # any wrongly retained access write before checking the actual database.
        await store._db_locked(lambda: None)
        final_changes = conn.total_changes
        await store.close()
        pool.shutdown(wait=True)
    assert final_changes == changes


@pytest.mark.asyncio
async def test_shared_waiters_do_not_occupy_workers_or_default_pool(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-saga")
    monkeypatch.setattr("mimir.saga.client._SAGA_EXECUTOR", pool)
    store = SagaStore()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def blocked():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    first = asyncio.create_task(store._db_locked(blocked))
    waiters = []
    try:
        await asyncio.wait_for(entered.wait(), 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        waiters = [asyncio.create_task(store._db_locked(lambda: None)) for _ in range(8)]
        await asyncio.sleep(0)
        # The cancelled worker still owns its slot. Other shared operations
        # wait as coroutines, leaving the second dedicated worker available.
        name = await asyncio.wait_for(store._run_worker(lambda: threading.current_thread().name), 2)
        assert name.startswith("test-saga")
        default_name = await asyncio.wait_for(asyncio.to_thread(lambda: threading.current_thread().name), 2)
        assert not default_name.startswith("test-saga")
        assert not any(task.done() for task in waiters)
    finally:
        release.set()
        await asyncio.gather(first, *waiters, return_exceptions=True)
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_query_failure_finalizes_authorization(tmp_path, monkeypatch):
    store = SagaStore(db_path=tmp_path / "failed.db")
    finalized = []

    def fail(_query):
        raise RuntimeError("read failed")

    monkeypatch.setattr("mimir.saga.client._query_embed_sync", fail)
    monkeypatch.setattr("mimir.saga.ownership.SagaReadAuthorization.finalize", lambda self: finalized.append(self))
    try:
        with pytest.raises(RuntimeError, match="read failed"):
            await store.query("query")
        assert len(finalized) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_shared_connection_entry_points_lock_and_preserve_sync_api(monkeypatch):
    store = SagaStore()
    held = []
    calls = []
    loop_thread = threading.get_ident()

    class Connection:
        def execute(self, sql):
            assert held == ["db"]
            assert threading.get_ident() != loop_thread
            calls.append("health")

        def close(self):
            assert held == ["db"]
            assert threading.get_ident() != loop_thread
            calls.append("close")

    conn = Connection()

    def ensure():
        assert held[0] == "db"
        return conn

    def index(connection):
        assert connection is conn
        assert held == ["db", "index"]
        calls.append("rebuild")

    class Lock:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            held.append(self.name)

        def __exit__(self, *exc):
            assert held.pop() == self.name

    store._db_lock = Lock("db")
    store._index_lock = Lock("index")
    monkeypatch.setattr(store, "_ensure_conn", ensure)
    monkeypatch.setattr(store, "_ensure_index", index)
    assert store.connection() is conn
    assert store.rebuild_index() is None
    assert await store.__aenter__() is store
    assert await store.health() is True
    store._conn = conn
    await store.close()
    assert store._conn is None
    assert calls == ["rebuild", "health", "close"]
