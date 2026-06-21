"""Per-user web auth + RBAC: middleware gate, /whoami, chat attribution (#726)."""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.bridges.web_chat import WebChatBridge
from mimir.turn_event_bus import TurnEventBus
from mimir.identities import IdentityResolver
from mimir.identities_populator import issue_web_key
from mimir import web_ui
from mimir.server import _is_admin_required, _make_auth_middleware
from mimir.web_ui import _whoami_payload, web_gate_active


async def _echo(request: web.Request) -> web.Response:
    ident = request.get("auth_identity")
    return web.json_response({
        "canonical": ident.canonical if ident else None,
        "is_master": bool(request.get("auth_is_master")),
        "is_admin": bool(request.get("auth_is_admin")),
    })


def _resolver(home: Path) -> IdentityResolver:
    r = IdentityResolver(home)
    r.reload()
    return r


def _app(home: Path, master_key: str) -> web.Application:
    app = web.Application(middlewares=[_make_auth_middleware(master_key)])
    app["identity_resolver"] = _resolver(home)
    app.router.add_get("/api/v1/turns", _echo)         # auth, non-admin
    app.router.add_get("/api/v1/admin/config", _echo)  # auth + admin
    app.router.add_get("/api/v1/saga", _echo)          # auth + admin: global atoms
    app.router.add_post("/api/v1/saga/sql", _echo)     # auth + admin: global SQL
    app.router.add_get("/api/v1/memory", _echo)        # auth + admin: global files
    app.router.add_get("/api/saga", _echo)             # legacy auth + admin: global atoms
    app.router.add_post("/api/saga/sql", _echo)        # legacy auth + admin: global SQL
    app.router.add_get("/api/memory", _echo)           # legacy auth + admin: global files
    app.router.add_get("/api/ops", _echo)              # legacy global ops dashboard
    app.router.add_get("/api/v1/ops", _echo)           # global ops dashboard
    app.router.add_get("/api/v1/scheduler", _echo)     # global scheduler dashboard
    app.router.add_get("/api/v1/chainlink-board", _echo)  # global task dashboard
    app.router.add_get(
        "/api/v1/chainlink-board/artifact", _echo,
    )  # Worklink artifacts
    app.router.add_get("/health", _echo)               # exempt
    return app


# ── middleware: the RBAC security gate ──────────────────────────────────


async def test_user_key_allowed_on_normal_route_but_403_on_admin(tmp_path: Path) -> None:
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        r = await c.get("/api/v1/turns", headers={"X-API-Key": user_key})
        assert r.status == 200
        body = await r.json()
        assert body["canonical"] == "alice" and body["is_admin"] is False
        r2 = await c.get("/api/v1/admin/config", headers={"X-API-Key": user_key})
        assert r2.status == 403  # the RBAC boundary — server-side, not UI




async def test_saga_and_memory_routes_are_admin_only(tmp_path: Path) -> None:
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        for path in ("/api/v1/saga", "/api/v1/memory", "/api/saga", "/api/memory"):
            user_resp = await c.get(path, headers={"X-API-Key": user_key})
            assert user_resp.status == 403, path
            admin_resp = await c.get(path, headers={"X-API-Key": admin_key})
            assert admin_resp.status == 200, path
            master_resp = await c.get(path, headers={"X-API-Key": "master-secret"})
            assert master_resp.status == 200, path

        for path in ("/api/v1/saga/sql", "/api/saga/sql"):
            user_sql = await c.post(path, headers={"X-API-Key": user_key})
            assert user_sql.status == 403, path
            admin_sql = await c.post(path, headers={"X-API-Key": admin_key})
            assert admin_sql.status == 200, path


async def test_global_dashboard_routes_are_admin_only(tmp_path: Path) -> None:
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    admin_only_paths = (
        "/api/ops",
        "/api/v1/ops",
        "/api/v1/scheduler",
        "/api/v1/chainlink-board",
        "/api/v1/chainlink-board/artifact",
    )
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        for path in admin_only_paths:
            denied = await c.get(path, headers={"X-API-Key": user_key})
            assert denied.status == 403, path

            admin = await c.get(path, headers={"X-API-Key": admin_key})
            assert admin.status == 200, path

            master = await c.get(path, headers={"X-API-Key": "master-secret"})
            assert master.status == 200, path


