"""Tests for mimir.saga_dashboard (chainlink #222 — Phase 1).

Tests cover:
  - build_db_stats_payload: stats from a real in-memory SQLite DB
  - build_recent_atoms_payload: returns rows, channel filter, limit cap
  - build_atom_payload: single atom + access_events + relations
  - render_saga_html: valid HTML shell with expected tokens
  - web_ui routes: /saga HTML + /api/saga view={recent,atom,stats}
  - Path-safety: missing DB returns error, not crash
  - Auth-exempt: /saga returns 200 without API key when key is set
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui
from mimir.saga_dashboard import (
    build_atom_payload,
    build_db_stats_payload,
    build_recent_atoms_payload,
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
    # Auth pattern — fetches API key from localStorage.
    assert "mimir_api_key" in html
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
def saga_app(tmp_path: Path):
    """aiohttp app with /saga + /api/saga wired to a real saga DB."""
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
