"""Per-user web auth + RBAC: middleware gate, /whoami, chat attribution (#726)."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.bridges.web_chat import WebChatBridge
from mimir.identities import IdentityResolver
from mimir.identities_populator import issue_web_key
from mimir import web_ui
from mimir.server import _make_auth_middleware
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


def test_admin_required_prefix_matching_is_segment_aware() -> None:
    from mimir.server import _is_admin_required

    assert _is_admin_required("/api/v1/saga") is True
    assert _is_admin_required("/api/v1/saga/sql") is True
    assert _is_admin_required("/api/v1/memory") is True
    assert _is_admin_required("/api/v1/memory/file") is True
    assert _is_admin_required("/api/v1/admin/config") is True
    assert _is_admin_required("/api/saga") is True
    assert _is_admin_required("/api/saga/sql") is True
    assert _is_admin_required("/api/memory") is True
    assert _is_admin_required("/api/memory/file") is True
    assert _is_admin_required("/api/v1/sagacity") is False
    assert _is_admin_required("/api/v1/memoryless") is False
    assert _is_admin_required("/api/sagacity") is False
    assert _is_admin_required("/api/memoryless") is False


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
