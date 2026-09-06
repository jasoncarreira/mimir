from __future__ import annotations

import os
import sqlite3
import struct
from pathlib import Path
from unittest.mock import Mock

import pytest

from mimir.saga import embeddings
from mimir.saga.reembed import reembed


@pytest.fixture
def provider(monkeypatch):
    from mimir.saga import _config_io

    monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: 2000)
    provider = Mock(provider_name="live-provider", model_id="live-model")
    provider.dimensions.return_value = 3
    provider.batch_embed.side_effect = lambda texts, **kw: [[1., 2., 3.] for _ in texts]
    monkeypatch.setattr(embeddings, "get_provider", lambda: provider)
    return provider


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "saga.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            (Path(__file__).parents[1] / "mimir/saga/schema.sql").read_text()
        )
        for i, dim in enumerate([1, 2, 2, 3, 2]):
            conn.execute(
                "INSERT INTO atoms(id, content, content_hash, created_at, tombstoned) "
                "VALUES (?, ?, ?, 'old', ?)",
                (str(i), f"atom {i}", str(i), int(i == 4)),
            )
            conn.execute(
                "INSERT INTO embeddings(atom_id, provider, model, dim, vec, embedded_at) "
                "VALUES (?, 'old-provider', 'old-model', ?, ?, 'old')",
                (str(i), dim, struct.pack(f"<{dim}f", *([0.] * dim))),
            )
        for i, dim in enumerate([1, 2, 2, 3, None]):
            conn.execute(
                "INSERT INTO sessions(id, started_at, summary, embedding, embedding_dim) "
                "VALUES (?, 'old', ?, ?, ?)",
                (str(i), f"session {i}", bytes((dim or 2) * 4), dim),
            )
        conn.execute(
            "INSERT INTO atoms(id, content, content_hash, created_at) "
            "VALUES ('missing', 'no vector', 'missing', 'old')"
        )
    return path


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("atoms", "embeddings", "sessions")}


def test_repair_bounded_batches_live_provenance_and_rerun(db, provider):
    before = snapshot(db)
    progress = []
    report = reembed(db, batch_size=2, progress=progress.append)
    assert report == dict(atoms_stale=3, atoms_updated=3, sessions_stale=4,
                          sessions_updated=4, atoms_unrepaired=0, sessions_unrepaired=0)
    assert [call.args[0] for call in provider.batch_embed.call_args_list] == [
        ["atom 0", "atom 1"], ["atom 2"], ["session 0", "session 1"], ["session 2", "session 4"],
    ]
    assert all(call.kwargs == {"input_type": "passage"}
               for call in provider.batch_embed.call_args_list)
    after = snapshot(db)
    assert before["atoms"] == after["atoms"]
    assert before["embeddings"][3:] == after["embeddings"][3:]
    assert before["sessions"][3] == after["sessions"][3]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT provider, model, dim, vec FROM embeddings WHERE atom_id='0'"
        ).fetchone() == ("live-provider", "live-model", 3, struct.pack("<3f", 1, 2, 3))
        assert conn.execute(
            "SELECT embedding_dim, embedding FROM sessions WHERE id='0'"
        ).fetchone() == (3, struct.pack("<3f", 1, 2, 3))
    assert sum("committed" in message for message in progress) == 4
    provider.batch_embed.reset_mock()
    assert reembed(db)["atoms_stale"] == 0
    provider.batch_embed.assert_not_called()
    assert snapshot(db) == after


def test_dry_run_is_read_only(db, provider):
    before = snapshot(db)
    bytes_before = db.read_bytes()
    assert reembed(db, dry_run=True) == dict(
        atoms_stale=3, atoms_updated=0, sessions_stale=4, sessions_updated=0,
        atoms_unrepaired=0, sessions_unrepaired=0,
    )
    provider.batch_embed.assert_not_called()
    assert snapshot(db) == before
    assert db.read_bytes() == bytes_before


