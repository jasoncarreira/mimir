"""Cultivate web contributions stay selected and server-authorized."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import chainlink_board, web_ui
from mimir.dashboard_extensions import add_plugin_routes
from mimir.plugins.manager import load_plugins
from mimir_cultivate.board import worklink_evidence
from mimir_cultivate.worklink.compute import LaunchHandle
from mimir_cultivate.worklink.factory_state import (
    FactoryRunRecord, load_factory_record, save_factory_record,
)


@web.middleware
async def _admin(request: web.Request, handler):
    request["auth_is_master"] = True
    return await handler(request)


def test_web_contributions_are_selected_only() -> None:
    core = load_plugins(enable="").policy
    assert not core.routes
    assert "cultivate-factory-runs" not in core.dashboard_entries
    assert not any(name == "chainlink_board" for _, name in core.server_hooks)
    assert "factory-runs" not in {
        entry.id for entry in web_ui.first_party_dashboard_extensions().manifests
    }

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
        base = "/api/v1/plugins/cultivate"
        for endpoint in ("/factory-runs", "/factory-run?run_id=x", "/pr-board"):
            assert (await client.get(base + endpoint)).status == 403
        assert (await client.post(f"{base}/factory-run/archive?run_id=x")).status == 403

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
async def test_chainlink_board_http_enrichment_requires_selected_cultivate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "state" / "worklink" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "545-2.json").write_text(json.dumps({
        "backend": "codex", "status": "completed", "branch": "issue/545-a2",
        "diff_stat": "3 files changed", "tests": {"cmd": "npm test", "exit_code": 0},
        "transcript": "state/worklink/transcripts/545-2.jsonl",
    }), encoding="utf-8")
    (evidence_dir / "545-1.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")

    async def run(home, args):
        assert home == tmp_path
        if args == ["export", "--json"]:
            return {"issues": [{"id": 545, "title": "Board", "status": "open"}]}, None
        assert args == ["issue", "show", "545", "--json"]
        return {"id": 545}, None

    monkeypatch.setattr(chainlink_board, "_run_chainlink_json", run)
    for selected in (False, True):
        app = web.Application()
        app["config"] = SimpleNamespace(plugin_manager=load_plugins(enable="cultivate" if selected else ""))
        web_ui.register_routes(app, turns_log=tmp_path / "turns", events_log=tmp_path / "events", home=tmp_path)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/v1/chainlink-board?issue=545")
            assert response.status == 200
            payload = (await response.json())["data"]
            assert payload["available"] is True
            issue = next(row for row in payload["issues"] if row["id"] == 545)
            assert issue["worklink"] == ({
                "issue": 545, "attempt": 2, "backend": "codex", "status": "completed",
                "branch": "issue/545-a2", "started_at": "", "finished_at": "",
                "diff_stat": "3 files changed", "tests": {"cmd": "npm test", "exit_code": 0},
                "pr_url": "", "blocked_reason": "",
                "transcript": "state/worklink/transcripts/545-2.jsonl",
                "transcript_href": "/api/v1/chainlink-board/artifact?path=state/worklink/transcripts/545-2.jsonl",
                "evidence_path": "state/worklink/evidence/545-2.json",
                "evidence_href": "/api/v1/chainlink-board/artifact?path=state/worklink/evidence/545-2.json",
            } if selected else None)
            assert payload["selected_issue"]["worklink"] == issue["worklink"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    ("missing_git", (None, "git CLI not on PATH")),
    ("git_timeout", (None, "git remote timed out after 8.0s")),
    ("missing_gh", ("owner/home", "gh CLI not on PATH")),
    ("gh_timeout", ("owner/home", "gh pr list timed out after 8.0s")),
    ("gh_auth", ("owner/home", "Bad credentials")),
    ("gh_json", ("owner/home", "gh output:")),
])
async def test_selected_pr_board_reports_command_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected: tuple[str | None, str],
) -> None:
    def run(args, **kwargs):
        command = args[0]
        if failure == f"missing_{command}":
            raise FileNotFoundError(command)
        if failure == f"{command}_timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if command == "git":
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/home.git", stderr="")
        if failure == "gh_auth":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="HTTP 401: Bad credentials")
        return subprocess.CompletedProcess(args, 0, stdout="{bad json", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    app = web.Application(middlewares=[_admin])
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/v1/plugins/cultivate/pr-board")
        assert response.status == 200
        board = (await response.json())["data"]
        assert board["available"] is False
        assert board["repo"] == expected[0]
        assert expected[1] in board["error"]
        assert board["pull_requests"] == []


@pytest.mark.asyncio
async def test_selected_pr_board_normalizes_open_prs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:owner/home.git\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([
            {"number": 12, "title": "Review", "url": "https://github.com/owner/home/pull/12",
             "author": {"login": "alice"}, "createdAt": "2026-06-27T12:00:00Z",
             "reviewDecision": "REVIEW_REQUIRED", "isDraft": False},
            {"number": True, "title": "invalid"},
        ]), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    app = web.Application(middlewares=[_admin])
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/v1/plugins/cultivate/pr-board")
        assert response.status == 200
        board = (await response.json())["data"]
        assert board["available"] is True
        assert board["repo"] == "owner/home"
        assert board["total_count"] == 1
        assert board["truncated"] is False
        assert board["pull_requests"] == [{
            "number": 12, "title": "Review", "url": "https://github.com/owner/home/pull/12",
            "author": "alice", "created_at": "2026-06-27T12:00:00Z",
            "review_decision": "REVIEW_REQUIRED", "is_draft": False,
        }]
    assert calls[1] == ["gh", "pr", "list", "-R", "owner/home", "--state", "open",
                        "--json", "number,title,url,author,createdAt,reviewDecision,isDraft"]


@pytest.mark.asyncio
async def test_selected_factory_archive_persists_audit_and_denies_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import event_logger

    home = tmp_path / "home"
    home.mkdir()
    record = FactoryRunRecord(
        run_id="1620", issue_id=1620, attempt=2, repository="owner/repo",
        base_ref="main", branch="worklink/1620", launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "sandbox"), session="session-1",
        handle=LaunchHandle("local_subprocess", "987654", 123, shim_pid=987654),
        status=None, observed_at="2026-09-11T10:00:00Z", controller_phase="running",
    )
    source = save_factory_record(home, record)
    original = source.read_bytes()

    def missing_process(pid, signal):
        assert pid == 987654 and signal == 0
        raise ProcessLookupError

    monkeypatch.setattr("mimir_cultivate.worklink.factory_state.os.kill", missing_process)
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_logger, "_logger", event_logger.EventLogger(events, "test-session"))

    @web.middleware
    async def admin(request: web.Request, handler):
        request["auth_identity"] = SimpleNamespace(canonical="bob")
        request["auth_is_master"] = True
        return await handler(request)

    app = web.Application(middlewares=[admin])
    app["cultivate_home"] = home
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    base = "/api/v1/plugins/cultivate"
    async with TestClient(TestServer(app)) as client:
        listing = await client.get(f"{base}/factory-runs")
        assert (await listing.json())["data"]["runs"][0]["clearable"] is True
        response = await client.post(
            f"{base}/factory-run/archive?run_id=1620",
            json={"reason": "  clear stale run  ", "operator": "mallory"},
        )
        assert response.status == 200
        assert (await response.json())["data"] == {"run_id": "1620", "archived": True}
        assert not source.exists()
        assert load_factory_record(home, "1620") is None
        assert (await (await client.get(f"{base}/factory-runs")).json())["data"]["runs"] == []
        assert (await client.post(f"{base}/factory-run/archive?run_id=1620",
                                  json={"reason": "repeat"})).status == 404
    audit, = [json.loads(line) for line in events.read_text().splitlines()]
    assert audit["type"] == "worklink_factory_record_archived"
    assert audit["source"] == "web_ui"
    assert audit["reason"] == "web archive by bob: clear stale run"
    assert (audit["run_id"], audit["issue_id"], audit["attempt"], audit["session"], audit["phase"]) == (
        "1620", 1620, 2, "session-1", "running",
    )
    assert Path(audit["archive_path"]).read_bytes() == original
    assert Path(audit["archive_path"]) == source.parent / "archive" / "1620-attempt-2.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [{}, {"reason": "  "}, {"reason": 1}, [], None])
async def test_selected_factory_archive_requires_reason(tmp_path: Path, reason: object) -> None:
    record = FactoryRunRecord(
        run_id="1620", issue_id=1620, attempt=1, repository="owner/repo",
        base_ref="main", branch="worklink/1620", launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "sandbox"), session=None, handle=None, status=None,
        observed_at=None, controller_phase="terminal",
    )
    source = save_factory_record(tmp_path, record)
    app = web.Application(middlewares=[_admin])
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/v1/plugins/cultivate/factory-run/archive?run_id=1620", json=reason,
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_reason"
    assert source.exists()


@pytest.mark.asyncio
async def test_selected_factory_archive_refuses_unverified_process(tmp_path: Path) -> None:
    record = FactoryRunRecord(
        run_id="1620", issue_id=1620, attempt=1, repository="owner/repo",
        base_ref="main", branch="worklink/1620", launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "sandbox"), session=None, handle=None, status=None,
        observed_at=None, controller_phase="running",
    )
    source = save_factory_record(tmp_path, record)
    app = web.Application(middlewares=[_admin])
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/v1/plugins/cultivate/factory-run/archive?run_id=1620",
            json={"reason": "cleanup"},
        )
        assert response.status == 409
        assert (await response.json())["error"]["code"] == "run_not_verified_dead"
    assert source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("process_state", [
    "live", "permission_denied", "unreadable_ticks", "missing_handle", "missing_ticks",
])
async def test_selected_factory_archive_requires_verified_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, process_state: str,
) -> None:
    from mimir_cultivate.worklink import factory_state

    def probe(pid: int, signal: int) -> None:
        assert (pid, signal) == (987654, 0)
        if process_state == "permission_denied":
            raise PermissionError

    monkeypatch.setattr(factory_state.os, "kill", probe)
    monkeypatch.setattr(factory_state, "process_is_zombie", lambda pid: False)
    monkeypatch.setattr(factory_state, "process_start_ticks", lambda pid: (
        None if process_state == "unreadable_ticks" else 123
    ))
    record = FactoryRunRecord(
        run_id="1620", issue_id=1620, attempt=1, repository="owner/repo",
        base_ref="main", branch="worklink/1620", launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "sandbox"), session=None,
        handle=None if process_state == "missing_handle" else LaunchHandle(
            substrate="local_subprocess", identifier="987654", shim_pid=987654,
            process_start_ticks=None if process_state == "missing_ticks" else 123,
        ),
        status=None, observed_at=None, controller_phase="running",
    )
    source = save_factory_record(tmp_path, record)
    original = source.read_bytes()
    app = web.Application(middlewares=[_admin])
    app["cultivate_home"] = tmp_path
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/v1/plugins/cultivate/factory-run/archive?run_id=1620",
            json={"reason": "cleanup"},
        )
        assert response.status == 409
        assert (await response.json())["error"]["code"] == "run_not_verified_dead"
    assert source.read_bytes() == original
    assert not (source.parent / "archive").exists()


# Path safety for the selected factory-run endpoints. Restores the coverage the
# pre-plugin ``/api/v1/factory-runs/{run_id}`` tests in tests/test_web_ui.py
# carried; ``run_id`` now arrives as a query parameter instead of a path segment.

_FACTORY_BASE = "/api/v1/plugins/cultivate"


def _factory_app(home: Path) -> web.Application:
    app = web.Application(middlewares=[_admin])
    app["cultivate_home"] = home
    add_plugin_routes(app, load_plugins(enable="cultivate").policy)
    return app


async def _request_run(client: TestClient, operation: str, run_id: str):
    if operation == "archive":
        return await client.post(
            f"{_FACTORY_BASE}/factory-run/archive", params={"run_id": run_id},
            json={"reason": "cleanup"},
        )
    return await client.get(f"{_FACTORY_BASE}/factory-run", params={"run_id": run_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["detail", "archive"])
@pytest.mark.parametrize(
    ("unsafe", "error_code"),
    [
        ("valid..child", "invalid_run_id"),
        ("valid/child", "invalid_run_id"),
        ("valid\\child", "invalid_run_id"),
        (".hidden", "invalid_run_id"),
        ("/etc/passwd", "invalid_run_id"),
        ("../834", "invalid_run_id"),
        ("", "missing_run_id"),
        ("   ", "missing_run_id"),
    ],
    ids=["dot-dot", "slash", "backslash", "leading-dot", "absolute", "parent", "empty", "blank"],
)
async def test_selected_factory_run_rejects_unsafe_ids_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, unsafe: str, error_code: str,
) -> None:
    from mimir_cultivate import web as cultivate_web

    home = tmp_path / "home"
    home.mkdir()
    # A sibling record an escaping id could name, so a missed check is visible.
    outside = tmp_path / "834.json"
    outside.write_text("{}", encoding="utf-8")

    def unexpected(*_args):
        pytest.fail("unsafe run id reached load_factory_record")

    monkeypatch.setattr(cultivate_web, "load_factory_record", unexpected)
    async with TestClient(TestServer(_factory_app(home))) as client:
        response = await _request_run(client, operation, unsafe)
        assert response.status == 400
        data = await response.json()
        assert data["ok"] is False
        assert data["error"]["code"] == error_code
    assert outside.read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["detail", "archive"])
async def test_selected_factory_run_refuses_symlinked_record(
    tmp_path: Path, operation: str,
) -> None:
    home = tmp_path / "home"
    factory_dir = home / "state" / "worklink" / "factory-runs"
    factory_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (factory_dir / "834.json").symlink_to(outside)
    async with TestClient(TestServer(_factory_app(home))) as client:
        response = await _request_run(client, operation, "834")
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "run_invalid"
    assert outside.read_text(encoding="utf-8") == "{}"
    assert (factory_dir / "834.json").is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["detail", "archive"])
async def test_selected_factory_run_refuses_symlinked_parent_directory(
    tmp_path: Path, operation: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    factory_dir = outside / "worklink" / "factory-runs"
    factory_dir.mkdir(parents=True)
    (factory_dir / "834.json").write_text("{}", encoding="utf-8")
    (home / "state").symlink_to(outside, target_is_directory=True)
    async with TestClient(TestServer(_factory_app(home))) as client:
        response = await _request_run(client, operation, "834")
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "run_invalid"
    assert (factory_dir / "834.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["detail", "archive"])
async def test_selected_factory_run_not_found(tmp_path: Path, operation: str) -> None:
    home = tmp_path / "home"
    (home / "state" / "worklink" / "factory-runs").mkdir(parents=True)
    async with TestClient(TestServer(_factory_app(home))) as client:
        response = await _request_run(client, operation, "999")
        assert response.status == 404
        data = await response.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "run_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["detail", "archive"])
async def test_selected_factory_run_malformed_record(tmp_path: Path, operation: str) -> None:
    home = tmp_path / "home"
    factory_dir = home / "state" / "worklink" / "factory-runs"
    factory_dir.mkdir(parents=True)
    record = factory_dir / "834.json"
    record.write_text("not valid json", encoding="utf-8")
    async with TestClient(TestServer(_factory_app(home))) as client:
        response = await _request_run(client, operation, "834")
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "run_invalid"
        # The listing skips the unreadable record instead of failing.
        listing = await client.get(f"{_FACTORY_BASE}/factory-runs")
        assert listing.status == 200
        assert (await listing.json())["data"]["runs"] == []
    assert record.read_text(encoding="utf-8") == "not valid json"


@pytest.mark.asyncio
async def test_selected_factory_archive_rejects_malformed_request_body(tmp_path: Path) -> None:
    record = FactoryRunRecord(
        run_id="1620", issue_id=1620, attempt=1, repository="owner/repo",
        base_ref="main", branch="worklink/1620", launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "sandbox"), session=None, handle=None, status=None,
        observed_at=None, controller_phase="terminal",
    )
    source = save_factory_record(tmp_path, record)
    async with TestClient(TestServer(_factory_app(tmp_path))) as client:
        response = await client.post(
            f"{_FACTORY_BASE}/factory-run/archive", params={"run_id": "1620"},
            data=b"{not json", headers={"Content-Type": "application/json"},
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "invalid_reason"
    assert source.exists()
