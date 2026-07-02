from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mimir.cli import main
from mimir.index import build_memory_index, build_state_index, build_wiki_index
from mimir.memory_doctor import render_text, run_doctor


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_clean_home(home: Path) -> None:
    _write(
        home / "memory" / "core" / "00-identity.md",
        "<!-- desc: identity -->\n# Identity\n" + "x" * 250,
    )
    _write(
        home / "memory" / "issues" / "one.md",
        "<!-- desc: one issue -->\n# One\nbody",
    )
    _write(home / "memory" / "learnings-pending.md", "<!-- desc: pending learnings -->\n")
    _write(home / "memory" / "INDEX.md", build_memory_index(home))


def _init_saga_doctor_db(home: Path) -> Path:
    db_path = home / ".mimir" / "saga.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            stream TEXT DEFAULT 'semantic',
            memory_type TEXT DEFAULT 'raw',
            tombstoned INTEGER DEFAULT 0,
            is_pinned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE embeddings (
            atom_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vec BLOB NOT NULL,
            embedded_at TEXT NOT NULL
        );
        CREATE TABLE triples (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            source_atom_id TEXT,
            embedding BLOB,
            embedding_dim INTEGER,
            tombstoned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE access_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atom_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            weight REAL DEFAULT 1.0
        );
        CREATE TABLE atom_access_summary (
            atom_id TEXT PRIMARY KEY,
            recent_ts_json TEXT DEFAULT '[]',
            recent_weights_json TEXT DEFAULT '[]',
            old_count INTEGER DEFAULT 0,
            old_weight_sum REAL DEFAULT 0.0,
            old_oldest_ts TEXT,
            last_updated_ts TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _find(report, *, layer: str, check: str, path: str | None = None):
    matches = [
        f for f in report.findings
        if f.layer == layer and f.check == check and (path is None or f.path == path)
    ]
    assert matches
    return matches[0]


def test_missing_core_desc_header_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "memory" / "core" / "00-identity.md", "# Identity\n" + "x" * 250)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="core",
        check="desc-header",
        path="memory/core/00-identity.md",
    )
    assert finding.severity == "warning"
    assert report.severity_counts["warning"] >= 1
    core = next(s for s in report.sections if s.name == "core")
    assert core.metrics["missing_desc_headers"] == 1


def test_real_channel_over_cap_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "channels" / "discord-1" / "notes.md", "x" * 8_300)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="channels",
        check="over-cap",
        path="memory/channels/discord-1",
    )
    assert finding.severity == "warning"
    channels = next(s for s in report.sections if s.name == "channels")
    assert channels.metrics["cap_bytes"] == 8_192
    assert channels.metrics["over_cap_directories"] == 1


def test_synthetic_channel_non_injection_note(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "channels" / "scheduler:heartbeat" / "notes.md", "x" * 9_000)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="channels",
        check="synthetic-non-injection",
        path="memory/channels/scheduler:heartbeat",
    )
    assert finding.severity == "info"
    assert not [f for f in report.findings if f.check == "over-cap"]


def test_stale_memory_index_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "topic.md", "<!-- desc: new topic -->\nbody")

    report = run_doctor(tmp_path)

    finding = _find(report, layer="index", check="stale", path="memory/INDEX.md")
    assert finding.severity == "warning"
    index = next(s for s in report.sections if s.name == "index")
    assert index.metrics["stale"] == 1


def test_stale_state_index_is_reported_without_rewriting(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "raw" / "one.md", "<!-- desc: one -->\nbody")
    _write(tmp_path / "state" / "INDEX.md", build_state_index(tmp_path))
    before = (tmp_path / "state" / "INDEX.md").read_text(encoding="utf-8")
    _write(tmp_path / "state" / "raw" / "two.md", "<!-- desc: two -->\nbody")

    report = run_doctor(tmp_path)

    finding = _find(report, layer="state", check="index-stale", path="state/INDEX.md")
    assert finding.severity == "warning"
    assert (tmp_path / "state" / "INDEX.md").read_text(encoding="utf-8") == before
    state = next(s for s in report.sections if s.name == "state")
    assert state.metrics["index_stale"] == 1


def test_stale_wiki_index_is_reported_without_rewriting(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "wiki" / "entities" / "alice.md", "<!-- desc: Alice -->\n[[bob]]")
    _write(tmp_path / "state" / "wiki" / "entities" / "bob.md", "<!-- desc: Bob -->\n[[alice]]")
    _write(tmp_path / "state" / "wiki" / "index.md", build_wiki_index(tmp_path))
    before = (tmp_path / "state" / "wiki" / "index.md").read_text(encoding="utf-8")
    _write(tmp_path / "state" / "wiki" / "entities" / "carol.md", "<!-- desc: Carol -->\n[[alice]]")

    report = run_doctor(tmp_path)

    finding = _find(report, layer="wiki", check="index-stale", path="state/wiki/index.md")
    assert finding.severity == "warning"
    assert (tmp_path / "state" / "wiki" / "index.md").read_text(encoding="utf-8") == before
    wiki = next(s for s in report.sections if s.name == "wiki")
    assert wiki.metrics["index_stale"] == 1


