"""Cultivate web contributions stay selected and server-authorized."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.dashboard_extensions import add_plugin_routes
from mimir.plugins.manager import load_plugins
from mimir_cultivate.board import worklink_evidence


def test_web_contributions_are_selected_only() -> None:
    core = load_plugins(enable="").policy
    assert not core.routes
    assert "cultivate-factory-runs" not in core.dashboard_entries
    assert not any(name == "chainlink_board" for _, name in core.server_hooks)

    policy = load_plugins(enable="cultivate").policy
    routes = policy.routes
    assert {path for _, path in routes} >= {
        "/api/v1/plugins/cultivate/factory-runs",
        "/api/v1/plugins/cultivate/factory-run",
        "/api/v1/plugins/cultivate/factory-run/archive",
        "/api/v1/plugins/cultivate/pr-board",
    }
    assert all(route.role == "admin" for route in routes.values())
    assert policy.dashboard_entries["cultivate-factory-runs"][0] == "cultivate"
    assert ("cultivate", "chainlink_board") in policy.server_hooks


@pytest.mark.asyncio
async def test_plugin_routes_check_admin_on_server(tmp_path: Path) -> None:
    app = web.Application()
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        path = "/api/v1/plugins/cultivate/factory-runs"
        assert (await client.get(path)).status == 401
        # The host's authentication middleware, not navigation visibility,
        # sets these trusted request properties.
        @web.middleware
        async def authenticated(request: web.Request, handler):
            request["auth_identity"] = object()
            return await handler(request)

        # Exercise the same mounted policy with a new app to avoid frozen routes.
    user_app = web.Application(middlewares=[authenticated])
    user_app["cultivate_home"] = tmp_path
    add_plugin_routes(user_app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(user_app)) as client:
        assert (await client.get(path)).status == 403
        assert (await client.post("/api/v1/plugins/cultivate/factory-run/archive?run_id=x")).status == 403

    @web.middleware
    async def admin(request: web.Request, handler):
        request["auth_is_master"] = True
        return await handler(request)

    admin_app = web.Application(middlewares=[admin])
    admin_app["cultivate_home"] = tmp_path
    add_plugin_routes(admin_app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(admin_app)) as client:
        response = await client.get(path)
        assert response.status == 200
        assert (await response.json())["data"]["runs"] == []
        assert (await client.get("/api/v1/plugins/cultivate/factory-run?run_id=../bad")).status == 400


def test_worklink_enrichment_reads_only_when_provider_supplies_it(tmp_path: Path) -> None:
    directory = tmp_path / "state/worklink/evidence"
    directory.mkdir(parents=True)
    (directory / "42-2.json").write_text(json.dumps({"attempt": 2, "status": "completed"}))
    (directory / "42-1.json").write_text(json.dumps({"attempt": 1, "status": "failed"}))
    assert worklink_evidence(tmp_path)[42]["attempt"] == 2


@pytest.mark.asyncio
async def test_selected_factory_api_projects_real_record_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir_cultivate import web as cultivate_web

    state = SimpleNamespace(
        issue_key="chainlink-42", valid=True, status="needs-human", mode="autonomous",
        pr_draft=True, lock="absent", dead_lock=False, lock_session="session-1",
        pr_url=None, next_present=False, gates={"brief": {"status": "approved"}},
        steps=(), slices=(), validator=None, terminal_result=None,
    )
    record = SimpleNamespace(
        run_id="chainlink-42", issue_id=42, status=state, sandbox="/sandbox", branch="topic",
        base_ref="main", session="session-1", controller_phase="parked", observed_at=None,
        controller_error=None,
    )
    monkeypatch.setattr(cultivate_web, "list_factory_records", lambda home: SimpleNamespace(records=[record]))
    monkeypatch.setattr(cultivate_web, "load_factory_record", lambda home, run_id: record if run_id == record.run_id else None)
    monkeypatch.setattr(cultivate_web, "factory_process_is_verified_dead", lambda record: False)

    @web.middleware
    async def admin(request: web.Request, handler):
        request["auth_is_master"] = True
        return await handler(request)

    app = web.Application(middlewares=[admin])
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        base = "/api/v1/plugins/cultivate"
        listing = await (await client.get(f"{base}/factory-runs")).json()
        assert listing["data"]["runs"][0]["issue_key"] == "chainlink-42"
        detail = await (await client.get(f"{base}/factory-run?run_id=chainlink-42")).json()
        assert detail["data"]["gates"]["brief"]["status"] == "approved"
        assert (await client.post(f"{base}/factory-run/archive?run_id=chainlink-42",
                                  json={"reason": "done"})).status == 409
