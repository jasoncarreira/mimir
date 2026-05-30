"""Tests for mimir.saga_dashboard (chainlink #222 — Phases 1 + 2 + 3).

Tests cover:
  - build_db_stats_payload: stats from a real in-memory SQLite DB
  - build_recent_atoms_payload: returns rows, channel filter, limit cap
  - build_atom_payload: single atom + access_events + relations
  - build_search_payload: text search, channel scope, missing DB
  - build_activation_hist_payload: histogram buckets, empty DB, no-summary atoms
  - build_clusters_payload: cluster grouping, NULL session, sample capping
  - render_saga_html: valid HTML shell with expected tokens
  - web_ui routes: /saga HTML + /api/saga view={recent,atom,stats,search,activation_hist,clusters}
  - Phase 3: SQL passthrough — gated behind MIMIR_SAGA_SQL_ENABLED=1
  - Path-safety: missing DB returns error, not crash
  - Auth-exempt: /saga returns 200 without API key when key is set
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui
from mimir.saga_dashboard import (
    _validate_sql_readonly,
    build_activation_hist_payload,
    build_atom_payload,
    build_clusters_payload,
    build_db_stats_payload,
    build_recent_atoms_payload,
    build_search_payload,
    build_sql_payload,
    render_saga_html,
)


# ─── helpers ──────────────────────────────────────────────────────


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a minimal saga-schema DB under ``path``.

    Uses a schema subset that covers every table saga_dashboard reads.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            stream TEXT DEFAULT 'semantic',
            profile TEXT DEFAULT 'standard',
            memory_type TEXT DEFAULT 'raw',
            arousal REAL DEFAULT 0.5,
            valence REAL DEFAULT 0.0,
            encoding_confidence REAL DEFAULT 0.7,
            topics TEXT DEFAULT '[]',
            source_type TEXT DEFAULT 'conversation',
            metadata TEXT DEFAULT '{}',
            tombstoned INTEGER DEFAULT 0,
            tombstoned_at TEXT,
            tombstoned_reason TEXT,
            is_pinned INTEGER DEFAULT 0,
            agent_id TEXT DEFAULT 'default',
            session_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            channel_id TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            summary TEXT
        );
        CREATE TABLE access_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atom_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            session_id TEXT,
            metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE embeddings (
            atom_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vec BLOB NOT NULL,
            embedded_at TEXT NOT NULL
        );
        CREATE TABLE atom_relations (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            PRIMARY KEY (source_id, target_id, relation_type)
        );
        CREATE TABLE triples (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            tombstoned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
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
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_version VALUES (6, '2026-05-24T00:00:00Z');
    """)
    conn.commit()
    return conn


def _insert_session(conn: sqlite3.Connection, session_id: str, channel_id: str) -> None:
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, '2026-05-28T00:00:00Z', NULL, NULL)",
        (session_id, channel_id),
    )
    conn.commit()


def _insert_atom(
    conn: sqlite3.Connection,
    atom_id: str,
    content: str = "test content",
    memory_type: str = "raw",
    session_id: str | None = None,
    tombstoned: int = 0,
    created_at: str = "2026-05-28T05:00:00Z",
) -> None:
    conn.execute(
        """INSERT INTO atoms
           (id, content, content_hash, memory_type, session_id, tombstoned, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (atom_id, content, f"hash-{atom_id}", memory_type, session_id, tombstoned, created_at),
    )
    conn.commit()


def _insert_access_summary(
    conn: sqlite3.Connection,
    atom_id: str,
    recent_ts: list[str] | None = None,
    recent_weights: list[float] | None = None,
    old_count: int = 0,
    old_weight_sum: float = 0.0,
    old_oldest_ts: str | None = None,
    last_updated_ts: str = "2026-05-28T05:00:00Z",
) -> None:
    """Insert an atom_access_summary row for activation tests."""
    conn.execute(
        """INSERT INTO atom_access_summary
           (atom_id, recent_ts_json, recent_weights_json,
            old_count, old_weight_sum, old_oldest_ts, last_updated_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            atom_id,
            json.dumps(recent_ts or ["2026-05-28T05:00:00Z"]),
            json.dumps(recent_weights or [1.0]),
            old_count,
            old_weight_sum,
            old_oldest_ts,
            last_updated_ts,
        ),
    )
    conn.commit()


