"""Turn viewer + log API routes (SPEC §11)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.config import Config
from mimir.dashboard_extensions import (
    DashboardExtensionManifest,
    first_party_dashboard_extensions,
)
from mimir.chainlink_board import build_chainlink_board_payload
from mimir import web_ui
from mimir.commitments.models import CommitmentRecord
from mimir.commitments.store import CommitmentsStore
from mimir.pollers import PollerConfig
from mimir.scheduler import Scheduler, SchedulerJob
from mimir.web_contracts import (
    render_typescript_contracts,
    validate_api_envelope,
    validate_live_event,
    validate_list_meta,
)


@pytest.fixture
def app(tmp_path: Path) -> tuple[web.Application, Path, Path]:
    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    a = web.Application()
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log)
    return a, turns_log, events_log


def test_generated_typescript_contracts_are_current():
    generated = Path("frontend/src/api/generated/contracts.ts").read_text(
        encoding="utf-8"
    )
    assert generated == render_typescript_contracts()


def test_global_dashboard_extensions_are_admin_only_in_nav_payload():
    manifests = {
        item["id"]: item
        for item in first_party_dashboard_extensions().navigation_payload()
    }

    assert manifests["ops"]["requires_role"] == "admin"
    assert manifests["chainlink-board"]["requires_role"] == "admin"
    assert manifests["scheduler"]["requires_role"] == "admin"
    # Endpoints are admin-gated (_ADMIN_REQUIRED_PREFIXES), so these nav entries
    # must be admin-only too or a non-admin sees a tab that only 403s.
    assert manifests["usage"]["requires_role"] == "admin"
    assert manifests["saga"]["requires_role"] == "admin"
    assert manifests["state-memory"]["requires_role"] == "admin"
    assert manifests["chat"]["requires_role"] is None


def test_dashboard_extension_registry_sorts_hides_and_validates_scope():
    registry = first_party_dashboard_extensions(
        [
            DashboardExtensionManifest(
                id="late",
                route_path="/late",
                label="Late",
                nav_position=20,
            ),
            DashboardExtensionManifest(
                id="early",
                route_path="/early",
                label="Early",
                nav_position=10,
            ),
            DashboardExtensionManifest(
                id="hidden",
                route_path="/hidden",
                label="Hidden",
                nav_position=1,
                enabled=False,
            ),
        ]
    )

    assert [manifest.id for manifest in registry.enabled()] == ["early", "late"]
    assert [item["id"] for item in registry.navigation_payload()] == ["early", "late"]

    with pytest.raises(ValueError, match="trusted first-party"):
        first_party_dashboard_extensions(
            [
                DashboardExtensionManifest(
                    id="remote",
                    route_path="/remote",
                    label="Remote",
                    trusted_first_party=False,
                )
            ]
        )


@pytest.mark.asyncio
async def test_chainlink_board_parses_cli_json_and_worklink_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    evidence_dir = home / "state" / "worklink" / "evidence"
    evidence_dir.mkdir(parents=True)
    (home / "state" / "worklink" / "transcripts").mkdir(parents=True)
    (evidence_dir / "545-2.json").write_text(
        json.dumps({
            "issue": 545,
            "attempt": 2,
            "backend": "codex",
            "status": "completed",
            "branch": "issue/545-a2",
            "diff_stat": "3 files changed",
            "tests": {"cmd": "npm test", "exit_code": 0},
            "transcript": "state/worklink/transcripts/545-2.jsonl",
            "pr_url": "https://example.test/pr/1",
        }),
        encoding="utf-8",
    )
    chainlink = bin_dir / "chainlink"
    chainlink.write_text(
        """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ["issue", "list"]:
    print(json.dumps([
        {"id": 524, "title": "Parent", "status": "open", "priority": "high", "labels": ["epic"], "updated_at": "2026-06-18T00:00:00Z"},
        {"id": 545, "title": "Board", "status": "open", "priority": "medium", "labels": ["worklink:review", "frontend"], "parent_id": 524, "blocked_by": [540], "updated_at": "2026-06-18T01:00:00Z"},
        {"id": 540, "title": "Prereq", "status": "closed", "priority": "low", "labels": [], "updated_at": "2026-06-17T00:00:00Z"}
    ]))
elif args[:2] == ["issue", "show"] and args[2] == "545":
    print(json.dumps({"id": 545, "description": "Acceptance criteria", "comments": [{"author": "mimir", "created_at": "2026-06-18T02:00:00Z", "body": "WORKLINK_EVIDENCE attached"}]}))
elif args[:2] == ["issue", "show"] and args[2] == "524":
    print(json.dumps({"id": 524, "subissues": [545]}))
elif args[:2] == ["issue", "show"] and args[2] == "540":
    print(json.dumps({"id": 540}))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    chainlink.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    payload = await build_chainlink_board_payload(home)

    assert payload["available"] is True
    board = next(issue for issue in payload["issues"] if issue["id"] == 545)
    parent = next(issue for issue in payload["issues"] if issue["id"] == 524)
    assert board["status"] == "review"
    assert board["blocked_by"] == [540]
    assert board["comments"][0]["body"] == "WORKLINK_EVIDENCE attached"
    assert board["worklink"]["attempt"] == 2
    assert board["worklink"]["evidence_href"].endswith("state/worklink/evidence/545-2.json")
    assert parent["child_progress"] == {"done": 0, "total": 1}
    assert {"from": 524, "to": 545, "kind": "parent"} in payload["edges"]
    assert {"from": 540, "to": 545, "kind": "blocks"} in payload["edges"]


@pytest.mark.asyncio
async def test_chainlink_board_degrades_when_cli_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    payload = await build_chainlink_board_payload(tmp_path)

    assert payload["available"] is False
    assert payload["issues"] == []
    assert "chainlink CLI not on PATH" in payload["error"]