def test_wiki_orphan_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "wiki" / "concepts" / "lonely.md", "<!-- desc: Lonely -->\n# Lonely")
    _write(tmp_path / "state" / "wiki" / "index.md", build_wiki_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="wiki",
        check="orphan",
        path="state/wiki/concepts/lonely.md",
    )
    assert finding.severity == "warning"
    wiki = next(s for s in report.sections if s.name == "wiki")
    assert wiki.metrics["orphans"] == 1


def test_wiki_dangling_link_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "wiki" / "topics" / "source.md", "<!-- desc: Source -->\n[[ghost]]")
    _write(tmp_path / "state" / "wiki" / "topics" / "target.md", "<!-- desc: Target -->\n[[source]]")
    _write(tmp_path / "state" / "wiki" / "index.md", build_wiki_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="wiki",
        check="dangling-link",
        path="state/wiki/topics/source.md",
    )
    assert finding.severity == "warning"
    assert "[[ghost]]" in finding.message
    wiki = next(s for s in report.sections if s.name == "wiki")
    assert wiki.metrics["dangling_links"] == 1


def test_unexpected_top_level_state_markdown_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "voice-drafts.md", "<!-- desc: draft -->\nbody")
    _write(tmp_path / "state" / "INDEX.md", build_state_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="state",
        check="top-level-md",
        path="state/voice-drafts.md",
    )
    assert finding.severity == "warning"
    state = next(s for s in report.sections if s.name == "state")
    assert state.metrics["unexpected_top_level_md_files"] == 1


def test_overgrown_learnings_pending_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(
        tmp_path / "memory" / "learnings-pending.md",
        "\n".join(f"- item {i}" for i in range(220)),
    )
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="learnings-pending",
        check="overgrown",
        path="memory/learnings-pending.md",
    )
    assert finding.severity == "warning"
    section = next(s for s in report.sections if s.name == "learnings-pending")
    assert section.metrics["lines"] == 220
    assert section.metrics["overgrown"] == 1


def test_duplicate_issue_note_detection(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    body = "<!-- desc: duplicate incident -->\n# Incident\nsame facts"
    _write(tmp_path / "memory" / "issues" / "a.md", body)
    _write(tmp_path / "memory" / "issues" / "b.md", body)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="issues",
        check="obvious-duplicate",
        path="memory/issues/b.md",
    )
    assert finding.severity == "warning"
    issues = next(s for s in report.sections if s.name == "issues")
    assert issues.metrics["duplicate_files"] == 1


