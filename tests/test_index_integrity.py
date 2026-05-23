"""Tests for the index-integrity probes (SPEC §8.3, §16 item 16)."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.index_integrity import (
    IntegrityCheck,
    IntegrityReport,
    check_all,
    check_file_corpus,
    check_saga,
    run_verify_index_cmd,
)


# ── fixtures ─────────────────────────────────────────────────────────


def _init_file_corpus(home: Path, *, embedding_dim_bytes: int = 384 * 4) -> Path:
    """Create a minimal but realistic file-corpus index.db: files +
    chunks + chunks_fts schema, two rows in each, FTS5 in sync,
    embeddings uniformly sized."""
    db_path = home / ".mimir" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE files (
            path TEXT PRIMARY KEY, scope TEXT, mtime REAL,
            size INTEGER, chunk_count INTEGER, description TEXT
        );
        CREATE TABLE chunks (
            path TEXT, chunk_index INTEGER, content TEXT,
            embedding BLOB,
            PRIMARY KEY (path, chunk_index),
            FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            path UNINDEXED, chunk_index UNINDEXED, content,
            tokenize='porter unicode61'
        );
    """)
    blob = b"\0" * embedding_dim_bytes
    conn.execute(
        "INSERT INTO files VALUES ('a.md', 'memory', 1.0, 10, 1, 'a')",
    )
    conn.execute(
        "INSERT INTO files VALUES ('b.md', 'memory', 2.0, 20, 1, 'b')",
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('a.md', 0, 'hello world', ?)",
        (blob,),
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('b.md', 0, 'goodbye world', ?)",
        (blob,),
    )
    conn.execute(
        "INSERT INTO chunks_fts (path, chunk_index, content) "
        "VALUES ('a.md', 0, 'hello world')",
    )
    conn.execute(
        "INSERT INTO chunks_fts (path, chunk_index, content) "
        "VALUES ('b.md', 0, 'goodbye world')",
    )
    conn.commit()
    conn.close()
    return db_path


def _init_saga(home: Path) -> Path:
    """Create a minimal saga.db: atoms + atoms_fts with two atoms."""
    db_path = home / ".mimir" / "saga.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY, content TEXT, content_hash TEXT,
            stream TEXT, profile TEXT, memory_type TEXT,
            arousal REAL, valence REAL, encoding_confidence REAL,
            topics TEXT, source_type TEXT, metadata TEXT,
            tombstoned INTEGER DEFAULT 0,
            tombstoned_at TEXT, tombstoned_reason TEXT,
            is_pinned INTEGER DEFAULT 0,
            agent_id TEXT, session_id TEXT, created_at TEXT
        );
        CREATE VIRTUAL TABLE atoms_fts USING fts5(content, tokenize='porter unicode61');
    """)
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, stream, memory_type, "
        "arousal, valence, encoding_confidence, source_type, created_at) "
        "VALUES ('a1', 'first atom', 'h1', 's', 'raw', 0.0, 0.0, 1.0, 'x', '2026-05-23')",
    )
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, stream, memory_type, "
        "arousal, valence, encoding_confidence, source_type, created_at) "
        "VALUES ('a2', 'second atom', 'h2', 's', 'raw', 0.0, 0.0, 1.0, 'x', '2026-05-23')",
    )
    conn.execute("INSERT INTO atoms_fts (rowid, content) VALUES (1, 'first atom')")
    conn.execute("INSERT INTO atoms_fts (rowid, content) VALUES (2, 'second atom')")
    conn.commit()
    conn.close()
    return db_path


# ── happy path ───────────────────────────────────────────────────────


def test_clean_file_corpus_passes_all_checks(tmp_path: Path):
    _init_file_corpus(tmp_path)
    checks = check_file_corpus(tmp_path)
    assert len(checks) == 5
    for c in checks:
        assert c.ok, f"unexpected failure: {c.render()}"
    names = {c.name for c in checks}
    assert "sqlite_integrity_check" in names
    assert "foreign_key_check" in names
    assert "fts5_integrity_chunks_fts" in names
    assert "fts5_sync_chunks_fts" in names
    assert "embedding_dim_uniform_chunks" in names


def test_clean_saga_passes_all_checks(tmp_path: Path):
    _init_saga(tmp_path)
    checks = check_saga(tmp_path)
    assert len(checks) == 4
    for c in checks:
        assert c.ok, f"unexpected failure: {c.render()}"


def test_check_all_combines_both_dbs(tmp_path: Path):
    _init_file_corpus(tmp_path)
    _init_saga(tmp_path)
    report = check_all(tmp_path)
    # 5 file-corpus + 4 saga.
    assert len(report.checks) == 9
    assert report.ok
    assert report.failures == []


# ── missing DBs ──────────────────────────────────────────────────────


def test_missing_file_corpus_db_reports_clearly(tmp_path: Path):
    checks = check_file_corpus(tmp_path)
    assert len(checks) == 1
    assert checks[0].name == "file_corpus_db_present"
    assert not checks[0].ok
    assert "missing" in checks[0].detail


def test_missing_saga_db_reports_clearly(tmp_path: Path):
    checks = check_saga(tmp_path)
    assert len(checks) == 1
    assert checks[0].name == "saga_db_present"
    assert not checks[0].ok


# ── corruption detection ────────────────────────────────────────────


def test_fts5_row_count_drift_detected(tmp_path: Path):
    """A chunk in the base table without a corresponding FTS5 row
    (e.g. crash between the two INSERTs) shows up as drift."""
    db_path = _init_file_corpus(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO files VALUES ('c.md', 'memory', 3.0, 30, 1, 'c')")
    conn.execute(
        "INSERT INTO chunks VALUES ('c.md', 0, 'orphaned content', ?)",
        (b"\0" * (384 * 4),),
    )
    # No corresponding INSERT into chunks_fts → drift.
    conn.commit()
    conn.close()
    checks = check_file_corpus(tmp_path)
    sync = next(c for c in checks if c.name == "fts5_sync_chunks_fts")
    assert not sync.ok
    assert "drift" in sync.detail
    assert "chunks=3 vs chunks_fts=2" in sync.detail


def test_mixed_embedding_dims_detected(tmp_path: Path):
    """A row with a different-length embedding blob (e.g. embedder
    swapped from bge-small (384d) to a larger model without rebuild)
    shows up as mixed dims."""
    db_path = _init_file_corpus(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO files VALUES ('c.md', 'memory', 3.0, 30, 1, 'c')")
    # Different-sized blob (1024d * 4 bytes vs the fixture's 384d * 4).
    conn.execute(
        "INSERT INTO chunks VALUES ('c.md', 0, 'mixed', ?)",
        (b"\0" * (1024 * 4),),
    )
    # Keep FTS5 consistent so we don't get a different failure.
    conn.execute(
        "INSERT INTO chunks_fts (path, chunk_index, content) "
        "VALUES ('c.md', 0, 'mixed')",
    )
    conn.commit()
    conn.close()
    checks = check_file_corpus(tmp_path)
    dims = next(c for c in checks if c.name == "embedding_dim_uniform_chunks")
    assert not dims.ok
    assert "mixed dims" in dims.detail
    # Both sizes should be surfaced for the operator.
    assert "1536" in dims.detail  # 384 * 4
    assert "4096" in dims.detail  # 1024 * 4


def test_empty_index_doesnt_trip_dim_check(tmp_path: Path):
    """A fresh, empty index has no embeddings — the dim check passes
    (the failure case requires AT LEAST one row of differing length)."""
    # Create the schema but don't insert any chunks.
    db_path = tmp_path / ".mimir" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE files (
            path TEXT PRIMARY KEY, scope TEXT, mtime REAL,
            size INTEGER, chunk_count INTEGER, description TEXT
        );
        CREATE TABLE chunks (
            path TEXT, chunk_index INTEGER, content TEXT,
            embedding BLOB,
            PRIMARY KEY (path, chunk_index)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(content);
    """)
    conn.commit()
    conn.close()
    checks = check_file_corpus(tmp_path)
    dims = next(c for c in checks if c.name == "embedding_dim_uniform_chunks")
    assert dims.ok
    assert "empty" in dims.detail