def test_dashboard_extension_route_path_allows_app_prefix_words_only():
    DashboardExtensionManifest(
        id="apple",
        route_path="/apple",
        label="Apple",
    ).validate()
    DashboardExtensionManifest(
        id="applications",
        route_path="/applications",
        label="Applications",
    ).validate()

    with pytest.raises(ValueError, match="route_path"):
        DashboardExtensionManifest(
            id="app",
            route_path="/app",
            label="App",
        ).validate()
    with pytest.raises(ValueError, match="route_path"):
        DashboardExtensionManifest(
            id="app-child",
            route_path="/app/child",
            label="App Child",
        ).validate()


@pytest.mark.asyncio
async def test_turns_page_serves_html(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/turns")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        assert resp.headers["X-Mimir-Frontend"] == "legacy-html"
        assert resp.headers["Link"] == '</app>; rel="alternate"'
        body = await resp.text()
        assert "mimir turns" in body  # header title (renamed from "Turn Viewer")
        assert "/api/turns" in body  # the page polls this endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/turns", "/ops", "/saga", "/state"])
async def test_legacy_html_routes_are_marked(path, tmp_path: Path):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        home=tmp_path,
        saga_db=tmp_path / "saga.db",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get(path)

    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.headers["X-Mimir-Frontend"] == "legacy-html"
    assert resp.headers["Link"] == '</app>; rel="alternate"'


@pytest.mark.asyncio
async def test_api_turns_returns_records(app):
    a, turns_log, _ = app
    rows = [
        {"turn_id": "t1", "channel_id": "c1", "output": "hi"},
        {"turn_id": "t2", "channel_id": "c1", "output": "ok"},
    ]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns")
        body = await resp.json()
    assert [t["turn_id"] for t in body["turns"]] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_api_v1_turns_returns_envelope_and_list_metadata(app):
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/turns?limit=2")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    validate_list_meta(body["meta"])
    assert [t["turn_id"] for t in body["data"]["turns"]] == ["t3", "t4"]
    assert body["meta"] == {
        "cursor": "t4",
        "limit": 2,
        # Cursor-limited turn pages intentionally do not count the full log;
        # exact totals require decoding every retained row and defeat
        # progressive loading on large turns.jsonl files.
        "total": None,
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_api_v1_turns_offloads_tail_read(monkeypatch, app):
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    calls = []
    real_to_thread = web_ui.asyncio.to_thread

    async def fake_to_thread(func, *args, **kwargs):
        calls.append(func.__name__)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(web_ui.asyncio, "to_thread", fake_to_thread)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/turns?limit=2")
        body = await resp.json()

    assert resp.status == 200
    assert [t["turn_id"] for t in body["data"]["turns"]] == ["t3", "t4"]
    assert "_turns_response" in calls


@pytest.mark.asyncio
async def test_api_v1_sessions_groups_missing_saga_session_id_by_channel_time(tmp_path: Path):
    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    rows = [
        {
            "turn_id": "t1",
            "ts": "2026-06-18T10:00:00Z",
            "trigger": "user_message",
            "channel_id": "web-a",
            "input": "alpha prompt",
            "output": "alpha answer",
        },
        {
            "turn_id": "t2",
            "ts": "2026-06-18T10:05:00Z",
            "trigger": "user_message",
            "channel_id": "web-a",
            "input": "follow up",
            "output": "still alpha",
        },
        {
            "turn_id": "t3",
            "ts": "2026-06-18T11:00:00Z",
            "trigger": "scheduled_tick",
            "channel_id": "web-a",
            "input": "later",
            "output": "later answer",
        },
    ]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    a = web.Application()
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log, home=home)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/sessions?q=alpha&channel=web-a&trigger=user_message")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["meta"]["total"] == 1
    [session] = body["data"]["sessions"]
    assert session["synthetic"] is True
    assert session["saga_session_id"] is None
    assert session["turn_ids"] == ["t1", "t2"]
    assert session["triggers"] == ["user_message"]


@pytest.mark.asyncio
async def test_api_v1_sessions_includes_synthesis_only_saga_summary(tmp_path: Path):
    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    home = tmp_path / "home"
    saga_dir = home / ".mimir"
    saga_dir.mkdir(parents=True)
    saga_db = saga_dir / "saga.db"
    conn = sqlite3.connect(saga_db)
    try:
        conn.executescript((Path("mimir/saga/schema.sql")).read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO sessions (
                id, channel_id, started_at, ended_at, summary, reflected_at,
                topics_discussed, decisions_made, unfinished, closed_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "saga-web-a-1",
                "web-a",
                "2026-06-18T10:00:00Z",
                "2026-06-18T10:30:00Z",
                "Synthesized without retained turn records.",
                "2026-06-18T10:31:00Z",
                json.dumps(["browser"]),
                json.dumps([]),
                json.dumps(["wire detail view"]),
                json.dumps([]),
            ),
        )
        conn.execute(
            """
            INSERT INTO atoms (
                id, content, content_hash, source_type, created_at, session_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "atom-1",
                "Session atom tied to synthesis-only browser work.",
                "hash-1",
                "conversation",
                "2026-06-18T10:32:00Z",
                "saga-web-a-1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    a = web.Application()
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log, home=home)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/sessions?q=retained")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    [session] = body["data"]["sessions"]
    assert session["id"] == "saga-web-a-1"
    assert session["turn_ids"] == []
    assert session["summary"] == "Synthesized without retained turn records."
    assert session["unfinished"] == ["wire detail view"]
    assert session["related_saga_atoms"][0]["id"] == "atom-1"


@pytest.mark.asyncio
async def test_api_turns_with_after_filter(app):
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns?after=t2")
        body = await resp.json()
    # Strictly after t2 — t3, t4.
    assert [t["turn_id"] for t in body["turns"]] == ["t3", "t4"]


@pytest.mark.asyncio
async def test_api_turns_limit_returns_newest_page(app):
    """Progressive loading: ?limit=N returns the newest N turns (file tail)."""
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns?limit=2")
        body = await resp.json()
    # Newest 2 (file is oldest-first; tail is t3, t4).
    assert [t["turn_id"] for t in body["turns"]] == ["t3", "t4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/turns?limit=2", "/api/v1/turns?limit=2"])
async def test_api_turns_limit_stops_tail_reading_after_page(monkeypatch, app, path):
    """React/legacy progressive loads must not decode every retained turn."""
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    calls = 0
    real_tail = web_ui.tail_jsonl_records

    def counting_tail(path_arg):
        nonlocal calls
        for record in real_tail(path_arg):
            calls += 1
            yield record

    monkeypatch.setattr(web_ui, "tail_jsonl_records", counting_tail)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get(path)
        body = await resp.json()

    turns = body.get("turns") or body["data"]["turns"]
    assert [t["turn_id"] for t in turns] == ["t3", "t4"]
    assert calls == 2


@pytest.mark.asyncio
async def test_api_turns_before_returns_older_page(app):
    """Progressive loading: ?before=<id>&limit=N returns up to N turns
    immediately OLDER than the cursor (scroll-back page)."""
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(6)]  # t0..t5
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    async with TestClient(TestServer(a)) as client:
        # Two turns older than t4 -> t2, t3.
        resp = await client.get("/api/turns?before=t4&limit=2")
        body = await resp.json()
        # Unknown cursor -> empty (treated as "no older page").
        resp2 = await client.get("/api/turns?before=nope&limit=2")
        body2 = await resp2.json()
    assert [t["turn_id"] for t in body["turns"]] == ["t2", "t3"]
    assert body2["turns"] == []


@pytest.mark.asyncio
async def test_api_turns_handles_missing_file(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns")
        body = await resp.json()
    assert body == {"turns": []}


@pytest.mark.asyncio
async def test_api_events_filters_by_type_and_limit(app):
    a, _, events_log = app
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_started"},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "tool_call"},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "tool_call"},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "turn_finished"},
    ]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/events?type=tool_call")
        body = await resp.json()
        assert all(e["type"] == "tool_call" for e in body["events"])
        assert len(body["events"]) == 2

        # Multiple types via repeated query param.
        resp = await client.get("/api/events?type=turn_started&type=turn_finished")
        body = await resp.json()
        assert {e["type"] for e in body["events"]} == {"turn_started", "turn_finished"}

        # Comma-joined form should work too.
        resp = await client.get("/api/events?type=turn_started,turn_finished")
        body = await resp.json()
        assert {e["type"] for e in body["events"]} == {"turn_started", "turn_finished"}

        # Limit returns the tail.
        resp = await client.get("/api/events?limit=2")
        body = await resp.json()
        assert [e["type"] for e in body["events"]] == ["tool_call", "turn_finished"]

        # since= drops anything before the timestamp.
        resp = await client.get("/api/events?since=2026-01-01T00:00:02Z")
        body = await resp.json()
        assert [e["type"] for e in body["events"]] == ["tool_call", "turn_finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/events?limit=2", "/api/v1/events?limit=2"])
async def test_api_events_reads_jsonl_off_event_loop(monkeypatch, app, path):
    """Dashboard polling must not tail/parse JSONL logs on the aiohttp loop."""
    a, _, events_log = app
    rows = [
        {"timestamp": f"2026-01-01T00:00:0{i}Z", "type": "tool_call"}
        for i in range(4)
    ]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    loop_thread = threading.get_ident()
    real_read_jsonl = web_ui._read_jsonl
    read_threads: list[int] = []

    def recording_read_jsonl(*args, **kwargs):
        read_threads.append(threading.get_ident())
        return real_read_jsonl(*args, **kwargs)

    monkeypatch.setattr(web_ui, "_read_jsonl", recording_read_jsonl)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get(path)
        body = await resp.json()

    events = body.get("events") or body["data"]["events"]
    assert resp.status == 200
    assert [e["timestamp"] for e in events] == [
        "2026-01-01T00:00:02Z",
        "2026-01-01T00:00:03Z",
    ]
    assert read_threads
    assert all(thread_id != loop_thread for thread_id in read_threads)


@pytest.mark.asyncio
async def test_api_v1_events_returns_envelope_and_list_metadata(app):
    a, _, events_log = app
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_started"},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "tool_call"},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "tool_call"},
    ]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/events?type=tool_call&limit=1")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert [e["timestamp"] for e in body["data"]["events"]] == ["2026-01-01T00:00:02Z"]
    assert body["meta"] == {
        "cursor": "2026-01-01T00:00:02Z",
        "limit": 1,
        "total": 2,
        "truncated": True,
    }


def _sse_data_items(text: str) -> list[dict]:
    items = []
    for block in text.strip().split("\n\n"):
        data_lines = [
            line.removeprefix("data: ").removeprefix("data:")
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            items.append(json.loads("\n".join(data_lines)))
    return items


@pytest.mark.asyncio
async def test_api_v1_live_events_backfill_orders_and_dedups(app):
    a, turns_log, _ = app
    rows = [
        {
            "turn_id": "t1",
            "ts": "2026-01-01T00:00:01Z",
            "events": [{"type": "reasoning", "content": "a"}],
        },
        {
            "turn_id": "t2",
            "ts": "2026-01-01T00:00:02Z",
            "events": [
                {"type": "tool_call", "id": "call-1"},
                {"type": "tool_result", "id": "call-1"},
            ],
        },
        {
            "turn_id": "t2",
            "ts": "2026-01-01T00:00:02Z",
            "events": [{"type": "tool_call", "id": "call-1"}],
        },
    ]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/live-events?once=1")
        body = await resp.text()

    assert resp.status == 200
    assert resp.content_type == "text/event-stream"
    assert resp.headers["X-Accel-Buffering"] == "no"
    items = _sse_data_items(body)
    assert [item["cursor"] for item in items] == [
        "2026-01-01T00:00:01Z:t1:000000",
        "2026-01-01T00:00:01Z:t1:000001",
        "2026-01-01T00:00:02Z:t2:000000",
        "2026-01-01T00:00:02Z:t2:000001",
        "2026-01-01T00:00:02Z:t2:000002",
    ]
    assert len({item["id"] for item in items}) == len(items)
    for item in items:
        validate_live_event(item["event"])


@pytest.mark.asyncio
async def test_api_v1_live_events_since_backfill_is_strict(app):
    a, turns_log, _ = app
    rows = [
        {"turn_id": "t1", "ts": "2026-01-01T00:00:01Z", "events": [{"type": "a"}]},
        {"turn_id": "t2", "ts": "2026-01-01T00:00:02Z", "events": [{"type": "b"}]},
    ]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/live-events?once=1&since=2026-01-01T00:00:01Z:t1:000001")
        body = await resp.text()

    assert resp.status == 200
    items = _sse_data_items(body)
    assert [item["cursor"] for item in items] == ["2026-01-01T00:00:02Z:t2:000000", "2026-01-01T00:00:02Z:t2:000001"]


@pytest.mark.asyncio
async def test_api_v1_live_events_cursor_is_monotonic_for_random_turn_ids(app):
    a, turns_log, _ = app
    rows = [
        {"turn_id": "f1c5e26f1c2e", "ts": "2026-01-01T00:00:01Z"},
        {"turn_id": "37387608ce3b", "ts": "2026-01-01T00:00:02Z"},
    ]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get(
            "/api/v1/live-events?once=1&since=2026-01-01T00:00:01Z:f1c5e26f1c2e:000000"
        )
        body = await resp.text()

    assert resp.status == 200
    assert [item["event"]["turn_id"] for item in _sse_data_items(body)] == ["37387608ce3b"]


def test_turn_record_to_live_items_carries_seq_on_lifecycle():
    from mimir.live_events import turn_record_to_live_items

    items = turn_record_to_live_items({"turn_id": "t1", "ts": "2026-01-01T00:00:00Z", "seq": 42})
    lifecycle = next(i for i in items if i.event["kind"] == "turn.lifecycle")
    assert lifecycle.event["seq"] == 42
    # Records predating seq surface None (the dossier just ignores them).
    legacy = turn_record_to_live_items({"turn_id": "t2", "ts": "2026-01-01T00:00:01Z"})
    assert legacy[0].event["seq"] is None


def test_turn_record_to_live_items_carries_channel_and_trigger():
    from mimir.live_events import turn_record_to_live_items

    items = turn_record_to_live_items({
        "turn_id": "t1",
        "ts": "2026-01-01T00:00:00Z",
        "channel_id": "web-default",
        "trigger": "user_message",
        "events": [{"type": "tool_call", "name": "x"}],
    })
    lifecycle = next(i for i in items if i.event["kind"] == "turn.lifecycle")
    event = next(i for i in items if i.event["kind"] == "turn.event")
    assert lifecycle.event["channel_id"] == "web-default"
    assert lifecycle.event["trigger"] == "user_message"
    assert event.event["channel_id"] == "web-default"
    assert event.event["trigger"] == "user_message"


def test_read_live_event_items_since_stops_after_crossing_acknowledged_timestamp(tmp_path: Path):
    from mimir.live_events import read_live_event_items_since

    path = tmp_path / "turns.jsonl"
    path.write_text("", encoding="utf-8")
    rows = [
        {"turn_id": "very-old", "ts": "2026-01-01T00:00:00Z"},
        {"turn_id": "old", "ts": "2026-01-01T00:00:01Z"},
        {"turn_id": "seen", "ts": "2026-01-01T00:00:02Z"},
        {"turn_id": "new", "ts": "2026-01-01T00:00:03Z"},
    ]
    calls = []

    def tail_reader(_path: Path):
        for row in reversed(rows):
            calls.append(row["turn_id"])
            yield row

    items = read_live_event_items_since(
        path,
        since="2026-01-01T00:00:02Z:seen:000000",
        tail_reader=tail_reader,
    )

    assert [item.event["turn_id"] for item in items] == ["new"]
    assert calls == ["new", "seen", "old"]


@pytest.mark.asyncio
async def test_api_v1_live_events_auth_uses_header_not_query_param(tmp_path: Path):
    from mimir.server import _make_auth_middleware

    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    turns_log.write_text(
        json.dumps({"turn_id": "t1", "ts": "2026-01-01T00:00:01Z"}) + "\n",
        encoding="utf-8",
    )
    a = web.Application(middlewares=[_make_auth_middleware("live-secret")])
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log)

    async with TestClient(TestServer(a)) as client:
        query_resp = await client.get("/api/v1/live-events?once=1&api_key=live-secret")
        assert query_resp.status == 401

        resp = await client.get(
            "/api/v1/live-events?once=1",
            headers={"X-API-Key": "live-secret"},
        )
        body = await resp.text()

    assert resp.status == 200
    assert _sse_data_items(body)[0]["cursor"] == "2026-01-01T00:00:01Z:t1:000000"


@pytest.mark.asyncio
async def test_api_v1_live_events_rejects_when_stream_cap_exhausted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_ui, "LIVE_EVENTS_MAX_STREAMS", 0)
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/live-events?once=1")
        body = await resp.text()

    assert resp.status == 429
    assert "too many live event streams" in body


@pytest.mark.asyncio
async def test_api_v1_live_events_releases_slot_when_prepare_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(web_ui, "LIVE_EVENTS_MAX_STREAMS", 1)
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
    )

    original_prepare = web.StreamResponse.prepare
    calls = 0

    async def flaky_prepare(self, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionResetError("client disconnected during SSE handshake")
        return await original_prepare(self, request)

    monkeypatch.setattr(web.StreamResponse, "prepare", flaky_prepare)

    async with TestClient(TestServer(a)) as client:
        failed_resp = await client.get("/api/v1/live-events?once=1")
        await failed_resp.text()

        resp = await client.get("/api/v1/live-events?once=1")
        body = await resp.text()

    assert calls >= 2
    assert resp.status == 200
    assert "too many live event streams" not in body

@pytest.mark.asyncio
async def test_read_jsonl_caps_at_max_records(app):
    """Pattern A (2026-05-10): ``_read_jsonl`` is bounded by
    ``max_records`` (default 5000). Pre-2026-05-10 it forward-read
    the entire file synchronously per HTTP request — combined with
    the turn-viewer polling every 5s, the loop got pinned re-parsing
    hundreds of MB on a hot file. The cap means older records past
    the limit are silently dropped from the response."""
    from mimir.web_ui import _read_jsonl

    a, _, events_log = app
    # Write 50 records but cap at 10.
    rows = [{"i": i, "type": "x"} for i in range(50)]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = _read_jsonl(events_log, max_records=10)
    # Output is chronological — most recent 10 records (i=40..49).
    assert [r["i"] for r in out] == list(range(40, 50))


@pytest.mark.asyncio
async def test_read_jsonl_under_cap_returns_all(app):
    """When the file has fewer records than the cap, all are returned
    in chronological order (no silent dropping)."""
    from mimir.web_ui import _read_jsonl

    a, _, events_log = app
    rows = [{"i": i, "type": "x"} for i in range(7)]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = _read_jsonl(events_log, max_records=100)
    assert [r["i"] for r in out] == list(range(7))


@pytest.mark.asyncio
async def test_register_routes_is_idempotent(app):
    """Calling register_routes twice (e.g. server rebuild) doesn't crash."""
    a, turns_log, events_log = app
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log)
    # Should still work.
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/health" if False else "/api/turns")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_react_app_serves_built_index_and_assets(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<div id="root"></div><script src="/app/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('mimir app')", encoding="utf-8")

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=dist,
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/app")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        assert "X-Mimir-Frontend" not in resp.headers
        assert resp.headers["Cache-Control"].startswith("no-store")
        assert "/app/assets/app.js" in await resp.text()

        asset_resp = await client.get("/app/assets/app.js")
        assert asset_resp.status == 200
        assert await asset_resp.text() == "console.log('mimir app')"

        fallback_resp = await client.get("/app/turns/42")
        assert fallback_resp.status == 200
        assert fallback_resp.headers["Cache-Control"].startswith("no-store")
        assert "/app/assets/app.js" in await fallback_resp.text()


@pytest.mark.asyncio
async def test_web_bootstrap_is_no_store_and_secret_free(tmp_path: Path):
    class _Config:
        web_host = "0.0.0.0"

    a = web.Application()
    a["api_key"] = "super-secret"
    a["config"] = _Config()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/web/bootstrap")
        body_text = await resp.text()
        body = json.loads(body_text)

    assert resp.status == 200
    assert resp.headers["Cache-Control"].startswith("no-store")
    assert "super-secret" not in body_text
    assert body["auth"]["required"] is True
    assert body["server"]["public_bind"] is True
    assert body["stream_auth"]["shape"] == "fetch-event-stream"
    assert body["stream_auth"]["native_eventsource_supported_when_auth_required"] is False


@pytest.mark.asyncio
async def test_api_v1_web_bootstrap_is_enveloped_no_store_and_secret_free(tmp_path: Path):
    class _Config:
        web_host = "0.0.0.0"
        model_spec = "codex_plus:gpt-5.5"

    a = web.Application()
    a["api_key"] = "super-secret"
    a["config"] = _Config()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/web/bootstrap")
        body_text = await resp.text()
        body = json.loads(body_text)

    assert resp.status == 200
    assert resp.headers["Cache-Control"].startswith("no-store")
    assert "super-secret" not in body_text
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["version"]  # mimir build/release version surfaced
    # Running model = the model part of the provider:model spec.
    assert body["data"]["model"] == "gpt-5.5"
    # No turns logged in this fixture -> running total is 0.
    assert body["data"]["turns_total"] == 0
    # No home configured -> agent UI config falls back to defaults.
    assert body["data"]["ui"] == {"agent_name": "Mimir", "skin": "neon-terminal"}
    assert body["data"]["auth"]["required"] is True
    assert body["data"]["server"]["public_bind"] is True
    assert [item["id"] for item in body["data"]["dashboard_extensions"]][:4] == [
        "chat",
        "usage",
        "turns",
        "ops",
    ]
    usage_manifest = next(
        item for item in body["data"]["dashboard_extensions"] if item["id"] == "usage"
    )
    assert usage_manifest["label"] == "Usage"
    assert usage_manifest["route_path"] == "/usage"
    assert usage_manifest["api_namespace"] is None
    ops_manifest = next(
        item for item in body["data"]["dashboard_extensions"] if item["id"] == "ops"
    )
    assert ops_manifest["label"] == "Ops"
    assert ops_manifest["api_namespace"] == "ops"
    assert ops_manifest["trusted_first_party"] is True


@pytest.mark.asyncio
async def test_api_v1_web_bootstrap_reads_turn_total_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )
    loop_thread = threading.get_ident()
    read_threads: list[int] = []

    def recording_read_turns_total(path: Path) -> int:
        read_threads.append(threading.get_ident())
        return 123

    monkeypatch.setattr(web_ui, "read_turns_total", recording_read_turns_total)

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/web/bootstrap")
        body = await resp.json()

    assert resp.status == 200
    assert body["data"]["turns_total"] == 123
    assert read_threads
    assert all(thread_id != loop_thread for thread_id in read_threads)


