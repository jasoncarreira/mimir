"""Cultivate's authenticated factory-run and pull-request read APIs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from mimir.dashboard_extensions import DashboardExtensionManifest
from mimir.web_contracts import json_error, json_success, list_meta

from .worklink.backends.feature_factory import _RUN_ID
from .worklink.factory_state import (
    FactoryRecordError, FactoryRunRecord, archive_factory_record,
    factory_process_is_verified_dead, list_factory_records, load_factory_record,
)
from .board import build_pr_board_payload


def _home(request: web.Request) -> Path | None:
    home = request.app.get("cultivate_home")
    if home is None:
        home = getattr(request.app.get("config"), "home", None)
    if home is None:
        home = os.environ.get("MIMIR_HOME")
    return Path(home) if home else None


def _summary(record: FactoryRunRecord) -> dict[str, Any]:
    state = record.status
    result = {
        "run_id": record.run_id,
        "clearable": factory_process_is_verified_dead(record),
        "issue_key": (state.issue_key if state is not None else None) or str(record.issue_id),
        "valid": state.valid if state is not None else False,
        "sandbox_path": record.sandbox,
        "status": state.status if state is not None else None,
        "mode": state.mode if state is not None else "autonomous",
        "branch": record.branch,
        "pr_base": record.base_ref,
        "pr_draft": state.pr_draft if state is not None else True,
        "lock": state.lock if state is not None else "absent",
        "dead_lock": state.dead_lock if state is not None else False,
        "lock_session": state.lock_session if state is not None else record.session,
        "pr_url": state.pr_url if state is not None else None,
        "controller_phase": record.controller_phase,
        "observed_at": record.observed_at,
        "controller_error": record.controller_error,
    }
    if state is not None and state.next_present:
        result["next"] = state.next
    return result


def _detail(record: FactoryRunRecord) -> dict[str, Any]:
    result = _summary(record)
    state = record.status
    result.update({
        "gates": state.gates if state is not None and state.gates is not None else {},
        "steps": [f"{row['agent']}:{row['status']}({row['attempts']})" for row in (state.steps or ())] if state else [],
        "slices": [f"{row['id']}:{row['status']}({row['attempts']})" for row in (state.slices or ())] if state else [],
        "validator": state.validator["verdict"] if state and state.validator else None,
        "terminal_result": state.terminal_result if state else None,
    })
    return result


def _requested_run(home: Path | None, run_id: str) -> FactoryRunRecord | web.Response:
    run_id = run_id.strip()
    if not run_id:
        return json_error("missing_run_id", "run_id is required", status=400)
    if not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        return json_error("invalid_run_id", "run_id is invalid", status=400)
    if home is None:
        return json_error("home_not_configured", "home path not configured", status=503)
    try:
        record = load_factory_record(home, run_id)
    except FactoryRecordError:
        return json_error("run_invalid", "factory run record is malformed", status=400)
    if record is None:
        return json_error("run_not_found", f"factory run not found: {run_id}", status=404)
    return record


async def factory_runs(request: web.Request) -> web.Response:
    home = _home(request)

    def read() -> list[dict[str, Any]]:
        if home is None:
            return []
        try:
            return [_summary(record) for record in list_factory_records(home).records[:1000]]
        except FactoryRecordError:
            return []

    runs = await asyncio.to_thread(read)
    return json_success({"runs": runs}, meta=list_meta(cursor=None, limit=None, total=len(runs), truncated=False))


async def factory_run(request: web.Request) -> web.Response:
    record = await asyncio.to_thread(_requested_run, _home(request), request.query.get("run_id", ""))
    return record if isinstance(record, web.Response) else json_success(_detail(record))


async def archive_run(request: web.Request) -> web.Response:
    home = _home(request)
    record = await asyncio.to_thread(_requested_run, home, request.query.get("run_id", ""))
    if isinstance(record, web.Response):
        return record
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return json_error("invalid_reason", "reason must be a nonempty string", status=400)
    reason = body.get("reason") if isinstance(body, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return json_error("invalid_reason", "reason must be a nonempty string", status=400)
    if not factory_process_is_verified_dead(record):
        return json_error("run_not_verified_dead", "cannot prove factory process is dead", status=409)
    from mimir.event_logger import log_durable_event_sync

    identity = request.get("auth_identity")
    operator = identity.canonical if identity is not None else "master"
    try:
        await asyncio.to_thread(
            archive_factory_record, home, record,
            event_logger=log_durable_event_sync, source_kind="web_ui",
            reason=f"web archive by {operator}: {reason.strip()}",
        )
    except FactoryRecordError:
        return json_error("run_archive_failed", "factory run could not be archived", status=500)
    return json_success({"run_id": record.run_id, "archived": True})


async def pr_board(request: web.Request) -> web.Response:
    return json_success(await build_pr_board_payload(_home(request)))


def register_web(registrar: Any) -> None:
    base = "/api/v1/plugins/cultivate"
    registrar.register_route("GET", f"{base}/factory-runs", factory_runs, role="admin")
    registrar.register_route("GET", f"{base}/factory-run", factory_run, role="admin")
    registrar.register_route("POST", f"{base}/factory-run/archive", archive_run, role="admin")
    registrar.register_route("GET", f"{base}/pr-board", pr_board, role="admin")
    registrar.register_dashboard_entry("cultivate-factory-runs", DashboardExtensionManifest(
        id="cultivate-factory-runs", route_path="/cultivate/factory-runs",
        label="Factory Runs", nav_position=36, bundle="cultivate.v1.js", requires_role="admin",
    ))