def test_admin_required_prefix_matching_is_segment_aware() -> None:
    for path in (
        "/api/v1/admin",
        "/api/v1/admin/config",
        "/api/ops",
        "/api/ops/health",
        "/api/v1/ops",
        "/api/v1/ops/health",
        "/api/v1/scheduler",
        "/api/v1/scheduler/jobs",
        "/api/v1/chainlink-board",
        "/api/v1/chainlink-board/artifact",
        "/api/v1/saga",
        "/api/v1/saga/sql",
        "/api/v1/memory",
        "/api/v1/memory/file",
        "/api/saga",
        "/api/saga/sql",
        "/api/memory",
        "/api/memory/file",
    ):
        assert _is_admin_required(path), path

    for path in (
        "/api/v1/adminish",
        "/api/opsical",
        "/api/v1/opsical",
        "/api/v1/schedulerish",
        "/api/v1/chainlink-boardwalk",
        "/api/v1/chainlink",
        "/api/v1/sagacity",
        "/api/v1/memoryless",
        "/api/sagacity",
        "/api/memoryless",
        "/api/v1/turns",
    ):
        assert not _is_admin_required(path), path


async def test_admin_key_allowed_on_admin_route(tmp_path: Path) -> None:
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        r = await c.get("/api/v1/admin/config", headers={"X-API-Key": admin_key})
        assert r.status == 200
        body = await r.json()
        assert body["canonical"] == "ops" and body["is_admin"] is True


async def test_master_key_is_admin_but_no_identity(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        r = await c.get("/api/v1/admin/config", headers={"X-API-Key": "master-secret"})
        assert r.status == 200
        body = await r.json()
        assert body["is_master"] is True and body["is_admin"] is True
        assert body["canonical"] is None  # master key carries no user identity


async def test_unknown_and_missing_keys_rejected(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        assert (await c.get("/api/v1/turns", headers={"X-API-Key": "nope"})).status == 401
        assert (await c.get("/api/v1/turns")).status == 401  # no key


async def test_exempt_route_needs_no_key(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        assert (await c.get("/health")).status == 200


async def test_unauthorized_identity_rejected(tmp_path: Path) -> None:
    # A person with a key but NO roles is authenticated-but-unauthorized.
    key = issue_web_key(tmp_path, "stranger", roles=[])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        assert (await c.get("/api/v1/turns", headers={"X-API-Key": key})).status == 401


async def test_dev_open_mode_when_no_key_and_no_users(tmp_path: Path) -> None:
    # No master key + no web keys → legacy open path (localhost dev).
    async with TestClient(TestServer(_app(tmp_path, ""))) as c:
        r = await c.get("/api/v1/turns")
        assert r.status == 200
        assert (await r.json())["canonical"] is None  # no identity attached


async def test_failsafe_gate_active_when_users_exist_without_master(tmp_path: Path) -> None:
    # Web keys configured but MIMIR_API_KEY unset → gate must still activate,
    # so adding users can't leave the server wide open.
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, ""))) as c:
        assert (await c.get("/api/v1/turns")).status == 401  # no key → blocked
        ok = await c.get("/api/v1/turns", headers={"X-API-Key": user_key})
        assert ok.status == 200 and (await ok.json())["canonical"] == "alice"


# ── /whoami payload ─────────────────────────────────────────────────────


def test_whoami_payload_master() -> None:
    p = _whoami_payload(None, True)
    assert p["is_master"] and p["is_admin"] and p["roles"] == ["admin"] and p["canonical"] is None


