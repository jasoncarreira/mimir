from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from mimir.reflection import most_retrieved
from mimir.saga import _config_io
from mimir.saga.client import SagaStore
from mimir.saga_client import RecordingSagaClient, SagaError, make_saga_client


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