def test_read_web_ui_config_reads_agent_file(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "web_ui.json").write_text(
        json.dumps({"agent_name": "Nebula-9", "skin": "cosmic-nebula"}), encoding="utf-8"
    )
    assert web_ui.read_web_ui_config(tmp_path) == {
        "agent_name": "Nebula-9",
        "skin": "cosmic-nebula",
    }


def test_read_web_ui_config_falls_back_to_defaults(tmp_path: Path):
    defaults = {"agent_name": "Mimir", "skin": "neon-terminal"}
    state = tmp_path / "state"
    state.mkdir()
    # No home, missing file, malformed JSON, and partial config all fall back.
    assert web_ui.read_web_ui_config(None) == defaults
    assert web_ui.read_web_ui_config(tmp_path) == defaults
    (state / "web_ui.json").write_text("{ not json", encoding="utf-8")
    assert web_ui.read_web_ui_config(tmp_path) == defaults
    (state / "web_ui.json").write_text(
        json.dumps({"agent_name": "  Solo  "}), encoding="utf-8"
    )
    assert web_ui.read_web_ui_config(tmp_path) == {
        "agent_name": "Solo",
        "skin": "neon-terminal",
    }


def test_read_turns_total_uses_newest_seq(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    assert web_ui.read_turns_total(path) == 0  # missing file -> 0
    path.write_text(
        json.dumps({"turn_id": "a", "seq": 41}) + "\n"
        + json.dumps({"turn_id": "b", "seq": 42}) + "\n",
        encoding="utf-8",
    )
    assert web_ui.read_turns_total(path) == 42  # newest record's seq


def test_ensure_web_ui_config_seeds_defaults_without_clobbering(tmp_path: Path):
    path = tmp_path / "state" / "web_ui.json"
    # Missing -> seeded with defaults (and the state/ dir is created).
    web_ui.ensure_web_ui_config(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "agent_name": "Mimir",
        "skin": "neon-terminal",
    }
    # Existing -> left untouched (agent edits survive restarts).
    path.write_text(json.dumps({"agent_name": "Nebula-9", "skin": "cosmic-nebula"}), encoding="utf-8")
    web_ui.ensure_web_ui_config(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["agent_name"] == "Nebula-9"
    # No home is a no-op (doesn't raise).
    web_ui.ensure_web_ui_config(None)


@pytest.mark.asyncio
async def test_first_party_backend_namespace_hook_registers_ops_api(tmp_path: Path):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/ops")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)


@pytest.mark.asyncio
async def test_disabled_backend_namespace_manifest_skips_ops_api(tmp_path: Path):
    registry = first_party_dashboard_extensions(
        [
            DashboardExtensionManifest(
                id="ops",
                route_path="/ops",
                label="Ops",
                nav_position=10,
                enabled=False,
                api_namespace="ops",
            )
        ]
    )
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
        dashboard_extensions=registry,
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/ops")
        bootstrap = await client.get("/api/v1/web/bootstrap")
        body = await bootstrap.json()

    assert resp.status == 404
    assert body["data"]["dashboard_extensions"] == []


@pytest.mark.asyncio
async def test_api_v1_memory_tree_search_and_file_detail(tmp_path: Path):
    home = tmp_path / "home"
    memory = home / "memory"
    state = home / "state"
    memory.mkdir(parents=True)
    state.mkdir(parents=True)
    (memory / "INDEX.md").write_text("<!-- desc: Memory index -->\n# Memory\n")
    topics = memory / "topics"
    topics.mkdir()
    (topics / "alpha.md").write_text("<!-- desc: Alpha topic -->\nAlpha memory note\n")
    wiki = state / "wiki"
    wiki.mkdir()
    (wiki / "alpha.md").write_text("# Alpha state\nSearchable alpha state note\n")

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        home=home,
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        tree_resp = await client.get("/api/v1/memory?view=tree")
        tree_body = await tree_resp.json()
        search_resp = await client.get("/api/v1/memory?view=search&q=alpha")
        search_body = await search_resp.json()
        file_resp = await client.get("/api/v1/memory?view=file&path=state/wiki/alpha.md")
        file_body = await file_resp.json()

    assert tree_resp.status == 200
    validate_api_envelope(tree_body, expect_ok=True)
    assert [child["name"] for child in tree_body["data"]["children"]] == [
        "memory",
        "state",
    ]

    def _find_path(node: dict, path: str) -> dict | None:
        if node.get("path") == path:
            return node
        for child in node.get("children", []):
            found = _find_path(child, path)
            if found is not None:
                return found
        return None

    alpha_node = _find_path(tree_body["data"], "memory/topics/alpha.md")
    assert alpha_node is not None
    assert alpha_node["desc"] == "Alpha topic"

    assert search_resp.status == 200
    validate_api_envelope(search_body, expect_ok=True)
    validate_list_meta(search_body["meta"])
    hit_paths = {hit["path"] for hit in search_body["data"]["hits"]}
    assert {"memory/topics/alpha.md", "state/wiki/alpha.md"} <= hit_paths
    assert search_body["meta"]["total"] >= 2

    assert file_resp.status == 200
    validate_api_envelope(file_body, expect_ok=True)
    assert file_body["data"]["path"] == "state/wiki/alpha.md"
    assert "Searchable alpha state note" in file_body["data"]["content"]


@pytest.mark.asyncio
async def test_api_v1_memory_file_errors_are_enveloped(tmp_path: Path):
    home = tmp_path / "home"
    (home / "memory").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    (home / "state" / "note.txt").write_text("not markdown\n")

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        home=home,
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        missing_path = await client.get("/api/v1/memory?view=file")
        non_md = await client.get("/api/v1/memory?view=file&path=state/note.txt")
        missing_file = await client.get("/api/v1/memory?view=file&path=state/missing.md")
        missing_query = await client.get("/api/v1/memory?view=search")

        missing_path_body = await missing_path.json()
        non_md_body = await non_md.json()
        missing_file_body = await missing_file.json()
        missing_query_body = await missing_query.json()

    assert missing_path.status == 400
    validate_api_envelope(missing_path_body, expect_ok=False)
    assert missing_path_body["error"]["code"] == "missing_path"

    assert non_md.status == 400
    validate_api_envelope(non_md_body, expect_ok=False)
    assert non_md_body["error"]["code"] == "memory_file_error"
    assert "only .md" in non_md_body["error"]["message"]

    assert missing_file.status == 404
    validate_api_envelope(missing_file_body, expect_ok=False)
    assert "not found" in missing_file_body["error"]["message"]

    assert missing_query.status == 400
    validate_api_envelope(missing_query_body, expect_ok=False)
    assert missing_query_body["error"]["code"] == "missing_query"


@pytest.mark.asyncio
async def test_api_v1_web_bootstrap_auth_exempt_with_middleware(tmp_path: Path):
    from mimir.server import _make_auth_middleware

    class _Config:
        web_host = "0.0.0.0"

    a = web.Application(middlewares=[_make_auth_middleware("super-secret")])
    a["api_key"] = "super-secret"
    a["config"] = _Config()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/web/bootstrap")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["auth"]["required"] is True


@pytest.mark.asyncio
async def test_api_v1_admin_config_requires_auth_and_redacts_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from mimir.server import _make_auth_middleware

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "anthropic:MiniMax-M2.7")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-admin-config-secret")
    monkeypatch.setenv("MIMIR_API_KEY", "admin-route-secret")
    monkeypatch.setenv("ADMIN_CONFIG_MCP_SECRET", "nested-admin-config-secret")
    monkeypatch.setenv(
        "MIMIR_MCP_SERVERS_JSON",
        '[{"name": "demo", "command": "uvx", "args": ["mcp-server-demo"], '
        '"env": {"API_KEY": "${ADMIN_CONFIG_MCP_SECRET}"}}]',
    )
    monkeypatch.setenv(
        "MIMIR_STATE_REPO",
        "https://user:raw-git-token@example.invalid/repo.git?token=ghp_adminconfigquerytoken",
    )
    monkeypatch.setenv(
        "MIMIR_PUBLIC_CALLBACK_URL",
        "https://example.invalid/hook?api_key=sk-public-admin-config-value",
    )
    monkeypatch.setenv("MIMIR_PUBLIC_BARE_VALUE", "ghp_adminconfigbaretoken")
    config = Config.from_env()
    config.resend_nudge_channels = ("channel-with-secret-shaped-value",)
    config.file_op_extra_roots = [tmp_path / "private-extra-root"]

    (tmp_path / "scheduler.yaml").write_text(
        "- name: heartbeat\n"
        "  prompt: Check status.\n"
        "  cron: '0 * * * *'\n"
        "  channel_id: null\n",
        encoding="utf-8",
    )

    a = web.Application(middlewares=[_make_auth_middleware("admin-route-secret")])
    a["api_key"] = "admin-route-secret"
    a["config"] = config
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl",
        home=tmp_path,
    )

    async with TestClient(TestServer(a)) as client:
        denied = await client.get("/api/v1/admin/config")
        allowed = await client.get(
            "/api/v1/admin/config",
            headers={"X-API-Key": "admin-route-secret"},
        )
        body = await allowed.json()

    assert denied.status == 401
    assert allowed.status == 200
    validate_api_envelope(body, expect_ok=True)
    data = body["data"]
    assert data["model"]["model_spec"] == "anthropic:MiniMax-M2.7"
    assert data["model"]["provider"] == "minimax"
    assert data["model"]["context_window"] == "1m beta"
    assert data["schedules"][0]["name"] == "heartbeat"
    assert data["mutation_policy"] == {
        "mode": "read_only_v1",
        "mutable_fields": [],
        "reveal_secret_values": False,
        "reveal_path": None,
        "edit_path": None,
        "rate_limited": False,
    }

    env_by_name = {row["name"]: row for row in data["env"]}
    assert env_by_name["ANTHROPIC_API_KEY"]["present"] is True
    assert env_by_name["ANTHROPIC_API_KEY"]["secret"] is True
    assert env_by_name["ANTHROPIC_API_KEY"]["value"] == "[REDACTED]"
    serialized = json.dumps(data)
    assert "sk-ant-admin-config-secret" not in serialized
    assert "nested-admin-config-secret" not in serialized
    assert "raw-git-token" not in serialized
    assert "ghp_adminconfigquerytoken" not in serialized
    assert "sk-public-admin-config-value" not in serialized
    assert "ghp_adminconfigbaretoken" not in serialized
    assert "channel-with-secret-shaped-value" not in serialized
    assert "private-extra-root" not in serialized
    assert data["raw_config"]["anthropic_api_key"] == "[REDACTED]"
    assert data["raw_config"]["mcp_servers"][0]["env"]["API_KEY"] == "[REDACTED]"
    assert (
        data["raw_config"]["git_state_repo"]
        == "https://[REDACTED]@example.invalid/repo.git?token=[REDACTED]"
    )
    assert env_by_name["MIMIR_PUBLIC_CALLBACK_URL"]["value"] == (
        "https://example.invalid/hook?api_key=[REDACTED]"
    )
    assert env_by_name["MIMIR_PUBLIC_BARE_VALUE"]["value"] == "[REDACTED]"
    assert "resend_nudge_channels" not in data["raw_config"]
    assert "file_op_extra_roots" not in data["raw_config"]