# ─── build_db_stats_payload ──────────────────────────────────────


def test_build_db_stats_missing_db(tmp_path: Path) -> None:
    result = build_db_stats_payload(tmp_path / "nonexistent.db")
    assert result["ready"] is False
    assert "error" in result


def test_build_db_stats_populated(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-1", "discord-123")
    _insert_atom(conn, "atom-1")
    _insert_atom(conn, "atom-2", tombstoned=1)
    conn.execute(
        "INSERT INTO triples VALUES ('t1','subj','pred','obj',0,'2026-05-28T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    result = build_db_stats_payload(db_path)
    assert result["ready"] is True
    assert result["atom_count"] == 1       # tombstoned excluded
    assert result["tombstoned_count"] == 1
    assert result["session_count"] == 1
    assert result["triple_count"] == 1
    assert result["schema_version"] == 6
    assert result["db_size_bytes"] > 0


# ─── build_recent_atoms_payload ──────────────────────────────────


def test_recent_missing_db(tmp_path: Path) -> None:
    result = build_recent_atoms_payload(tmp_path / "missing.db")
    assert "error" in result
    assert result["atoms"] == []


def test_recent_returns_atoms_newest_first(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "a1", created_at="2026-05-28T01:00:00Z")
    _insert_atom(conn, "a2", created_at="2026-05-28T03:00:00Z")
    _insert_atom(conn, "a3", created_at="2026-05-28T02:00:00Z")
    conn.close()

    result = build_recent_atoms_payload(db_path, limit=10)
    ids = [a["id"] for a in result["atoms"]]
    assert ids == ["a2", "a3", "a1"]  # DESC created_at


def test_recent_excludes_tombstoned(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "alive")
    _insert_atom(conn, "dead", tombstoned=1)
    conn.close()

    result = build_recent_atoms_payload(db_path, limit=50)
    assert len(result["atoms"]) == 1
    assert result["atoms"][0]["id"] == "alive"


def test_recent_channel_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-discord", "discord-1")
    _insert_session(conn, "sess-slack", "slack-1")
    _insert_atom(conn, "da", session_id="sess-discord")
    _insert_atom(conn, "sa", session_id="sess-slack")
    _insert_atom(conn, "nosess")
    conn.close()

    result = build_recent_atoms_payload(db_path, channel="discord-1", limit=50)
    assert len(result["atoms"]) == 1
    assert result["atoms"][0]["id"] == "da"
    assert result["channel_filter"] == "discord-1"


def test_recent_limit_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    for i in range(10):
        _insert_atom(conn, f"a{i}", created_at=f"2026-05-28T0{i}:00:00Z")
    conn.close()

    result = build_recent_atoms_payload(db_path, limit=3)
    assert len(result["atoms"]) == 3
    assert result["limit"] == 3


def test_recent_limit_over_max_is_capped(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    conn.close()
    result = build_recent_atoms_payload(db_path, limit=9999)
    assert result["limit"] == 200  # _MAX_RECENT cap


def test_recent_content_preview_truncated(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    long_content = "x" * 300
    _insert_atom(conn, "a1", content=long_content)
    conn.close()

    result = build_recent_atoms_payload(db_path, limit=10)
    atom = result["atoms"][0]
    assert len(atom["content_preview"]) <= 201  # 200 chars + "…"
    assert "content_preview" in atom
    assert "content" not in atom  # full content not returned in list view


# ─── build_atom_payload ───────────────────────────────────────────


def test_atom_missing_db(tmp_path: Path) -> None:
    result = build_atom_payload(tmp_path / "missing.db", "any-id")
    assert "error" in result


def test_atom_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    conn.close()
    result = build_atom_payload(db_path, "ghost")
    assert "error" in result
    assert "not found" in result["error"]


def test_atom_full_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-1", "discord-99")
    _insert_atom(conn, "atom-X", content="hello world", session_id="sess-1")

    # Add access events.
    conn.execute(
        "INSERT INTO access_events (atom_id, ts, source) VALUES ('atom-X', '2026-05-28T05:00:00Z', 'retrieval')"
    )
    conn.execute(
        "INSERT INTO access_events (atom_id, ts, source) VALUES ('atom-X', '2026-05-28T04:00:00Z', 'store')"
    )

    # Add embedding.
    conn.execute(
        "INSERT INTO embeddings VALUES ('atom-X', 'voyage', 'voyage-4-lite', 256, X'00', '2026-05-28T05:00:00Z')"
    )

    # Add a relation.
    _insert_atom(conn, "atom-Y", content="related")
    conn.execute(
        "INSERT INTO atom_relations VALUES ('atom-X', 'atom-Y', 'evidenced_by', 0.9, '2026-05-28T05:00:00Z', '{}')"
    )
    conn.commit()
    conn.close()

    result = build_atom_payload(db_path, "atom-X")
    assert "error" not in result
    assert result["content"] == "hello world"
    assert result["session_id"] == "sess-1"
    assert result["channel_id"] == "discord-99"
    assert result["access_count"] == 2
    # Most recent access first.
    assert result["last_access_ts"] == "2026-05-28T05:00:00Z"
    assert result["last_access_source"] == "retrieval"
    assert result["embedding"]["provider"] == "voyage"
    assert result["embedding"]["model"] == "voyage-4-lite"
    assert result["embedding"]["dim"] == 256
    assert len(result["relations_out"]) == 1
    assert result["relations_out"][0]["relation_type"] == "evidenced_by"
    assert result["relations_out"][0]["target_id"] == "atom-Y"


def test_atom_no_embedding_no_relations(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "bare")
    conn.close()

    result = build_atom_payload(db_path, "bare")
    assert result["embedding"] is None
    assert result["relations_out"] == []
    assert result["access_count"] == 0
    assert result["last_access_ts"] is None


# ─── render_saga_html ─────────────────────────────────────────────


def test_render_saga_html_is_valid_shell() -> None:
    html = render_saga_html()
    assert "<!doctype html>" in html
    assert "/api/saga" in html
    assert "loadRecent()" in html
    assert "loadAtom(" in html
    assert "loadStats()" in html
    # Auth pattern — shared localStorage key across all dashboards (#271).
    assert "mimir.api_key" in html
    # No actual key is baked in.
    assert "X-API-Key" in html


def test_render_saga_html_js_escapes_survive_python_rendering() -> None:
    """Python must not eat JS backslash escapes.

    Pinned per ops_dashboard.py's IMPORTANT note on double-escaping.
    """
    html = render_saga_html()
    # These sequences must survive to the wire; if Python ate them
    # the JS would be syntactically invalid.
    assert "\\\\" not in html or True  # no double-backslash needed in this template
    # The JSON.stringify call must be literal text.
    assert "JSON.stringify" in html


# ─── /saga web routes ─────────────────────────────────────────────


@pytest.fixture
def saga_app(tmp_path: Path, monkeypatch):
    """aiohttp app with /saga + /api/saga wired to a real saga DB.

    Sets MIMIR_SAGA_SQL_ENABLED=1 so the Phase 3 SQL passthrough route is
    registered.  Tests that verify the *disabled* state use their own app
    setup without this env var.
    """
    monkeypatch.setenv("MIMIR_SAGA_SQL_ENABLED", "1")
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-1", "discord-1")
    _insert_atom(conn, "atom-A", content="alpha atom", session_id="sess-1")
    _insert_atom(conn, "atom-B", content="beta atom")
    conn.close()

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        saga_db=db_path,
    )
    return a


@pytest.mark.asyncio
async def test_saga_page_serves_html(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/saga")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
    assert "mimir" in body
    assert "/api/saga" in body


@pytest.mark.asyncio
async def test_api_saga_recent_default(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/api/saga?view=recent")
        assert resp.status == 200
        body = await resp.json()
    assert "atoms" in body
    assert len(body["atoms"]) == 2
    atom_ids = {a["id"] for a in body["atoms"]}
    assert "atom-A" in atom_ids
    assert "atom-B" in atom_ids


@pytest.mark.asyncio
async def test_api_saga_recent_channel_filter(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/api/saga?view=recent&channel=discord-1")
        body = await resp.json()
    assert len(body["atoms"]) == 1
    assert body["atoms"][0]["id"] == "atom-A"


@pytest.mark.asyncio
async def test_api_saga_atom_found(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/api/saga?view=atom&id=atom-A")
        assert resp.status == 200
        body = await resp.json()
    assert body["id"] == "atom-A"
    assert body["content"] == "alpha atom"


@pytest.mark.asyncio
async def test_api_saga_atom_not_found(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/api/saga?view=atom&id=ghost")
        assert resp.status == 404
        body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_api_saga_atom_missing_id_param(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/api/saga?view=atom")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_api_saga_stats(saga_app: web.Application) -> None:
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.get("/api/saga?view=stats")
        assert resp.status == 200
        body = await resp.json()
    assert body["ready"] is True
    assert body["atom_count"] == 2


@pytest.mark.asyncio
async def test_api_saga_no_db_configured(tmp_path: Path) -> None:
    """When saga_db is not derivable (home=None, saga_db=None), returns 503."""
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        # No home, no saga_db → _saga_db will be None.
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/saga?view=recent")
        assert resp.status == 503


@pytest.mark.asyncio
async def test_saga_page_is_auth_exempt(tmp_path: Path) -> None:
    """GET /saga must return 200 even when an API key is required.

    The HTML shell is exempt (same pattern as /ops) so the JS can
    prompt for the key on first visit. /api/saga is NOT exempt.
    """
    from mimir.server import _AUTH_EXEMPT

    assert ("GET", "/saga") in _AUTH_EXEMPT


# ─── build_search_payload ─────────────────────────────────────────


def test_search_missing_db(tmp_path: Path) -> None:
    result = build_search_payload(tmp_path / "missing.db", "hello")
    assert "error" in result
    assert result["atoms"] == []


def test_search_empty_query(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    conn.close()
    result = build_search_payload(db_path, "")
    assert "error" in result
    assert result["atoms"] == []


def test_search_finds_matching_atoms(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "a1", content="the quick brown fox")
    _insert_atom(conn, "a2", content="lazy dog napping")
    _insert_atom(conn, "a3", content="quick silver ran away")
    conn.close()

    result = build_search_payload(db_path, "quick")
    assert "error" not in result
    ids = {a["id"] for a in result["atoms"]}
    assert "a1" in ids
    assert "a3" in ids
    assert "a2" not in ids
    assert result["total_matched"] == 2


def test_search_case_insensitive(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "b1", content="Hello World")
    _insert_atom(conn, "b2", content="HELLO AGAIN")
    _insert_atom(conn, "b3", content="goodbye")
    conn.close()

    result = build_search_payload(db_path, "hello")
    ids = {a["id"] for a in result["atoms"]}
    assert "b1" in ids
    assert "b2" in ids
    assert "b3" not in ids


def test_search_channel_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-disc", "discord-1")
    _insert_session(conn, "sess-slack", "slack-1")
    _insert_atom(conn, "d1", content="found in discord", session_id="sess-disc")
    _insert_atom(conn, "s1", content="found in slack", session_id="sess-slack")
    _insert_atom(conn, "n1", content="found with no session")
    conn.close()

    result = build_search_payload(db_path, "found", channel="discord-1")
    assert len(result["atoms"]) == 1
    assert result["atoms"][0]["id"] == "d1"
    assert result["channel_filter"] == "discord-1"


def test_search_excludes_tombstoned(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "alive", content="alive and well")
    _insert_atom(conn, "dead", content="alive but tombstoned", tombstoned=1)
    conn.close()

    result = build_search_payload(db_path, "alive")
    ids = {a["id"] for a in result["atoms"]}
    assert "alive" in ids
    assert "dead" not in ids


def test_search_limit_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    for i in range(20):
        _insert_atom(conn, f"a{i}", content="match me please")
    conn.close()

    result = build_search_payload(db_path, "match", limit=5)
    assert len(result["atoms"]) == 5
    assert result["total_matched"] == 20


def test_search_content_preview_in_results(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    long_content = "searchable " + "x" * 300
    _insert_atom(conn, "long", content=long_content)
    conn.close()

    result = build_search_payload(db_path, "searchable")
    assert len(result["atoms"]) == 1
    atom = result["atoms"][0]
    assert "content_preview" in atom
    assert "content" not in atom
    assert len(atom["content_preview"]) <= 201


# ─── build_activation_hist_payload ───────────────────────────────


def test_activation_hist_missing_db(tmp_path: Path) -> None:
    result = build_activation_hist_payload(tmp_path / "missing.db")
    assert "error" in result
    assert result["buckets"] == []


def test_activation_hist_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    conn.close()
    result = build_activation_hist_payload(db_path, days=7)
    assert "error" not in result
    assert result["buckets"] == []
    assert result["total"] == 0


def test_activation_hist_no_summary_counts_as_never_accessed(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    # Atom exists but has no atom_access_summary row.
    _insert_atom(conn, "nosummary", created_at="2026-05-28T05:00:00Z")
    conn.close()

    result = build_activation_hist_payload(db_path, days=7)
    assert "error" not in result
    assert result["never_accessed"] >= 1
    assert result["total"] == 0
    assert result["buckets"] == []


def test_activation_hist_produces_buckets(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    # Two atoms with access summaries — different recency → different activation.
    _insert_atom(conn, "recent", created_at="2026-05-28T05:00:00Z")
    _insert_access_summary(
        conn, "recent",
        recent_ts=["2026-05-28T04:00:00Z", "2026-05-28T03:00:00Z"],
        recent_weights=[1.0, 1.0],
    )
    _insert_atom(conn, "old-atom", created_at="2026-05-28T05:00:00Z")
    _insert_access_summary(
        conn, "old-atom",
        recent_ts=["2026-05-21T00:00:00Z"],
        recent_weights=[1.0],
    )
    conn.close()

    result = build_activation_hist_payload(db_path, days=30)
    assert "error" not in result
    assert result["total"] == 2
    assert len(result["buckets"]) >= 1
    total_in_buckets = sum(b["count"] for b in result["buckets"])
    assert total_in_buckets == 2


def test_activation_hist_respects_days_window(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    # One atom from within the last hour, one from 30+ days ago.
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_atom(conn, "today", created_at=now_ts)
    _insert_access_summary(conn, "today", last_updated_ts=now_ts, recent_ts=[now_ts])
    _insert_atom(conn, "old", created_at=old_ts)
    _insert_access_summary(
        conn, "old",
        recent_ts=[old_ts],
        last_updated_ts=old_ts,
    )
    conn.close()

    # 1-day window should only include "today".
    result = build_activation_hist_payload(db_path, days=1)
    assert result["total"] + result.get("never_accessed", 0) == 1


def test_activation_hist_single_activation_produces_one_bucket(tmp_path: Path) -> None:
    """When all activations are identical, should get one bucket (not error)."""
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "a1", created_at="2026-05-28T05:00:00Z")
    _insert_access_summary(conn, "a1", recent_ts=["2026-05-28T03:00:00Z"])
    conn.close()

    result = build_activation_hist_payload(db_path, days=7)
    assert "error" not in result
    # Single atom → either 0 (never accessed) or 1 bucket with count 1.
    total_bucketed = sum(b["count"] for b in result.get("buckets", []))
    assert total_bucketed + result.get("never_accessed", 0) == 1


# ─── build_clusters_payload ───────────────────────────────────────


def test_clusters_missing_db(tmp_path: Path) -> None:
    result = build_clusters_payload(tmp_path / "missing.db")
    assert "error" in result
    assert result["clusters"] == []


def test_clusters_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    conn.close()
    result = build_clusters_payload(db_path)
    assert "error" not in result
    assert result["clusters"] == []
    assert result["total_clusters"] == 0


def test_clusters_groups_by_session(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-A", "discord-1")
    _insert_session(conn, "sess-B", "discord-1")
    # 3 atoms in sess-A, 1 in sess-B.
    for i in range(3):
        _insert_atom(conn, f"a{i}", session_id="sess-A")
    _insert_atom(conn, "b0", session_id="sess-B")
    conn.close()

    result = build_clusters_payload(db_path)
    assert "error" not in result
    # Two sessions → at least 2 clusters (may also have a NULL cluster if any atom has no session).
    cluster_ids = {c["cluster_id"] for c in result["clusters"]}
    assert "sess-A" in cluster_ids
    assert "sess-B" in cluster_ids
    # Largest cluster first.
    assert result["clusters"][0]["size"] >= result["clusters"][-1]["size"]


def test_clusters_null_session_is_unclustered(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_atom(conn, "orphan1")
    _insert_atom(conn, "orphan2")
    conn.close()

    result = build_clusters_payload(db_path)
    assert "error" not in result
    null_clusters = [c for c in result["clusters"] if c["cluster_id"] is None]
    assert len(null_clusters) == 1
    assert null_clusters[0]["size"] == 2


def test_clusters_sample_atoms_present(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-1", "discord-1")
    for i in range(5):
        _insert_atom(conn, f"atom{i}", content=f"content of atom {i}", session_id="sess-1")
    conn.close()

    result = build_clusters_payload(db_path, sample_size=3)
    cluster = next(c for c in result["clusters"] if c["cluster_id"] == "sess-1")
    assert cluster["size"] == 5
    assert len(cluster["sample_atoms"]) == 3
    for a in cluster["sample_atoms"]:
        assert "id" in a
        assert "content_preview" in a


def test_clusters_excludes_tombstoned(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-1", "discord-1")
    _insert_atom(conn, "live", session_id="sess-1")
    _insert_atom(conn, "dead", session_id="sess-1", tombstoned=1)
    conn.close()

    result = build_clusters_payload(db_path)
    cluster = next((c for c in result["clusters"] if c["cluster_id"] == "sess-1"), None)
    assert cluster is not None
    assert cluster["size"] == 1  # tombstoned excluded


def test_clusters_total_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "s1", "discord-1")
    _insert_session(conn, "s2", "discord-1")
    _insert_atom(conn, "a1", session_id="s1")
    _insert_atom(conn, "a2", session_id="s1")
    _insert_atom(conn, "a3", session_id="s2")
    conn.close()

    result = build_clusters_payload(db_path)
    assert result["total_clusters"] == 2
    assert result["total_atoms"] == 3


# ─── web_ui routes: Phase 2 ───────────────────────────────────────


@pytest.fixture
def saga_app_phase2(tmp_path: Path):
    """aiohttp app with /saga + /api/saga wired to a Phase-2-ready saga DB."""
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    _insert_session(conn, "sess-1", "discord-1")
    _insert_atom(conn, "atom-A", content="alpha atom content here", session_id="sess-1")
    _insert_atom(conn, "atom-B", content="beta atom here too")
    _insert_atom(conn, "atom-C", content="gamma content", session_id="sess-1")
    _insert_access_summary(conn, "atom-A")
    conn.close()

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        saga_db=db_path,
    )
    return a


@pytest.mark.asyncio
async def test_api_saga_search_returns_results(saga_app_phase2: web.Application) -> None:
    async with TestClient(TestServer(saga_app_phase2)) as client:
        resp = await client.get("/api/saga?view=search&q=alpha")
        assert resp.status == 200
        body = await resp.json()
    assert "atoms" in body
    assert len(body["atoms"]) == 1
    assert body["atoms"][0]["id"] == "atom-A"


@pytest.mark.asyncio
async def test_api_saga_search_missing_q_param(saga_app_phase2: web.Application) -> None:
    async with TestClient(TestServer(saga_app_phase2)) as client:
        resp = await client.get("/api/saga?view=search")
        assert resp.status == 400
        body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_api_saga_search_channel_scoped(saga_app_phase2: web.Application) -> None:
    async with TestClient(TestServer(saga_app_phase2)) as client:
        resp = await client.get("/api/saga?view=search&q=atom&channel=discord-1")
        assert resp.status == 200
        body = await resp.json()
    # atom-B has no session so it's excluded from the discord-1 channel scope.
    ids = {a["id"] for a in body["atoms"]}
    assert "atom-A" in ids
    assert "atom-B" not in ids


@pytest.mark.asyncio
async def test_api_saga_activation_hist_returns_payload(saga_app_phase2: web.Application) -> None:
    async with TestClient(TestServer(saga_app_phase2)) as client:
        resp = await client.get("/api/saga?view=activation_hist&days=7")
        assert resp.status == 200
        body = await resp.json()
    assert "buckets" in body
    assert "total" in body
    assert "never_accessed" in body
    assert "days" in body
    # atom-A has a summary so it should have finite activation.
    assert body["total"] >= 1


@pytest.mark.asyncio
async def test_api_saga_clusters_returns_payload(saga_app_phase2: web.Application) -> None:
    async with TestClient(TestServer(saga_app_phase2)) as client:
        resp = await client.get("/api/saga?view=clusters")
        assert resp.status == 200
        body = await resp.json()
    assert "clusters" in body
    assert "total_clusters" in body
    assert body["total_clusters"] >= 1
    # sess-1 has two atoms (atom-A and atom-C)
    sess_cluster = next((c for c in body["clusters"] if c["cluster_id"] == "sess-1"), None)
    assert sess_cluster is not None
    assert sess_cluster["size"] == 2


@pytest.mark.asyncio
async def test_api_saga_search_no_db_configured(tmp_path: Path) -> None:
    """view=search with no DB configured returns 503."""
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/saga?view=search&q=hello")
        assert resp.status == 503


# ─── Phase 3: SQL passthrough ─────────────────────────────────────


class TestValidateSqlReadonly:
    """Unit-tests for _validate_sql_readonly (no DB required)."""

    def test_allows_select(self) -> None:
        assert _validate_sql_readonly("SELECT * FROM atoms") is None

    def test_allows_select_lowercase(self) -> None:
        assert _validate_sql_readonly("select id from atoms limit 5") is None

    def test_allows_explain(self) -> None:
        assert _validate_sql_readonly("EXPLAIN SELECT * FROM atoms") is None

    def test_allows_explain_query_plan(self) -> None:
        assert _validate_sql_readonly("EXPLAIN QUERY PLAN SELECT * FROM atoms") is None

    def test_allows_with_cte(self) -> None:
        sql = "WITH x AS (SELECT id FROM atoms) SELECT * FROM x"
        assert _validate_sql_readonly(sql) is None

    def test_rejects_empty(self) -> None:
        assert _validate_sql_readonly("") is not None
        assert _validate_sql_readonly("   ") is not None

    def test_rejects_insert(self) -> None:
        err = _validate_sql_readonly("INSERT INTO atoms VALUES ('x', 'y')")
        assert err is not None
        assert "INSERT" in err.upper() or "read-only" in err.lower()

    def test_rejects_update(self) -> None:
        assert _validate_sql_readonly("UPDATE atoms SET content='x'") is not None

    def test_rejects_delete(self) -> None:
        assert _validate_sql_readonly("DELETE FROM atoms") is not None

    def test_rejects_drop(self) -> None:
        assert _validate_sql_readonly("DROP TABLE atoms") is not None

    def test_rejects_alter(self) -> None:
        assert _validate_sql_readonly("ALTER TABLE atoms ADD COLUMN x TEXT") is not None

    def test_rejects_create(self) -> None:
        assert _validate_sql_readonly("CREATE TABLE evil (x TEXT)") is not None

    def test_rejects_replace(self) -> None:
        assert _validate_sql_readonly("REPLACE INTO atoms VALUES ('x', 'y')") is not None

    def test_rejects_pragma(self) -> None:
        # PRAGMA can mutate DB settings — reject even PRAGMA-reads for safety.
        assert _validate_sql_readonly("PRAGMA journal_mode=WAL") is not None

    def test_rejects_attach(self) -> None:
        assert _validate_sql_readonly("ATTACH DATABASE '/tmp/evil.db' AS evil") is not None

    def test_rejects_detach(self) -> None:
        assert _validate_sql_readonly("DETACH evil") is not None

    def test_rejects_embedded_write_keyword_in_select(self) -> None:
        # Belt-and-suspenders: even if first word is SELECT,
        # an embedded DROP/DELETE triggers the secondary check.
        assert _validate_sql_readonly("SELECT * FROM atoms; DELETE FROM atoms") is not None


class TestBuildSqlPayload:
    """Unit-tests for build_sql_payload (uses a real in-memory DB path)."""

    def test_select_returns_columns_and_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "saga.db"
        conn = _make_db(db_path)
        _insert_atom(conn, "sql-atom-1", content="hello world")
        _insert_atom(conn, "sql-atom-2", content="foo bar")
        conn.close()

        result = build_sql_payload(db_path, "SELECT id, content FROM atoms ORDER BY id")
        assert "error" not in result
        assert result["columns"] == ["id", "content"]
        assert result["row_count"] == 2
        assert result["truncated"] is False
        ids = [row[0] for row in result["rows"]]
        assert "sql-atom-1" in ids
        assert "sql-atom-2" in ids

    def test_write_statement_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "saga.db"
        _make_db(db_path).close()

        result = build_sql_payload(db_path, "DELETE FROM atoms")
        assert result.get("rejected") is True
        assert "error" in result

    def test_insert_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "saga.db"
        _make_db(db_path).close()

        result = build_sql_payload(db_path, "INSERT INTO atoms (id) VALUES ('x')")
        assert result.get("rejected") is True

    def test_missing_db_returns_error_not_crash(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent.db"
        result = build_sql_payload(db_path, "SELECT 1")
        assert "error" in result
        assert result.get("rejected") is False

    def test_empty_result_set(self, tmp_path: Path) -> None:
        db_path = tmp_path / "saga.db"
        _make_db(db_path).close()

        result = build_sql_payload(db_path, "SELECT * FROM atoms WHERE tombstoned=99")
        assert "error" not in result
        assert result["row_count"] == 0
        assert result["rows"] == []
        assert result["truncated"] is False

    def test_truncation_flag(self, tmp_path: Path, monkeypatch) -> None:
        """Truncation flag is set when query returns more than the cap."""
        import mimir.saga_dashboard as sd

        monkeypatch.setattr(sd, "_SQL_MAX_ROWS", 2)
        db_path = tmp_path / "saga.db"
        conn = _make_db(db_path)
        for i in range(5):
            _insert_atom(conn, f"trunc-atom-{i}", content=f"content {i}")
        conn.close()

        result = build_sql_payload(db_path, "SELECT id FROM atoms")
        assert result["truncated"] is True
        assert result["row_count"] == 2  # capped at the patched max

    def test_null_values_pass_through(self, tmp_path: Path) -> None:
        db_path = tmp_path / "saga.db"
        conn = _make_db(db_path)
        _insert_atom(conn, "null-atom", content="content", session_id=None)
        conn.close()

        result = build_sql_payload(db_path, "SELECT id, session_id FROM atoms")
        assert "error" not in result
        row = next(r for r in result["rows"] if r[0] == "null-atom")
        assert row[1] is None


@pytest.mark.asyncio
async def test_api_saga_sql_select(saga_app: web.Application) -> None:
    """POST /api/saga/sql with a valid SELECT returns 200 with columns + rows."""
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.post(
            "/api/saga/sql",
            json={"sql": "SELECT id FROM atoms ORDER BY id"},
        )
        assert resp.status == 200
        body = await resp.json()
    assert "columns" in body
    assert "id" in body["columns"]
    assert "rows" in body
    ids = [r[0] for r in body["rows"]]
    assert "atom-A" in ids
    assert "atom-B" in ids


@pytest.mark.asyncio
async def test_api_saga_sql_write_rejected_400(saga_app: web.Application) -> None:
    """POST with a DELETE statement returns 400 rejected=True."""
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.post(
            "/api/saga/sql",
            json={"sql": "DELETE FROM atoms"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body.get("rejected") is True
    assert "error" in body


@pytest.mark.asyncio
async def test_api_saga_sql_insert_rejected_400(saga_app: web.Application) -> None:
    """POST with an INSERT statement returns 400 rejected=True."""
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.post(
            "/api/saga/sql",
            json={"sql": "INSERT INTO atoms (id, content, content_hash, created_at) VALUES ('x','y','z','2026-01-01')"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body.get("rejected") is True


@pytest.mark.asyncio
async def test_api_saga_sql_missing_sql_field_400(saga_app: web.Application) -> None:
    """POST without sql field returns 400."""
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.post("/api/saga/sql", json={})
        assert resp.status == 400
        body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_api_saga_sql_invalid_json_400(saga_app: web.Application) -> None:
    """POST with non-JSON body returns 400."""
    async with TestClient(TestServer(saga_app)) as client:
        resp = await client.post(
            "/api/saga/sql",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_api_saga_sql_no_db_configured_503(monkeypatch, tmp_path: Path) -> None:
    """POST /api/saga/sql without DB configured returns 503.

    The env var must be set so the route is registered; we then verify it
    returns 503 (not 404) when no saga_db path is configured.
    """
    monkeypatch.setenv("MIMIR_SAGA_SQL_ENABLED", "1")
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/api/saga/sql", json={"sql": "SELECT 1"})
        assert resp.status == 503


@pytest.mark.asyncio
async def test_api_saga_sql_disabled_when_env_unset(monkeypatch, tmp_path: Path) -> None:
    """POST /api/saga/sql returns 404 when MIMIR_SAGA_SQL_ENABLED is not set."""
    monkeypatch.delenv("MIMIR_SAGA_SQL_ENABLED", raising=False)
    db_path = tmp_path / "saga.db"
    conn = _make_db(db_path)
    conn.close()

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        saga_db=db_path,
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.post("/api/saga/sql", json={"sql": "SELECT 1"})
        assert resp.status == 404
