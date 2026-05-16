"""Tests for mimir.memory.migrate — saga.db → mimir.memory.db importer.

Builds a synthetic source DB matching a recent-saga schema (atoms +
access_log + atom_topics + atom_relations + triples + embeddings),
runs the migration, and validates each table landed correctly in
the destination + summaries got rebuilt.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from mimir.memory.migrate import migrate


def _build_saga_source(db_path: Path) -> None:
    """Create a saga-shaped source database with a few atoms, an
    access_log history, an atom_relations entry, and a triple. Mirrors
    the columns the importer reads — not a full saga schema."""
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            stream TEXT DEFAULT 'semantic',
            profile TEXT DEFAULT 'standard',
            memory_type TEXT DEFAULT 'raw',
            arousal REAL DEFAULT 0.5,
            valence REAL DEFAULT 0.0,
            encoding_confidence REAL DEFAULT 0.7,
            topics TEXT DEFAULT '[]',
            source_type TEXT DEFAULT 'conversation',
            metadata TEXT DEFAULT '{}',
            agent_id TEXT DEFAULT 'default',
            session_id TEXT,
            is_pinned INTEGER DEFAULT 0,
            provisional INTEGER DEFAULT 0,
            state TEXT DEFAULT 'active',
            stability REAL DEFAULT 1.0,
            retrievability REAL DEFAULT 1.0,
            embedding BLOB
        );
        CREATE TABLE access_log (
            atom_id TEXT,
            accessed_at TEXT,
            activation_score REAL,
            retrieval_mode TEXT,
            session_id TEXT,
            contributed INTEGER
        );
        CREATE TABLE atom_topics (
            atom_id TEXT,
            topic TEXT,
            PRIMARY KEY (atom_id, topic)
        );
        CREATE TABLE atom_relations (
            source_id TEXT,
            target_id TEXT,
            relation_type TEXT,
            confidence REAL,
            created_at TEXT,
            metadata TEXT
        );
        CREATE TABLE triples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            source_atom_id TEXT,
            confidence REAL,
            valid_from TEXT,
            valid_until TEXT,
            state TEXT DEFAULT 'active',
            created_at TEXT,
            metadata TEXT
        );
    """)

    vec_a = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    vec_b = struct.pack("4f", 0.0, 1.0, 0.0, 0.0)
    vec_c = struct.pack("4f", 0.0, 0.0, 1.0, 0.0)

    c.executemany(
        "INSERT INTO atoms (id, content, content_hash, created_at, "
        "stream, memory_type, state, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("a1", "Alice prefers concise replies", "h1",
             "2026-05-01T00:00:00Z", "semantic", "raw", "active", vec_a),
            ("a2", "Alice prefers terse answers", "h2",
             "2026-05-02T00:00:00Z", "semantic", "raw", "active", vec_b),
            ("a3", "Bob enjoys verbose explanations", "h3",
             "2026-05-03T00:00:00Z", "semantic", "raw", "active", vec_c),
            ("a4", "tombstoned atom", "h4",
             "2026-05-04T00:00:00Z", "semantic", "raw", "tombstone", None),
            ("a5", "dormant atom (should keep)", "h5",
             "2026-05-05T00:00:00Z", "semantic", "raw", "dormant",
             struct.pack("4f", 0.5, 0.5, 0.0, 0.0)),
        ],
    )
    c.executemany(
        "INSERT INTO access_log (atom_id, accessed_at, retrieval_mode, "
        "session_id, contributed) VALUES (?, ?, ?, ?, ?)",
        [
            ("a1", "2026-05-01T00:00:01Z", "semantic", "s1", 0),
            ("a1", "2026-05-01T00:01:00Z", "semantic", "s1", 1),
            ("a2", "2026-05-02T00:00:01Z", "semantic", "s1", 0),
            ("a5", "2026-05-05T00:00:01Z", "semantic", "s2", 0),
        ],
    )
    c.executemany(
        "INSERT INTO atom_topics VALUES (?, ?)",
        [("a1", "preferences"), ("a2", "preferences"), ("a3", "preferences")],
    )
    c.executemany(
        "INSERT INTO atom_relations VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("a1", "a2", "supersedes", 1.0, "2026-05-02T00:01:00Z", "{}"),
            ("a3", "a4", "evidenced_by", 1.0, "2026-05-04T00:01:00Z", "{}"),
        ],
    )
    c.executemany(
        "INSERT INTO triples (subject, predicate, object, source_atom_id, "
        "confidence, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("Alice", "prefers", "concise_replies", "a1", 1.0, "active",
             "2026-05-01T00:00:00Z"),
            ("Bob", "enjoys", "verbose_explanations", "a3", 0.8, "active",
             "2026-05-03T00:00:00Z"),
        ],
    )
    c.commit()
    c.close()