@pytest.mark.parametrize("fail_call", [2, 4])
def test_interruption_commits_and_resumes(db, provider, fail_call):
    calls = 0

    def embed(texts, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise KeyboardInterrupt
        return [[1., 2., 3.] for _ in texts]

    provider.batch_embed.side_effect = embed
    with pytest.raises(KeyboardInterrupt):
        reembed(db, batch_size=2)
    with sqlite3.connect(db) as conn:
        atoms = conn.execute("SELECT count(*) FROM embeddings WHERE dim=3").fetchone()[0]
        sessions = conn.execute("SELECT count(*) FROM sessions WHERE embedding_dim=3").fetchone()[0]
    assert (atoms, sessions) == ((3, 1) if fail_call == 2 else (4, 3))
    provider.batch_embed.reset_mock()
    report = reembed(db, batch_size=2)
    assert report["atoms_updated"] == (1 if fail_call == 2 else 0)
    assert report["sessions_updated"] == (4 if fail_call == 2 else 2)
    assert reembed(db)["sessions_stale"] == 0


@pytest.mark.parametrize("vectors", [
    [], [[1., 2., 3.]], [[1., 2., 3.]] * 3,
    [[1., 2., 3.], [1., 2.]],
    [[1., 2., 3.], [float("nan"), 2., 3.]],
    [[1., 2., 3.], [float("inf"), 2., 3.]],
    [[1., 2., 3.], [1e100, 2., 3.]],
])
@pytest.mark.parametrize("target", ["atoms", "sessions"])
def test_invalid_outputs_do_not_write_any_of_batch(db, provider, vectors, target):
    if target == "sessions":
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE embeddings SET dim=3")
    before = snapshot(db)
    provider.batch_embed.side_effect = None
    provider.batch_embed.return_value = vectors
    with pytest.raises((ValueError, OverflowError)):
        reembed(db, batch_size=2)
    assert snapshot(db) == before


def test_write_interruption_rolls_back_batch(db, provider):
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TRIGGER fail_second BEFORE UPDATE ON embeddings
            WHEN NEW.atom_id = '1'
            BEGIN SELECT RAISE(ABORT, 'write failed'); END;
        """)
    before = snapshot(db)
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        reembed(db, batch_size=2)
    assert snapshot(db) == before


@pytest.mark.parametrize("batch_size", [0, -1])
def test_invalid_batch_size(db, provider, batch_size):
    with pytest.raises(ValueError, match="positive"):
        reembed(db, batch_size=batch_size)
    provider.batch_embed.assert_not_called()


def test_missing_db_not_created(tmp_path, provider):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        reembed(path)
    assert not path.exists()


@pytest.mark.parametrize("home_source", ["flag", "env", "cwd"])
@pytest.mark.parametrize("absolute_db", [False, True])
def test_cli_home_config_and_options(tmp_path, monkeypatch, home_source, absolute_db):
    from mimir.cli import main
    from mimir.saga import _config_io
    from mimir.saga import reembed as module
    from mimir import config

    monkeypatch.delenv("SAGA_CONFIG", raising=False)
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "saga.toml").write_text("[storage]\ndb_path='custom.db'\n")
    monkeypatch.setattr(config, "_load_home_dotenv", Mock())
    configured = str(tmp_path / "absolute.db") if absolute_db else "custom.db"

    def get_config():
        assert os.environ["MIMIR_HOME"] == str(home)
        assert os.environ["SAGA_CONFIG"] == str(home / "saga.toml")
        config._load_home_dotenv.assert_called_once_with(home)
        return lambda *args: configured

    monkeypatch.setattr(_config_io, "get_config", get_config)
    run = Mock()
    monkeypatch.setattr(module, "reembed", run)
    argv = ["saga-reembed", "--dry-run", "--batch-size", "7", "--batch-delay", "0.25"]
    if home_source == "flag":
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "wrong"))
        argv += ["--home", str(home)]
    elif home_source == "env":
        monkeypatch.setenv("MIMIR_HOME", str(home))
    else:
        monkeypatch.chdir(home)
    main(argv)
    assert run.call_args.args == (Path(configured) if absolute_db else home / ".mimir/custom.db",)
    assert run.call_args.kwargs["dry_run"] is True
    assert run.call_args.kwargs["batch_size"] == 7
    assert run.call_args.kwargs["batch_delay"] == 0.25


def test_cli_interrupt(tmp_path, monkeypatch, capsys):
    from mimir.cli import main
    from mimir.saga import _config_io
    from mimir.saga import reembed as module

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: "saga.db")
    monkeypatch.setattr(module, "reembed", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(SystemExit) as exc:
        main(["saga-reembed"])
    assert exc.value.code == 130
    assert "Committed batches are preserved" in capsys.readouterr().err


@pytest.mark.parametrize("summary", [None, "", " \t\n\r\v\f"])
def test_unrepaired_sessions_reported_and_cli_fails(db, provider, monkeypatch, capsys, summary):
    from mimir.cli import main
    from mimir.saga import _config_io

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET summary=? WHERE id='4'", (summary,))
    before = snapshot(db)
    preview = reembed(db, dry_run=True)
    assert preview["sessions_stale"] == 4
    assert preview["sessions_unrepaired"] == 1
    provider.batch_embed.assert_not_called()
    assert snapshot(db) == before
    monkeypatch.setenv("MIMIR_HOME", str(db.parent))
    monkeypatch.setattr(_config_io, "get_config", lambda: lambda section, *args:
                        str(db) if section == "storage" else 2000)
    main(["saga-reembed", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        main(["saga-reembed"])
    assert exc.value.code == 1
    assert "1 unrepaired stale rows remain" in capsys.readouterr().out
    assert snapshot(db)["sessions"][4] == before["sessions"][4]
    provider.batch_embed.reset_mock()
    with pytest.raises(SystemExit) as exc:
        main(["saga-reembed"])
    assert exc.value.code == 1
    provider.batch_embed.assert_not_called()


def test_null_atom_dimensions_in_legacy_schema(tmp_path, provider):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE atoms(id TEXT, content TEXT, tombstoned INTEGER);
            CREATE TABLE embeddings(atom_id TEXT, dim INTEGER, vec BLOB,
                                    provider TEXT, model TEXT, embedded_at TEXT);
            CREATE TABLE sessions(id TEXT, summary TEXT, embedding BLOB, embedding_dim INTEGER);
            INSERT INTO atoms VALUES ('a', 'original', 0);
            INSERT INTO embeddings VALUES ('a', NULL, X'00', 'old', 'old', 'old');
        """)
    assert reembed(path, dry_run=True)["atoms_stale"] == 1
    assert reembed(path)["atoms_updated"] == 1
    assert reembed(path)["atoms_stale"] == 0