# ── report shape + CLI ──────────────────────────────────────────────


def test_integrity_report_ok_property(tmp_path: Path):
    _init_file_corpus(tmp_path)
    _init_saga(tmp_path)
    report = check_all(tmp_path)
    assert report.ok
    assert len(report.failures) == 0

    # Introduce drift.
    conn = sqlite3.connect(str(tmp_path / ".mimir" / "index.db"))
    conn.execute("INSERT INTO files VALUES ('z.md', 'memory', 9.0, 90, 1, 'z')")
    conn.execute(
        "INSERT INTO chunks VALUES ('z.md', 0, 'z', ?)",
        (b"\0" * (384 * 4),),
    )
    conn.commit()
    conn.close()
    report = check_all(tmp_path)
    assert not report.ok
    assert len(report.failures) == 1
    assert report.failures[0].name == "fts5_sync_chunks_fts"


def test_render_includes_count_summary(tmp_path: Path):
    _init_file_corpus(tmp_path)
    _init_saga(tmp_path)
    report = check_all(tmp_path)
    out = report.render()
    assert "9/9 checks passed" in out


def test_run_verify_index_cmd_returns_0_on_clean(tmp_path: Path, capsys):
    _init_file_corpus(tmp_path)
    _init_saga(tmp_path)
    rc = run_verify_index_cmd(home=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "9/9 checks passed" in out


def test_run_verify_index_cmd_returns_1_on_failure(tmp_path: Path, capsys):
    # No DBs created → both report missing.
    rc = run_verify_index_cmd(home=tmp_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "0/2 checks passed" in out


def test_run_verify_index_cmd_filter_by_db(tmp_path: Path, capsys):
    _init_file_corpus(tmp_path)
    rc = run_verify_index_cmd(home=tmp_path, db="index")
    assert rc == 0
    out = capsys.readouterr().out
    # Only index checks ran.
    assert "[index]" in out
    assert "[saga]" not in out


# ── scheduled-job callable ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_check_emits_ok_on_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _init_file_corpus(tmp_path)
    _init_saga(tmp_path)
    events: list[tuple[str, dict]] = []

    async def _capture(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)
    from mimir.index_integrity import run_scheduled_integrity_check
    await run_scheduled_integrity_check(tmp_path)
    assert len(events) == 1
    kind, kw = events[0]
    assert kind == "index_integrity_ok"
    assert kw["checks"] == 9


@pytest.mark.asyncio
async def test_scheduled_check_emits_failed_with_failures_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # No DBs → both report missing → 2 failures.
    events: list[tuple[str, dict]] = []

    async def _capture(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)
    from mimir.index_integrity import run_scheduled_integrity_check
    await run_scheduled_integrity_check(tmp_path)
    assert len(events) == 1
    kind, kw = events[0]
    assert kind == "index_integrity_failed"
    assert kw["passed"] == 0
    assert kw["total"] == 2
    assert len(kw["failures"]) == 2
    names = {f["name"] for f in kw["failures"]}
    assert names == {"file_corpus_db_present", "saga_db_present"}