def test_migrate_basic_atoms(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db, force=False)

    assert stats["migrated"] == 5  # a1..a5
    assert stats["tombstoned"] == 1  # a4

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute("SELECT id, tombstoned FROM atoms ORDER BY id").fetchall()
    ids = {r[0]: r[1] for r in rows}
    assert ids == {"a1": 0, "a2": 0, "a3": 0, "a4": 1, "a5": 0}
    dst.close()


def test_migrate_access_events(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    assert stats["access_events"] == 4  # 4 access_log rows

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute(
        "SELECT atom_id, source FROM access_events ORDER BY id"
    ).fetchall()
    # contributed=1 → feedback_positive; contributed=0 → retrieval
    sources = [r[1] for r in rows]
    assert "feedback_positive" in sources
    assert "retrieval" in sources
    dst.close()


def test_migrate_seeds_store_events_for_atoms_with_no_history(tmp_path: Path):
    """a3 has no access_log entries in the fixture. The migrator should
    synthesize one 'store' event so activation isn't -inf."""
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    assert stats["seeded_store_events"] >= 1

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute(
        "SELECT source FROM access_events WHERE atom_id = 'a3'"
    ).fetchall()
    assert ("store",) in rows
    dst.close()


def test_migrate_rebuilds_summaries(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    assert stats["summaries"] >= 4  # one per atom with events

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute(
        "SELECT atom_id, recent_ts_json FROM atom_access_summary"
    ).fetchall()
    # Every summary row has at least one recent timestamp.
    for atom_id, recent_ts_json in rows:
        import json
        ts = json.loads(recent_ts_json or "[]")
        assert ts, f"{atom_id} has empty recent_ts_json"
    dst.close()


def test_migrate_embeddings(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    # 4 atoms have embeddings; a4 is None.
    assert stats["embeddings"] == 4

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute(
        "SELECT atom_id, dim FROM embeddings ORDER BY atom_id"
    ).fetchall()
    for atom_id, dim in rows:
        assert dim == 4
    dst.close()


def test_migrate_relations(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    # 2 relations in source; both endpoints survive (a4 is tombstoned
    # but still in atoms), so both should migrate.
    assert stats["atom_relations"] == 2

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute(
        "SELECT source_id, target_id, relation_type FROM atom_relations"
    ).fetchall()
    assert ("a1", "a2", "supersedes") in rows
    assert ("a3", "a4", "evidenced_by") in rows
    dst.close()


def test_migrate_triples(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    assert stats["triples"] == 2

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute(
        "SELECT subject, predicate, object FROM triples ORDER BY id"
    ).fetchall()
    assert ("Alice", "prefers", "concise_replies") in rows
    assert ("Bob", "enjoys", "verbose_explanations") in rows
    dst.close()


def test_migrate_topics(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)

    stats = migrate(source=src_db, dest=dst_db)
    assert stats["topics"] == 3

    dst = sqlite3.connect(str(dst_db))
    rows = dst.execute("SELECT atom_id, topic FROM atom_topics").fetchall()
    assert ("a1", "preferences") in rows
    dst.close()


def test_migrate_refuses_to_overwrite_without_force(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)
    dst_db.write_text("not empty")

    with pytest.raises(FileExistsError):
        migrate(source=src_db, dest=dst_db, force=False)


def test_migrate_force_overwrites(tmp_path: Path):
    src_db = tmp_path / "saga.db"
    dst_db = tmp_path / "mimir.memory.db"
    _build_saga_source(src_db)
    dst_db.write_text("not empty")

    stats = migrate(source=src_db, dest=dst_db, force=True)
    assert stats["migrated"] == 5


def test_migrate_handles_msam_era_source_without_extra_tables(tmp_path: Path):
    """MSAM snapshots lack atom_relations, triples, embedding_meta —
    importer should gracefully degrade, not crash."""
    src_db = tmp_path / "msam.db"
    c = sqlite3.connect(str(src_db))
    c.executescript("""
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            stream TEXT,
            profile TEXT,
            arousal REAL,
            valence REAL,
            encoding_confidence REAL,
            topics TEXT,
            source_type TEXT,
            metadata TEXT,
            agent_id TEXT,
            session_id TEXT,
            is_pinned INTEGER,
            provisional INTEGER,
            state TEXT,
            stability REAL,
            retrievability REAL,
            embedding BLOB
        );
        CREATE TABLE access_log (
            atom_id TEXT, accessed_at TEXT, activation_score REAL,
            retrieval_mode TEXT, session_id TEXT, contributed INTEGER
        );
        CREATE TABLE atom_topics (atom_id TEXT, topic TEXT);
    """)
    c.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, state) "
        "VALUES ('m1', 'msam atom', 'h1', '2026-04-01T00:00:00Z', 'active')",
    )
    c.commit()
    c.close()

    dst_db = tmp_path / "out.db"
    stats = migrate(source=src_db, dest=dst_db)
    assert stats["migrated"] == 1
    assert stats["atom_relations"] == 0
    assert stats["triples"] == 0
