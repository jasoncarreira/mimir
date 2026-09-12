from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from mimir.reflection import most_retrieved
from mimir.saga import _config_io
from mimir.saga.client import SagaStore
from mimir.saga_client import RecordingSagaClient, SagaError, make_saga_client


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "rebuild_index_if_needed", "consolidate", "consolidate_skill_memories",
])
async def test_maintenance_migrations_run_off_loop_under_db_lock(tmp_path, monkeypatch, method):
    store = SagaStore(db_path=tmp_path / "store.db")
    # An empty database must not need synthesis or an external provider.
    store._rich_synth_fn = object()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    migration_started = asyncio.Event()
    release_migration = threading.Event()
    migrate = store._apply_pending_migrations
    migration_connections = []

    def paused_migration(conn, *, fresh):
        assert threading.get_ident() != loop_thread
        migration_connections.append(conn)
        loop.call_soon_threadsafe(migration_started.set)
        assert release_migration.wait(10), "event loop did not release migration"
        migrate(conn, fresh=fresh)

    monkeypatch.setattr(store, "_apply_pending_migrations", paused_migration)
    kwargs = {"dedup_threshold": 0.95} if method != "rebuild_index_if_needed" else {}
    tasks = [asyncio.create_task(getattr(store, method)(**kwargs)) for _ in range(2)]
    started = asyncio.create_task(migration_started.wait())
    try:
        completed, _ = await asyncio.wait(
            [started, *tasks], timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in tasks:
            if task in completed:
                task.result()
        assert started in completed, "migration never started"
        # This runs on the loop while migration is paused in a worker. RLock
        # reentrancy would let this succeed if the loop owned the lock instead.
        acquired = store._db_lock.acquire(blocking=False)
        if acquired:
            store._db_lock.release()
        assert not acquired
        release_migration.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert migration_connections == [store._conn]
    finally:
        release_migration.set()
        started.cancel()
        await asyncio.gather(started, *tasks, return_exceptions=True)
        await store.close()


@pytest.fixture(params=["default", "relative", "absolute"])
def configured_store(request, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    config = home / "saga.toml"
    monkeypatch.setenv("SAGA_CONFIG", str(config))
    monkeypatch.setattr(_config_io, "_config", None)
    monkeypatch.setattr(_config_io, "_config_loaded", False)
    monkeypatch.setattr(_config_io, "_explicit_keys", {})
    if request.param == "default":
        config.write_text("")
        target = home / ".mimir" / "saga.db"
    else:
        custom = "nested/custom #1?.db"
        if request.param == "absolute":
            custom = str(tmp_path / "external" / custom)
        config.write_text(f"[storage]\ndb_path = {json.dumps(custom)}\n")
        target = Path(custom) if Path(custom).is_absolute() else home / ".mimir" / custom
    return home, cwd, target


@pytest.mark.asyncio
async def test_factory_configured_first_run_and_reflection_empty(configured_store, capsys):
    home, cwd, target = configured_store
    client = make_saga_client()
    assert isinstance(client, RecordingSagaClient)
    assert not target.exists()  # Factory retains lazy initialization.
    try:
        assert client.connection().execute("SELECT count(*) FROM atoms").fetchone()[0] == 0
        assert client._db_path == target
    finally:
        await client.close()
    assert target.exists()
    assert await most_retrieved.run(argparse.Namespace(
        days=7, count=10, channel=None, contributed_only=False, trend=None,
    )) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert list(cwd.iterdir()) == []
    if target != home / ".mimir" / "saga.db":
        assert not (home / ".mimir" / "saga.db").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing_parent", "missing", "zero_byte", "unrelated"])
async def test_reflection_requires_atoms_without_creating_files(configured_store, state, capsys):
    home, cwd, target = configured_store
    if state != "missing_parent":
        target.parent.mkdir(parents=True, exist_ok=True)
    if state == "zero_byte":
        target.touch()
    elif state == "unrelated":
        conn = sqlite3.connect(target)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
    root = home.parent
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    directories = {p.relative_to(root) for p in root.rglob("*") if p.is_dir()}
    error = sqlite3.OperationalError if state.startswith("missing") else SagaError
    with pytest.raises(error):
        await most_retrieved.run(argparse.Namespace(
            days=7, count=10, channel=None, contributed_only=False, trend=None,
        ))
    assert capsys.readouterr().out == ""
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert {p.relative_to(root) for p in root.rglob("*") if p.is_dir()} == directories
    assert list(cwd.iterdir()) == []


@pytest.mark.asyncio
async def test_existing_guard_applies_at_open_and_reopen(tmp_path):
    path = tmp_path / "store.db"
    original = SagaStore(db_path=path)
    original.connection()
    await original.close()
    client = make_saga_client(db_path=path, require_existing=True, record_calls=False)
    path.unlink()  # Disappears after factory construction, before lazy open.
    try:
        with pytest.raises(sqlite3.OperationalError):
            client.connection()
        assert not path.exists()
        with pytest.raises(sqlite3.OperationalError):
            client._connect_db_path(enable_wal=False)
        assert list(tmp_path.iterdir()) == []
    finally:
        await client.close()


def test_existing_guard_rejects_injected_connection_without_atoms():
    conn = sqlite3.connect(":memory:")
    try:
        client = SagaStore(conn=conn, require_existing=True)
        with pytest.raises(SagaError, match="no atoms table"):
            client.connection()
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        conn.close()