def test_json_output_is_stable(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "core" / "10-no-desc.md", "# Missing\n" + "x" * 250)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    payload = run_doctor(tmp_path).to_json()

    assert list(payload) == ["status", "severity_counts", "sections", "findings"]
    assert payload["status"] == "warning"
    assert list(payload["severity_counts"]) == ["error", "warning", "info"]
    assert {"layer", "check", "severity", "path", "message", "suggestion"} == set(
        payload["findings"][0]
    )
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def test_memory_doctor_cli_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)

    main(["memory", "doctor", "--home", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "warning"
    assert payload["severity_counts"] == {"error": 0, "warning": 1, "info": 0}


def test_saga_absent_db_reports_partial_success_in_text_and_json(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)

    report = run_doctor(tmp_path)
    saga = next(s for s in report.sections if s.name == "saga")

    assert saga.metrics == {"exists": 0}
    finding = _find(report, layer="saga", check="missing-db", path=".mimir/saga.db")
    assert finding.severity == "warning"
    assert "SAGA database is absent" in render_text(report)
    payload = report.to_json()
    assert payload["status"] == "warning"
    assert any(f["layer"] == "saga" and f["check"] == "missing-db" for f in payload["findings"])


def test_saga_stream_type_embedding_and_orphan_counts(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    db_path = _init_saga_doctor_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO atoms (id, content, stream, memory_type, tombstoned, is_pinned, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-07-01')",
        [
            ("a1", "semantic raw", "semantic", "raw", 0, 0),
            ("a2", "episodic observation", "episodic", "observation", 0, 1),
            ("a3", "tombstoned", "semantic", "raw", 1, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO embeddings (atom_id, provider, model, dim, vec, embedded_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-07-01')",
        [
            ("a1", "openai", "text-embedding-3-small", 1536, b"1" * 4),
            ("missing", "voyage", "voyage-4-lite", 1024, b"2" * 4),
        ],
    )
    conn.commit()
    conn.close()

    report = run_doctor(tmp_path)
    saga = next(s for s in report.sections if s.name == "saga")

    assert saga.metrics["exists"] == 1
    assert saga.metrics["atoms_total"] == 3
    assert saga.metrics["atoms_live"] == 2
    assert saga.metrics["atoms_tombstoned"] == 1
    assert saga.metrics["atoms_pinned"] == 1
    assert saga.metrics["atoms_stream_semantic"] == 2
    assert saga.metrics["atoms_stream_episodic"] == 1
    assert saga.metrics["atoms_memory_type_raw"] == 2
    assert saga.metrics["atoms_memory_type_observation"] == 1
    assert saga.metrics["embeddings_live_atoms_covered"] == 1
    assert saga.metrics["embeddings_live_atoms_missing"] == 1
    assert saga.metrics["embeddings_orphan_rows"] == 1
    assert saga.metrics["embeddings_provider_dim_openai_1536"] == 1
    assert saga.metrics["embeddings_provider_dim_voyage_1024"] == 1
    assert _find(report, layer="saga", check="missing-embeddings").severity == "warning"
    assert _find(report, layer="saga", check="orphan-embeddings").severity == "warning"


def test_saga_triple_access_summary_and_forget_preview_counts(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    db_path = _init_saga_doctor_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO atoms (id, content, stream, memory_type, tombstoned, is_pinned, created_at) "
        "VALUES (?, ?, 'semantic', 'raw', ?, 0, '2026-07-01')",
        [("a1", "live one", 0), ("a2", "live two", 0), ("a3", "dead", 1)],
    )
    conn.executemany(
        "INSERT INTO triples (id, subject, predicate, object, source_atom_id, embedding, embedding_dim, tombstoned, created_at) "
        "VALUES (?, 's', 'p', 'o', ?, ?, ?, ?, '2026-07-01')",
        [
            ("t1", "a1", b"vec", 3, 0),
            ("t2", "a2", None, None, 0),
            ("t3", "missing", None, None, 0),
            ("t4", "a1", None, None, 1),
        ],
    )
    conn.execute(
        "INSERT INTO access_events (atom_id, ts, source, weight) "
        "VALUES ('missing', '2026-07-01', 'retrieval', 1.0)"
    )
    conn.execute(
        "INSERT INTO atom_access_summary (atom_id, last_updated_ts) "
        "VALUES ('a1', '2026-07-01')"
    )
    conn.execute(
        "INSERT INTO atom_access_summary (atom_id, last_updated_ts) "
        "VALUES ('missing', '2026-07-01')"
    )
    conn.commit()
    conn.close()
    _write(
        tmp_path / "logs" / "events.jsonl",
        json.dumps({
            "timestamp": "2026-07-01T00:00:00+00:00",
            "type": "saga_decay_ok",
            "result": {"forgetting_candidates": 4},
        }) + "\n",
    )

    report = run_doctor(tmp_path)
    saga = next(s for s in report.sections if s.name == "saga")

    assert saga.metrics["triples_total"] == 4
    assert saga.metrics["triples_live"] == 3
    assert saga.metrics["triples_tombstoned"] == 1
    assert saga.metrics["triples_live_missing_embedding"] == 2
    assert saga.metrics["triples_orphan_source_atom"] == 1
    assert saga.metrics["access_events_orphan_rows"] == 1
    assert saga.metrics["access_summary_live_atoms_missing"] == 1
    assert saga.metrics["access_summary_orphan_rows"] == 1
    assert saga.metrics["forget_candidates_preview_available"] == 1
    assert saga.metrics["forget_candidates_pending"] == 4
    assert _find(report, layer="saga", check="missing-triple-embeddings").severity == "warning"
    assert _find(report, layer="saga", check="orphan-access-events").severity == "warning"
    assert _find(report, layer="saga", check="missing-access-summary").severity == "warning"


def test_saga_partial_old_db_skips_missing_tables(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    db_path = tmp_path / ".mimir" / "saga.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE atoms (id TEXT PRIMARY KEY, stream TEXT, memory_type TEXT, tombstoned INTEGER, is_pinned INTEGER)"
    )
    conn.execute(
        "INSERT INTO atoms (id, stream, memory_type, tombstoned, is_pinned) "
        "VALUES ('a1', 'semantic', 'raw', 0, 0)"
    )
    conn.commit()
    conn.close()

    report = run_doctor(tmp_path)
    saga = next(s for s in report.sections if s.name == "saga")

    assert saga.metrics["atoms_total"] == 1
    assert saga.metrics["table_embeddings_present"] == 0
    assert saga.metrics["table_triples_present"] == 0
    assert _find(report, layer="saga", check="missing-table-embeddings").severity == "warning"