def test_whoami_payload_user(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    ident = _resolver(tmp_path).identity("alice")
    p = _whoami_payload(ident, False)
    assert p == {
        "canonical": "alice", "display_name": None,
        "roles": ["user"], "is_admin": False, "is_master": False,
    }


def test_whoami_payload_empty() -> None:
    assert _whoami_payload(None, False) == {
        "canonical": None, "display_name": None,
        "roles": [], "is_admin": False, "is_master": False,
    }


# ── gate predicate (shared by middleware + bootstrap) ───────────────────


def test_web_gate_active_predicate(tmp_path: Path) -> None:
    assert web_gate_active("master", None) is True            # master key set
    assert web_gate_active("", None) is False                 # dev: no key, no resolver
    assert web_gate_active("", _resolver(tmp_path)) is False  # resolver, but no web keys
    issue_web_key(tmp_path, "alice", roles=["user"])          # now a web key exists
    assert web_gate_active("", _resolver(tmp_path)) is True   # fail-safe: gate on w/o master
    assert web_gate_active(None, _resolver(tmp_path)) is True


class _Config:
    web_host = "127.0.0.1"


def _bootstrap_app(home: Path, master_key: str, *, with_user: bool) -> web.Application:
    if with_user:
        issue_web_key(home, "alice", roles=["user"])
    app = web.Application()
    app["api_key"] = master_key
    app["config"] = _Config()
    app["identity_resolver"] = _resolver(home)
    web_ui.register_routes(
        app,
        turns_log=home / "t.jsonl",
        events_log=home / "e.jsonl",
        react_app_dist=home / "missing-dist",
    )
    return app


async def test_bootstrap_reports_gate_active_with_webkeys_and_no_master(tmp_path: Path) -> None:
    # The bug mimir caught: no MIMIR_API_KEY but per-user keys exist → the
    # middleware gates, so bootstrap must tell the browser auth is required.
    async with TestClient(TestServer(_bootstrap_app(tmp_path, "", with_user=True))) as c:
        for path in ("/api/web/bootstrap", "/api/v1/web/bootstrap"):
            body = await (await c.get(path)).json()
            data = body.get("data", body)  # v1 is enveloped, legacy is flat
            assert data["auth"]["required"] is True, path
            assert data["server"]["unauthenticated_allowed"] is False, path


async def test_bootstrap_dev_mode_reports_no_auth(tmp_path: Path) -> None:
    async with TestClient(TestServer(_bootstrap_app(tmp_path, "", with_user=False))) as c:
        for path in ("/api/web/bootstrap", "/api/v1/web/bootstrap"):
            body = await (await c.get(path)).json()
            data = body.get("data", body)
            assert data["auth"]["required"] is False, path
            assert data["server"]["unauthenticated_allowed"] is True, path


# ── web-chat trusted attribution ────────────────────────────────────────


class _FakeRequest(dict):
    """Minimal request: a MutableMapping (for request.get) + async json()."""

    def __init__(self, body: dict, **attrs) -> None:
        super().__init__()
        self.update(attrs)
        self._body = body

    async def json(self) -> dict:
        return self._body


def _bridge(tmp_path: Path) -> WebChatBridge:
    async def _noop(_event) -> bool:
        return True

    return WebChatBridge(enqueue=_noop, home=tmp_path)


async def test_chat_author_from_identity_not_client_body(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    ident = _resolver(tmp_path).identity("alice")
    req = _FakeRequest(
        {"content": "hi", "author": "SPOOFED", "author_id": "SPOOFED"},
        auth_identity=ident,
        auth_is_master=False,
    )
    event, channel_id, err = await _bridge(tmp_path)._build_inbound_event(req)
    assert err is None and event is not None
    assert event.author_id == "alice"  # from the authenticated key, NOT spoofed
    assert event.author == "alice"
    # An authenticated user's default-channel post routes to their own per-user
    # web channel (chainlink: web-chat history scoping), not the shared default.
    assert channel_id == "web-alice"


async def test_chat_master_key_rejected(tmp_path: Path) -> None:
    req = _FakeRequest({"content": "hi"}, auth_is_master=True)
    event, channel_id, err = await _bridge(tmp_path)._build_inbound_event(req)
    assert event is None and err is not None and err.status == 403


async def test_chat_dev_mode_falls_back_to_client_asserted(tmp_path: Path) -> None:
    # No auth attrs on the request (dev/open mode) → legacy client-asserted author.
    req = _FakeRequest({"content": "hi", "author": "alice", "author_id": "web-alice"})
    event, _channel, err = await _bridge(tmp_path)._build_inbound_event(req)
    assert err is None and event is not None
    assert event.author == "alice" and event.author_id == "web-alice"


# ── per-user data scoping for React log/session/live endpoints ───────────


def _scoped_web_app(home: Path, master_key: str) -> tuple[web.Application, Path, Path]:
    turns_log = home / "turns.jsonl"
    events_log = home / "events.jsonl"
    app = web.Application(middlewares=[_make_auth_middleware(master_key)])
    app["identity_resolver"] = _resolver(home)
    web_ui.register_routes(
        app,
        turns_log=turns_log,
        events_log=events_log,
        home=home,
        react_app_dist=home / "missing-dist",
        turn_event_bus=TurnEventBus(),
    )
    return app, turns_log, events_log


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


async def test_user_turns_endpoint_filters_to_own_web_channel(tmp_path: Path) -> None:
    alice_key = issue_web_key(tmp_path, "alice", roles=["user"])
    bob_key = issue_web_key(tmp_path, "bob", roles=["user"])
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    app, turns_log, _ = _scoped_web_app(tmp_path, "master-secret")
    _jsonl(turns_log, [
        {"turn_id": "a1", "channel_id": "web-alice"},
        {"turn_id": "b1", "channel_id": "web-bob"},
    ])

    async with TestClient(TestServer(app)) as c:
        r = await c.get("/api/v1/turns", headers={"X-API-Key": alice_key})
        body = await r.json()
        assert r.status == 200
        assert [t["turn_id"] for t in body["data"]["turns"]] == ["a1"]

        denied = await c.get(
            "/api/v1/turns?channel=web-bob",
            headers={"X-API-Key": alice_key},
        )
        assert denied.status == 403

        bob = await c.get("/api/v1/turns", headers={"X-API-Key": bob_key})
        assert [t["turn_id"] for t in (await bob.json())["data"]["turns"]] == ["b1"]

        admin = await c.get("/api/v1/turns", headers={"X-API-Key": admin_key})
        assert [t["turn_id"] for t in (await admin.json())["data"]["turns"]] == ["a1", "b1"]


async def test_user_sessions_endpoint_filters_and_rejects_cross_channel(tmp_path: Path) -> None:
    alice_key = issue_web_key(tmp_path, "alice", roles=["user"])
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    app, turns_log, _ = _scoped_web_app(tmp_path, "master-secret")
    _jsonl(turns_log, [
        {
            "turn_id": "a1",
            "ts": "2026-06-21T01:00:00Z",
            "trigger": "user_message",
            "channel_id": "web-alice",
            "input": "alice prompt",
        },
        {
            "turn_id": "b1",
            "ts": "2026-06-21T01:00:00Z",
            "trigger": "scheduled_tick",
            "channel_id": "web-bob",
            "input": "bob prompt",
        },
        {
            "turn_id": "o1",
            "ts": "2026-06-21T01:00:00Z",
            "trigger": "ops_event",
            "channel_id": "discord-ops",
            "input": "ops prompt",
        },
    ])

    async with TestClient(TestServer(app)) as c:
        r = await c.get("/api/v1/sessions", headers={"X-API-Key": alice_key})
        body = await r.json()
        assert r.status == 200
        assert body["meta"]["total"] == 1
        assert body["data"]["sessions"][0]["channel_id"] == "web-alice"
        # Facets are part of the API response too; they must be derived from the
        # caller-visible session set rather than all sessions in the store.
        assert body["data"]["channels"] == ["web-alice"]
        assert body["data"]["triggers"] == ["user_message"]

        denied = await c.get(
            "/api/v1/sessions?channel=web-bob",
            headers={"X-API-Key": alice_key},
        )
        assert denied.status == 403

        admin = await c.get(
            "/api/v1/sessions?channel=discord-ops",
            headers={"X-API-Key": admin_key},
        )
        admin_body = await admin.json()
        assert admin.status == 200
        assert admin_body["meta"]["total"] == 1
        assert admin_body["data"]["sessions"][0]["channel_id"] == "discord-ops"


async def test_user_events_endpoints_filter_to_own_channel(tmp_path: Path) -> None:
    alice_key = issue_web_key(tmp_path, "alice", roles=["user"])
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    app, _, events_log = _scoped_web_app(tmp_path, "master-secret")
    _jsonl(events_log, [
        {"timestamp": "2026-06-21T01:00:00Z", "type": "tool", "channel_id": "web-alice"},
        {"timestamp": "2026-06-21T01:00:01Z", "type": "tool", "channel_id": "web-bob"},
        {
            "timestamp": "2026-06-21T01:00:02Z",
            "type": "alert",
            "extra": {"source_channel_id": "web-alice"},
        },
    ])

    async with TestClient(TestServer(app)) as c:
        legacy = await c.get("/api/events", headers={"X-API-Key": alice_key})
        assert [e["timestamp"] for e in (await legacy.json())["events"]] == [
            "2026-06-21T01:00:00Z",
            "2026-06-21T01:00:02Z",
        ]

        v1 = await c.get("/api/v1/events", headers={"X-API-Key": alice_key})
        assert [e["timestamp"] for e in (await v1.json())["data"]["events"]] == [
            "2026-06-21T01:00:00Z",
            "2026-06-21T01:00:02Z",
        ]

        admin = await c.get("/api/v1/events", headers={"X-API-Key": admin_key})
        assert len((await admin.json())["data"]["events"]) == 3


async def test_user_live_events_endpoint_filters_backfill_to_own_channel(tmp_path: Path) -> None:
    alice_key = issue_web_key(tmp_path, "alice", roles=["user"])
    app, turns_log, _ = _scoped_web_app(tmp_path, "master-secret")
    _jsonl(turns_log, [
        {
            "turn_id": "a1",
            "ts": "2026-06-21T01:00:00Z",
            "channel_id": "web-alice",
            "events": [
                {
                    "type": "text",
                    "phase": "chunk",
                    "turn_id": "a1",
                    "channel_id": "web-alice",
                    "seq": 1,
                    "ts": "2026-06-21T01:00:00Z",
                    "text": "a",
                }
            ],
        },
        {
            "turn_id": "b1",
            "ts": "2026-06-21T01:01:00Z",
            "channel_id": "web-bob",
            "events": [
                {
                    "type": "text",
                    "phase": "chunk",
                    "turn_id": "b1",
                    "channel_id": "web-bob",
                    "seq": 1,
                    "ts": "2026-06-21T01:01:00Z",
                    "text": "b",
                }
            ],
        },
    ])

    async with TestClient(TestServer(app)) as c:
        r = await c.get("/api/v1/live-events?once=1&limit=10", headers={"X-API-Key": alice_key})
        text = await r.text()
        assert r.status == 200
        assert "web-alice" in text
        assert "web-bob" not in text

        denied = await c.get(
            "/api/v1/live-events?channel=web-bob&once=1",
            headers={"X-API-Key": alice_key},
        )
        assert denied.status == 403


async def test_user_turn_events_rejects_wildcard_and_cross_channel(tmp_path: Path) -> None:
    alice_key = issue_web_key(tmp_path, "alice", roles=["user"])
    app, _, _ = _scoped_web_app(tmp_path, "master-secret")

    async with TestClient(TestServer(app)) as c:
        wildcard = await c.get("/api/v1/turn-events?channel=*", headers={"X-API-Key": alice_key})
        assert wildcard.status == 403

        denied = await c.get(
            "/api/v1/turn-events?channel=web-bob",
            headers={"X-API-Key": alice_key},
        )
        assert denied.status == 403

        allowed = await c.get(
            "/api/v1/turn-events?channel=web-alice",
            headers={"X-API-Key": alice_key},
        )
        assert allowed.status == 200
        allowed.close()