@pytest.mark.asyncio
async def test_api_v1_ops_errors_use_stable_envelope(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/ops?days=bad")
        body = await resp.json()

    assert resp.status == 400
    validate_api_envelope(body, expect_ok=False)
    assert body["error"]["code"] == "invalid_days"


@pytest.mark.asyncio
async def test_api_v1_scheduler_lists_schedules_pollers_and_commitments(tmp_path: Path):
    async def enqueue(_event):
        return True

    home = tmp_path / "home"
    home.mkdir()
    scheduler_yaml = home / "scheduler.yaml"
    scheduler = Scheduler(scheduler_yaml, enqueue, home=home)
    await scheduler.add_job(
        SchedulerJob(
            name="morning-review",
            prompt_file="morning.md",
            cron="0 8 * * *",
            channel_id="ops",
            priority="normal",
        )
    )
    scheduler._pollers["github"] = PollerConfig(  # noqa: SLF001
        name="github",
        command="python poller.py",
        cron="*/5 * * * *",
        env={
            "API_KEY": "poller-secret-value",
            "PUBLIC_URL": "https://user:embedded-token@example.invalid/path",
            "NESTED": {"PASSWORD": "nested-secret-value"},
        },  # type: ignore[arg-type]
        pass_env=("GITHUB_TOKEN", "PUBLIC_FLAG"),
        env_required=("GITHUB_TOKEN",),
        skill_dir=home,
        priority="high",
    )
    scheduler._pollers["never-fired"] = PollerConfig(  # noqa: SLF001
        name="never-fired",
        command="python poller.py",
        cron="0 0 1 * *",
        env={},
        skill_dir=home,
    )
    events_log = tmp_path / "events.jsonl"
    old_schedule_event = json.dumps({
        "timestamp": "2026-06-15T08:00:00+00:00",
        "type": "scheduled_tick",
        "schedule_name": "morning-review",
        "channel_id": "ops",
    })
    events_log.write_text(
        old_schedule_event
        + "\n"
        + "\n".join(
            json.dumps({
                "timestamp": f"2026-06-18T07:{minute:02d}:00+00:00",
                "type": "noise",
            })
            for minute in range(5000)
        )
        + "\n"
        + json.dumps({
            "timestamp": "2026-06-18T08:02:00+00:00",
            "type": "poller_complete",
            "poller": "github",
            "events_emitted": 2,
            "events_rejected": 0,
        })
        + "\n",
        encoding="utf-8",
    )

    store = CommitmentsStore(home / ".mimir" / "commitments.jsonl")
    await store.add(
        CommitmentRecord(
            id="c-soon",
            channel_id="ops",
            text="Send the scheduler report",
            due_window_start_unix=60,
            due_window_end_unix=3600,
        )
    )

    a = web.Application()
    a["scheduler"] = scheduler
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=events_log,
        home=home,
        commitments_store=store,
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/scheduler?due_window=overdue")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    validate_list_meta(body["meta"])
    assert body["data"]["schedules"][0]["name"] == "morning-review"
    assert body["data"]["schedules"][0]["prompt_source"] == "file:morning.md"
    assert body["data"]["schedules"][0]["last_run_at"] == "2026-06-15T08:00:00+00:00"
    assert body["data"]["schedules"][0]["recent_result"] == "scheduled_tick"
    pollers = {row["name"]: row for row in body["data"]["pollers"]}
    assert pollers["github"]["priority"] == "high"
    assert pollers["github"]["recent_result"] == "emitted=2 rejected=0"
    assert pollers["never-fired"]["last_run_at"] is None
    assert pollers["never-fired"]["recent_result"] is None
    poller = pollers["github"]
    assert poller["pass_env"] == ["[REDACTED]", "PUBLIC_FLAG"]
    assert poller["env_required"] == ["[REDACTED]"]
    assert poller["config"]["env"]["API_KEY"] == "[REDACTED]"
    assert poller["config"]["env"]["NESTED"]["PASSWORD"] == "[REDACTED]"
    assert poller["config"]["env"]["PUBLIC_URL"] == "https://[REDACTED]@example.invalid/path"
    serialized = json.dumps(body)
    assert "poller-secret-value" not in serialized
    assert "nested-secret-value" not in serialized
    assert "embedded-token" not in serialized
    assert "GITHUB_TOKEN" not in serialized
    assert body["data"]["commitments"][0]["id"] == "c-soon"
    assert body["data"]["actions"]["mutations_enabled"] is False
    assert "trigger" in body["data"]["actions"]["deferred"]


def test_scheduler_state_event_scan_has_bounded_never_fired_path(tmp_path: Path):
    events_log = tmp_path / "events.jsonl"
    old_matching_event = json.dumps({
        "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "scheduled_tick",
        "schedule_name": "too-old",
    })
    events_log.write_text(
        old_matching_event
        + "\n"
        + "\n".join(
            json.dumps({"type": "noise", "i": i})
            for i in range(web_ui.SCHEDULER_STATE_EVENT_SCAN_RECORDS + 5)
        )
        + "\n",
        encoding="utf-8",
    )

    records = web_ui._read_jsonl_matching(  # noqa: SLF001
        events_log,
        max_records=web_ui.SCHEDULER_STATE_EVENT_SCAN_RECORDS,
        include=lambda record: record.get("type") == "scheduled_tick",
        stop_when=lambda records: False,
    )

    assert records == []


@pytest.mark.asyncio
async def test_api_v1_scheduler_rejects_invalid_due_window(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/scheduler?due_window=never")
        body = await resp.json()

    assert resp.status == 400
    validate_api_envelope(body, expect_ok=False)
    assert body["error"]["code"] == "invalid_due_window"


@pytest.mark.asyncio
async def test_shared_web_auth_script_served_no_store(tmp_path: Path):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/app/auth.js")
        body = await resp.text()

    assert resp.status == 200
    assert resp.headers["Cache-Control"].startswith("no-store")
    assert "window.MimirAuth" in body
    assert "api_key=" not in body


@pytest.mark.asyncio
async def test_react_app_missing_build_returns_503(tmp_path: Path):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/app")
        assert resp.status == 503
        assert "npm run build" in await resp.text()


def _make_min_saga_db(path: Path) -> None:
    """Minimal saga DB with just the tables build_db_stats_payload reads."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE atoms (id TEXT, tombstoned INTEGER DEFAULT 0);"
        "CREATE TABLE sessions (id TEXT);"
        "CREATE TABLE triples (id TEXT, tombstoned INTEGER DEFAULT 0);"
        "CREATE TABLE schema_version (version INTEGER);"
        "INSERT INTO atoms (id, tombstoned) VALUES ('a1', 0);"
        "INSERT INTO schema_version (version) VALUES (1);"
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_saga_db_fallback_uses_dot_mimir_not_state(tmp_path: Path):
    """Regression (saga page 'db not found or unreadable'): with home set
    and no explicit saga_db, the /saga dashboard must read
    <home>/.mimir/saga.db (saga's canonical default), not the stale
    <home>/state/saga.db that no longer exists."""
    _make_min_saga_db(tmp_path / ".mimir" / "saga.db")
    a = web.Application()
    web_ui.register_routes(
        a, turns_log=tmp_path / "t.jsonl", events_log=tmp_path / "e.jsonl",
        home=tmp_path,  # no saga_db → exercises the fallback
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/saga?view=stats")
        assert resp.status == 200
        payload = await resp.json()
        assert payload.get("ready") is True, payload
        assert payload["db_path"].replace("\\", "/").endswith("/.mimir/saga.db"), \
            payload["db_path"]


@pytest.mark.asyncio
async def test_saga_db_explicit_kwarg_wins(tmp_path: Path):
    """server.py passes the saga.toml-resolved path as saga_db=; it must
    take precedence over the home-derived fallback."""
    db = tmp_path / "custom" / "saga.db"
    _make_min_saga_db(db)
    a = web.Application()
    web_ui.register_routes(
        a, turns_log=tmp_path / "t.jsonl", events_log=tmp_path / "e.jsonl",
        home=tmp_path, saga_db=db,
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/saga?view=stats")
        payload = await resp.json()
        assert payload.get("ready") is True, payload
        assert payload["db_path"].replace("\\", "/").endswith("/custom/saga.db"), \
            payload["db_path"]