@pytest.mark.parametrize("dry_run,delay", [(False, 0.25), (False, 0.0), (True, 0.25)])
def test_input_limit_and_pacing(db, provider, monkeypatch, dry_run, delay):
    from mimir.saga import _config_io
    from mimir.saga import reembed as module

    monkeypatch.setattr(_config_io, "get_config", lambda: lambda *args: 4)
    events = []

    def embed(texts, **kwargs):
        events.append(("embed", texts))
        assert all(len(text) <= 4 for text in texts)
        return [[1., 2., 3.] for _ in texts]

    provider.batch_embed.side_effect = embed
    monkeypatch.setattr(module.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    reembed(db, batch_size=2, batch_delay=delay, dry_run=dry_run)
    if dry_run:
        assert events == []
    else:
        calls = [("embed", ["atom", "atom"]), ("embed", ["atom"]),
                 ("embed", ["sess", "sess"]), ("embed", ["sess", "sess"])]
        expected = []
        for call in calls:
            if expected and delay:
                expected.append(("sleep", delay))
            expected.append(call)
        assert events == expected


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf")])
def test_invalid_delay(db, provider, delay):
    from mimir.cli import main

    with pytest.raises(ValueError, match="nonnegative"):
        reembed(db, batch_delay=delay)
    with pytest.raises(SystemExit) as exc:
        main(["saga-reembed", "--batch-delay", str(delay)])
    assert exc.value.code == 2
    provider.batch_embed.assert_not_called()


def test_fresh_saga_store_builds_exclude_zero_after_repair(db, provider, monkeypatch):
    from mimir.saga.client import SagaStore
    from mimir.saga.embedding_status import embedding_status
    from mimir.saga.vector_index import FAISS_AVAILABLE

    assert FAISS_AVAILABLE, "requires the repository dev dependencies"
    monkeypatch.setattr("mimir.saga.vector_index._warn_drops", lambda *args: None)
    with sqlite3.connect(db) as conn:
        store = SagaStore(conn=conn)
        assert store._ensure_index(conn).dimension_mismatch_count == 3
        assert store._ensure_sessions_index(conn).dimension_mismatch_count == 4
    reembed(db, batch_size=2)
    # A new store represents the documented restart, without an explicit dim.
    with sqlite3.connect(db) as conn:
        store = SagaStore(conn=conn)
        atoms = store._ensure_index(conn)
        sessions = store._ensure_sessions_index(conn)
        assert atoms.dimension == sessions.dimension == provider.dimensions()
        assert atoms.dimension_mismatch_count == sessions.dimension_mismatch_count == 0
        assert atoms.total_vectors == 4
        assert sessions.total_vectors == 5
        health = embedding_status(conn, provider.dimensions())
        assert health["atoms"]["excluded_from_recall"] == 0
        assert health["sessions"]["excluded_from_recall"] == 0
