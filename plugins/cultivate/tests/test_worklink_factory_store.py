"""Quiescent OpenCode store reclamation and session continuity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import sys
import asyncio
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import autonomy, orchestrator
from mimir_cultivate.worklink.backends.base import LaunchHandle
from mimir_cultivate.worklink.checkout import prune_attempt_checkouts
from mimir_cultivate.worklink.factory_state import (
    FactoryRunRecord, FactoryStatus, process_start_ticks,
)
from mimir_cultivate.worklink.factory_store import _prune_db, clear_prunable_runtime, prune_factory_store


def _record(tmp_path: Path, handle: LaunchHandle) -> tuple[FactoryRunRecord, Path]:
    attempt = tmp_path / "repo" / ".worklink" / "repo" / "1983-1"
    sandbox = attempt / "checkout" / ".factory-sandboxes" / "chainlink-1983"
    sandbox.mkdir(parents=True)
    db = attempt / ".factory-runtime" / "data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    record = FactoryRunRecord(
        run_id="chainlink-1983", issue_id=1983, attempt=1, repository="owner/repo",
        base_ref="main", branch="issue/1983-a1", launcher=str(tmp_path / "launcher"),
        sandbox=str(sandbox), session="factory-session", handle=handle,
        status=FactoryStatus("chainlink-1983", True, str(sandbox), status="needs-human"),
        observed_at=datetime.now(UTC).isoformat(), controller_phase="parked",
        children_reaped=True,
    )
    return record, db


def _dead_handle() -> LaunchHandle:
    process = subprocess.Popen(["true"])
    process.wait()
    return LaunchHandle("local_subprocess", str(process.pid), 1, process.pid)


@pytest.mark.skipif(sys.platform != "linux", reason="birth ticks require procfs")
def test_factory_store_refuses_live_driver(tmp_path: Path) -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        ticks = process_start_ticks(process.pid)
        assert ticks is not None
        record, db = _record(tmp_path, LaunchHandle("local_subprocess", str(process.pid), ticks, process.pid))
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE event (type TEXT)")
            conn.execute("INSERT INTO event VALUES ('message.part.updated')")
        with pytest.raises(RuntimeError, match="driver death is not verified"):
            prune_factory_store(record)
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT count(*) FROM event").fetchone() == (1,)
    finally:
        process.terminate()
        process.wait()


def test_factory_store_refuses_other_attempt_and_symlink(tmp_path: Path) -> None:
    record, db = _record(tmp_path, _dead_handle())
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event (type TEXT)")
        conn.execute("INSERT INTO event VALUES ('message.part.updated')")
    other = Path(record.sandbox).parents[2].parent / "1983-2" / "checkout" / ".factory-sandboxes" / record.run_id
    other.mkdir(parents=True)
    foreign = replace(record, sandbox=str(other), status=replace(record.status, sandbox_path=str(other)))
    with pytest.raises(RuntimeError, match="outside the attempt boundary"):
        prune_factory_store(foreign)
    legacy = replace(record, run_id="1983", status=replace(record.status, run_id="1983"))
    with pytest.raises(RuntimeError, match="outside the attempt boundary"):
        prune_factory_store(legacy)
    other_parent = Path(record.sandbox).parent.parent / "unrelated" / record.run_id
    other_parent.mkdir(parents=True)
    foreign = replace(record, sandbox=str(other_parent), status=replace(record.status, sandbox_path=str(other_parent)))
    with pytest.raises(RuntimeError, match="outside the attempt boundary"):
        prune_factory_store(foreign)
    db.rename(db.with_suffix(".kept"))
    db.symlink_to(db.with_suffix(".kept"))
    with pytest.raises(RuntimeError, match="not a regular file"):
        prune_factory_store(record)
    with sqlite3.connect(db.with_suffix(".kept")) as conn:
        assert conn.execute("SELECT count(*) FROM event").fetchone() == (1,)


def test_factory_store_refuses_linked_file_and_symlink_directory(tmp_path: Path) -> None:
    _, db = _record(tmp_path, _dead_handle())
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event (type TEXT)")
    linked = tmp_path / "linked.db"
    os.link(db, linked)
    with pytest.raises(RuntimeError, match="not a regular file"):
        _prune_db(db)
    linked.unlink()
    data = db.parent.parent
    target = data.with_name("outside")
    data.rename(target)
    data.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="directory is unsafe"):
        _prune_db(db)


def test_factory_store_requires_verified_death(tmp_path: Path) -> None:
    record, db = _record(tmp_path, LaunchHandle("local_subprocess", "invalid", None))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event (type TEXT)")
        conn.execute("INSERT INTO event VALUES ('message.part.updated')")
    with pytest.raises(RuntimeError, match="driver death is not verified"):
        prune_factory_store(record)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM event").fetchone() == (1,)


@pytest.mark.asyncio
async def test_factory_finish_refuses_running_projection(tmp_path: Path) -> None:
    record, db = _record(tmp_path, _dead_handle())
    record = replace(record, status=replace(record.status, status="running"))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event (type TEXT)")
        conn.execute("INSERT INTO event VALUES ('message.part.updated')")
    runner = orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)
    with pytest.raises(orchestrator.WorklinkError, match="parked or terminal"):
        await runner._finish_factory_070(
            issue=orchestrator.IssueContext(1983, "Factory", "run", set()),
            claim_record=object(), claims=object(), backend=object(), compute=object(),
            factory_record=record, test_cmd="pytest", runner=object(), started_at=datetime.now(UTC),
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM event").fetchone() == (1,)


def test_factory_store_refuses_busy_wal_reader(tmp_path: Path) -> None:
    record, db = _record(tmp_path, _dead_handle())
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE event (type TEXT)")
        writer.execute("INSERT INTO event VALUES ('message.part.updated')")
        with sqlite3.connect(db) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM event").fetchall()
            writer.execute("INSERT INTO event VALUES ('message.updated')")
            writer.commit()
            with pytest.raises(RuntimeError, match="reader or writer"):
                _prune_db(db)
        assert writer.execute("SELECT count(*) FROM event").fetchone() == (2,)


@pytest.mark.asyncio
async def test_factory_park_prunes_without_touching_recovery_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, db = _record(tmp_path, _dead_handle())
    run_json = Path(record.sandbox) / ".factory" / record.run_id / "run.json"
    run_json.parent.mkdir(parents=True)
    run_json.write_text('{"status": "needs-human"}')
    artifact = Path(record.sandbox) / "evidence.txt"
    artifact.write_text("retained artifact")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event (type TEXT)")
        conn.execute("INSERT INTO event VALUES ('message.part.updated')")
    transitions: list[str] = []
    monkeypatch.setattr(orchestrator, "_record_factory_milestone", lambda *a, **kw: None)
    result = await orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)._finish_factory_070(
        issue=orchestrator.IssueContext(1983, "Factory", "run", set()),
        claim_record=SimpleNamespace(attempt=1, budget_attempt=None),
        claims=SimpleNamespace(transition_issue=lambda issue, **kw: transitions.append(kw["status"])),
        backend=object(), compute=object(), factory_record=record,
        test_cmd="pytest", runner=object(), started_at=datetime.now(UTC),
    )
    assert result.status == "needs-human" and transitions == ["blocked"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM event").fetchone() == (0,)
    assert run_json.read_text() == '{"status": "needs-human"}'
    assert artifact.read_text() == "retained artifact"


def test_factory_runtime_cleanup_refuses_invalid_attempt(tmp_path: Path) -> None:
    attempt = tmp_path / "unrelated"
    (attempt / "checkout").mkdir(parents=True)
    runtime = attempt / ".factory-runtime"
    runtime.mkdir()
    (runtime / "keep").write_text("evidence")
    with pytest.raises(RuntimeError, match="outside a checkout attempt"):
        clear_prunable_runtime(attempt)
    assert (runtime / "keep").read_text() == "evidence"
    (attempt / "checkout").rmdir()
    valid_name = attempt.with_name("1983-1")
    attempt.rename(valid_name)
    clear_prunable_runtime(valid_name)
    assert list((valid_name / ".factory-runtime").iterdir()) == []


@pytest.mark.parametrize("reaped", [False, True])
def test_ttl_partial_worker_layout_without_checkout(tmp_path, monkeypatch, reaped):
    from mimir_cultivate.worklink import worker_client
    record, db = _record(tmp_path, _dead_handle())
    record = replace(record, children_reaped=reaped)
    checkout = Path(record.sandbox).parent.parent
    attempt = checkout.parent
    db.write_bytes(b"retained partial store")
    shutil.rmtree(checkout)
    # Keep the real lexical worker binding: the old code attempted to reopen
    # this nonexistent checkout through the executor and could never reclaim it.
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", attempt.parent.parent)
    assert worker_client.factory_checkout_for_path(checkout) is not None
    monkeypatch.setattr(worker_client, "run_factory_control",
                        lambda *a, **kw: pytest.fail("missing checkout cannot be a worker cwd"))
    assert autonomy._prepare_factory_runtime_prune(attempt, [record]) is reaped
    if reaped:
        assert list((attempt / ".factory-runtime").iterdir()) == []
    else:
        assert db.read_bytes() == b"retained partial store"


def test_ttl_partial_layout_permission_failure_is_retained(tmp_path, monkeypatch):
    from mimir_cultivate.worklink import factory_store, worker_client
    record, db = _record(tmp_path, _dead_handle())
    checkout = Path(record.sandbox).parent.parent
    attempt = checkout.parent
    db.write_bytes(b"worker-owned")
    shutil.rmtree(checkout)
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", attempt.parent.parent)
    def denied(path):
        raise PermissionError("worker-owned runtime")
    monkeypatch.setattr(factory_store, "_clear_runtime", denied)
    assert not autonomy._prepare_factory_runtime_prune(attempt, [record])
    assert db.read_bytes() == b"worker-owned"


def test_factory_runtime_cleanup_refuses_symlink(tmp_path: Path) -> None:
    attempt = tmp_path / "1983-1"
    (attempt / "checkout").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("evidence")
    (attempt / ".factory-runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a directory"):
        clear_prunable_runtime(attempt)
    assert (outside / "keep").read_text() == "evidence"


@pytest.mark.parametrize("has_runtime", [False, True])
def test_runtime_prune_refuses_unverified_matching_record(tmp_path, monkeypatch, has_runtime):
    from mimir_cultivate.worklink import factory_store
    record, db = _record(tmp_path, LaunchHandle("local_subprocess", "invalid", None))
    attempt = Path(record.sandbox).parents[2]
    if has_runtime:
        db.write_bytes(b"retained")
    else:
        shutil.rmtree(attempt / ".factory-runtime")
    assert record.children_reaped is True  # Isolate verified death from the other guard.
    monkeypatch.setattr(factory_store, "clear_prunable_runtime",
                        lambda path: pytest.fail("unverified driver dispatched cleanup"))
    events = []
    monkeypatch.setattr(autonomy, "log_event_sync", lambda name, **kw: events.append((name, kw)))
    assert not autonomy._prepare_factory_runtime_prune(attempt, [record])
    assert events[-1][1]["reason"] == "driver death is not verified"
    if has_runtime:
        assert db.read_bytes() == b"retained"


def test_factory_runtime_removed_with_prunable_checkout(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "worklink.yaml").write_text("defaults:\n  reaper_ttl_s: 3600\n  factory_resumable_retention_s: 3600\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    attempt = tmp_path / ".worklink" / "repo" / "1983-1"
    checkout = attempt / "checkout"
    checkout.mkdir(parents=True)
    runtime = attempt / ".factory-runtime" / "data" / "opencode"
    runtime.mkdir(parents=True)
    (runtime / "opencode.db").write_bytes(b"old store")
    old = datetime.now(UTC) - timedelta(days=2)
    os.utime(attempt, (old.timestamp(), old.timestamp()))
    record, _ = _record(tmp_path / "record", _dead_handle())
    sandbox = checkout / ".factory-sandboxes" / record.run_id
    record = replace(record, sandbox=str(sandbox), status=None)
    autonomy_prepare = autonomy._prepare_factory_runtime_prune
    monkeypatch.setattr(autonomy, "_prepare_factory_runtime_prune",
                        lambda child, records: autonomy_prepare(child, [record]))
    # Inspect immediately before the enclosing rmtree, not after it.
    from mimir_cultivate.worklink import checkout as checkout_module
    real_rmtree = checkout_module.shutil.rmtree
    def checked_rmtree(path, *args, **kwargs):
        if Path(path) == attempt:
            assert list((attempt / ".factory-runtime").iterdir()) == []
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(checkout_module.shutil, "rmtree", checked_rmtree)
    assert autonomy.prune_stale_attempt_checkouts_for_home(home, repo=repo) == [attempt]
    assert not attempt.exists()


def test_attempt_pruner_requires_runtime_preparation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    attempt = tmp_path / ".worklink" / "repo" / "1983-1"
    attempt.mkdir(parents=True)
    old = datetime.now(UTC) - timedelta(days=2)
    os.utime(attempt, (old.timestamp(), old.timestamp()))
    assert prune_attempt_checkouts(repo, older_than=timedelta(days=1), now=datetime.now(UTC),
                                   prepare_prune=lambda child: False) == []
    assert attempt.is_dir()


def test_event_filter_and_vacuum_are_exact(tmp_path):
    _, db = _record(tmp_path, _dead_handle())
    retained = ["message.updated", "session.updated", "worklink", "message.part.updated.2"]
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event (type TEXT, data BLOB)")
        conn.executemany("INSERT INTO event VALUES (?, ?)",
                         [(name, b"keep") for name in retained] +
                         [(name, b"x" * 8192) for name in
                          ["message.part.updated", "message.part.updated.1"] for _ in range(100)])
    metrics = _prune_db(db)
    assert metrics["rows_deleted"] == 200
    assert metrics["bytes_after"] < metrics["bytes_before"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT type, data FROM event").fetchall() == [(name, b"keep") for name in retained]


def test_unacknowledged_specialists_refuse_prune_and_ttl(tmp_path):
    record, db = _record(tmp_path, _dead_handle())
    record = replace(record, children_reaped=False)
    db.write_bytes(b"untouched")
    with pytest.raises(RuntimeError, match="specialist reaping"):
        prune_factory_store(record)
    attempt = Path(record.sandbox).parents[2]
    assert not autonomy._prepare_factory_runtime_prune(attempt, [record])
    assert not autonomy._prepare_factory_runtime_prune(attempt, [])
    assert db.read_bytes() == b"untouched"


def test_ttl_matches_exact_attempt_path_not_name(tmp_path):
    from mimir_cultivate.worklink.factory_state import FactoryRunRecord
    record, db = _record(tmp_path, _dead_handle())
    db.write_bytes(b"old store")
    attempt = Path(record.sandbox).parents[2]
    other, _ = _record(tmp_path / "other", _dead_handle())
    other = replace(other, children_reaped=False)
    assert autonomy._prepare_factory_runtime_prune(attempt, [other, record])
    assert list((attempt / ".factory-runtime").iterdir()) == []
    # Old records load conservatively and invalid acknowledgement types refuse.
    payload = record.to_json()
    payload.pop("children_reaped")
    assert not FactoryRunRecord.from_json(payload).children_reaped
    payload["children_reaped"] = "yes"
    with pytest.raises(RuntimeError, match="children_reaped"):
        FactoryRunRecord.from_json(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,ack", [("feature_factory", True), ("opencode", False)])
async def test_compute_reaping_ack_only_for_factory_worker_result(tmp_path, backend, ack):
    from mimir_cultivate.worklink.compute import LocalSubprocessComputeBackend, WorkSpec
    from mimir_cultivate.contained_execution import CollectedExecutionResult
    handle = _dead_handle()
    compute = LocalSubprocessComputeBackend()
    compute._handles[handle.identifier] = handle
    spec = SimpleNamespace(backend=backend)
    async def collected():
        return CollectedExecutionResult(0, b"", b"", False, False, 0, 0)
    task = asyncio.create_task(collected())
    compute._jobs[handle.identifier] = (task, spec, ())
    result = await compute.wait(handle, timeout_s=2)
    assert result.children_reaped is ack
    async def refused():
        raise RuntimeError("worklink_factory_reap_refused")
    compute._jobs[handle.identifier] = (asyncio.create_task(refused()), spec, ())
    with pytest.raises(RuntimeError, match="reap_refused"):
        await compute.wait(handle, timeout_s=2)
    from mimir_cultivate.worklink.compute import ComputeResult
    assert not ComputeResult(0, "", "").children_reaped


def test_ttl_unacknowledged_record_without_runtime_is_prunable(tmp_path):
    record, db = _record(tmp_path, _dead_handle())
    record = replace(record, children_reaped=False)
    runtime = db.parents[2]
    shutil.rmtree(runtime)
    attempt = Path(record.sandbox).parents[2]
    assert autonomy._prepare_factory_runtime_prune(attempt, [record])
    assert autonomy._prepare_factory_runtime_prune(attempt, [replace(record, children_reaped=True)])


@pytest.mark.parametrize("case", ["ack", "unknown", "unverified", "cleanup"])
def test_runtime_ttl_refusal_emits_reason_and_keeps_store(tmp_path, monkeypatch, case):
    from mimir_cultivate.worklink import factory_store
    record, db = _record(tmp_path, _dead_handle())
    db.write_bytes(b"retained")
    attempt = Path(record.sandbox).parents[2]
    if case == "ack":
        record = replace(record, children_reaped=False)
    elif case == "unverified":
        record = replace(record, handle=LaunchHandle("local_subprocess", "invalid", None))
    calls = []
    def clear(path):
        calls.append(path)
        raise PermissionError("worker-owned runtime")
    monkeypatch.setattr(factory_store, "clear_prunable_runtime", clear)
    events = []
    monkeypatch.setattr(autonomy, "log_event_sync", lambda name, **kw: events.append((name, kw)))
    assert not autonomy._prepare_factory_runtime_prune(attempt, [] if case == "unknown" else [record])
    assert calls == ([attempt] if case == "cleanup" else [])
    assert db.read_bytes() == b"retained"
    assert len(events) == 1
    assert events[0][0] == "worklink_factory_runtime_prune_refused"
    assert events[0][1]["attempt_path"] == str(attempt)
    assert {"ack": "specialist reaping", "unknown": "no authoritative", "unverified": "driver death",
            "cleanup": "PermissionError"}[case] in events[0][1]["reason"]


@pytest.mark.parametrize("case", ["success", "no-confirmation", "unverified", "unknown", "bad-records", "busy"])
def test_operator_runtime_recovery_requires_confirmation_and_verified_record(tmp_path, monkeypatch, case):
    from contextlib import contextmanager
    from mimir_cultivate.worklink.factory_state import save_factory_record, load_factory_record
    record, db = _record(tmp_path, _dead_handle())
    record = replace(record, children_reaped=False)
    if case == "unverified":
        record = replace(record, handle=LaunchHandle("local_subprocess", "invalid", None))
    home = tmp_path / "home"
    save_factory_record(home, record)
    db.write_bytes(b"legacy store")
    attempt = Path(record.sandbox).parents[2]
    if case in {"unknown", "bad-records"}:
        monkeypatch.setattr(autonomy, "list_factory_records", lambda home: SimpleNamespace(
            records=[] if case == "unknown" else [record], failures=["broken"] if case == "bad-records" else []))
    if case == "busy":
        @contextmanager
        def busy(*args, **kwargs):
            yield False
        monkeypatch.setattr(autonomy, "factory_checkout_interlock", busy)
    events = []
    monkeypatch.setattr(autonomy, "log_event_sync", lambda name, **kw: events.append((name, kw)))
    if case == "success":
        autonomy.recover_factory_runtime(home, attempt, operator_confirmed_quiescent=True)
        assert not (attempt / ".factory-runtime").exists()
        assert autonomy._prepare_factory_runtime_prune(attempt, [record])
        assert load_factory_record(home, record.run_id) == record  # No invented reap acknowledgement.
        assert events[0][0] == "worklink_factory_runtime_operator_cleared"
    else:
        with pytest.raises(RuntimeError):
            autonomy.recover_factory_runtime(home, attempt, operator_confirmed_quiescent=case != "no-confirmation")
        assert db.read_bytes() == b"legacy store"
        assert not events


def test_operator_runtime_recovery_cli_requires_confirmation_and_dispatches(tmp_path, monkeypatch):
    import argparse
    from mimir_cultivate import commands
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_parser = commands.add_argparse(sub)
    argv = ["worklink", "clear-factory-runtime", str(tmp_path / "1983-1"), "--home", str(tmp_path)]
    with pytest.raises(SystemExit):
        parser.parse_args(argv)
    calls = []
    monkeypatch.setattr(autonomy, "recover_factory_runtime", lambda *a, **kw: calls.append((a, kw)))
    assert commands.dispatch(parser.parse_args(argv + ["--confirm-quiescent"]), worklink_parser) == 0
    assert calls == [((tmp_path, tmp_path / "1983-1"), {"operator_confirmed_quiescent": True})]


def test_base_verified_dead_ttl_regressions_execute_with_cultivate_selected(tmp_path):
    """Do not let a core-only runner silently skip the unchanged base TTL matrix."""
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ, MIMIR_PLUGINS="cultivate", PYTEST_ADDOPTS="")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-o", "addopts=",
         "tests/test_worklink_autonomy.py",
         "--basetemp", str(tmp_path / "base-ttl")],
        cwd=root, env=env, text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    executed = [line for line in result.stdout.splitlines()
                if line.startswith("PASSED tests/test_worklink_autonomy.py::"
                                   "test_prune_removes_old_attempt_with_verified_dead_unresumed_factory[")]
    assert len(executed) == 24, result.stdout


def test_executor_prune_uses_current_isolated_interpreter(tmp_path, monkeypatch):
    from mimir_cultivate.worklink import worker_client
    record, db = _record(tmp_path, _dead_handle())
    db.write_bytes(b"worker-owned")
    checkout = Path(record.sandbox).parent.parent
    calls = []
    monkeypatch.setattr(worker_client, "factory_checkout_for_path", lambda path: (checkout, 1983, 1))
    metrics = {"bytes_before": 20, "bytes_after": 10, "rows_deleted": 1}
    def control(path, argv, **kwargs):
        assert path == checkout
        assert argv == [sys.executable, "-I", str(Path(sys.modules[prune_factory_store.__module__].__file__).resolve()),
                        "--prune", str(db)]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(metrics).encode(), b"")
    monkeypatch.setattr(worker_client, "run_factory_control", control)
    assert prune_factory_store(record) == metrics
    assert len(calls) == 1


def test_ttl_cleanup_timeout_is_fail_closed(tmp_path, monkeypatch):
    from mimir_cultivate.worklink import factory_store
    record, db = _record(tmp_path, _dead_handle())
    db.write_bytes(b"retained")
    def timeout(attempt):
        raise subprocess.TimeoutExpired("worker", 1)
    monkeypatch.setattr(factory_store, "clear_prunable_runtime", timeout)
    assert not autonomy._prepare_factory_runtime_prune(Path(record.sandbox).parents[2], [record])
    assert db.read_bytes() == b"retained"


def test_legacy_store_layout_has_no_sibling_runtime(tmp_path):
    record, _ = _record(tmp_path, _dead_handle())
    sandbox = tmp_path / "legacy" / record.run_id
    record = replace(record, sandbox=str(sandbox), status=None)
    assert prune_factory_store(record) is None


def test_executor_runtime_cleanup_uses_current_interpreter_and_precedes_rmtree(tmp_path, monkeypatch):
    from mimir_cultivate.worklink import worker_client
    record, db = _record(tmp_path, _dead_handle())
    db.write_bytes(b"worker-owned")
    checkout = Path(record.sandbox).parent.parent
    attempt = checkout.parent
    calls = []
    monkeypatch.setattr(worker_client, "factory_checkout_for_path", lambda path: (checkout, 1983, 1))
    def control(path, argv, **kwargs):
        assert path == checkout and db.exists()
        assert argv == [sys.executable, "-I", str(Path(sys.modules[prune_factory_store.__module__].__file__).resolve()),
                        "--clear-runtime", str(attempt / ".factory-runtime")]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(worker_client, "run_factory_control", control)
    assert autonomy._prepare_factory_runtime_prune(attempt, [record])
    assert len(calls) == 1  # A no-op clear_prunable_runtime mutation must fail.


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs-human", "completed"])
@pytest.mark.parametrize("error", [RuntimeError("busy WAL"), sqlite3.OperationalError("disk full"), subprocess.TimeoutExpired("worker", 1)])
async def test_housekeeping_failure_preserves_run_outcome(tmp_path, monkeypatch, state, error):
    from mimir_cultivate.worklink import factory_store
    record, _ = _record(tmp_path, _dead_handle())
    record = replace(record, status=replace(record.status, status=state))
    def refuse(record):
        raise error
    monkeypatch.setattr(factory_store, "prune_factory_store", refuse)
    monkeypatch.setattr(orchestrator, "_record_factory_milestone", lambda *a, **kw: None)
    events = []
    monkeypatch.setattr(orchestrator, "_log_event", lambda name, **kw: events.append((name, kw)))
    async def verified(**kw):
        return tmp_path / "evidence", "https://github.com/owner/repo/pull/1"
    monkeypatch.setattr(orchestrator, "_verify_factory_completion", verified)
    transitions = []
    result = await orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)._finish_factory_070(
        issue=orchestrator.IssueContext(1983, "Factory", "run", set()),
        claim_record=SimpleNamespace(attempt=1, budget_attempt=None),
        claims=SimpleNamespace(transition_issue=lambda issue, **kw: transitions.append(kw["status"])),
        backend=object(), compute=object(), factory_record=record,
        test_cmd="pytest", runner=object(), started_at=datetime.now(UTC),
    )
    assert result.status == ("needs-human" if state == "needs-human" else "review_ready")
    assert transitions == (["blocked"] if state == "needs-human" else ["review"])
    assert any(name == "worklink_factory_opencode_prune_failed" and fields["reason"] for name, fields in events)


@pytest.mark.asyncio
async def test_cancellation_drains_store_thread_before_return(tmp_path, monkeypatch):
    from mimir_cultivate.worklink import factory_store
    record, _ = _record(tmp_path, _dead_handle())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def prune(record):
        entered.set()
        assert release.wait(5)
        finished.set()
    monkeypatch.setattr(factory_store, "prune_factory_store", prune)
    runner = orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)
    async def finish_under_production_locks(*args, **kwargs):
        return await runner._finish_factory_070(
            issue=orchestrator.IssueContext(1983, "Factory", "run", set()),
            claim_record=object(), claims=object(), backend=object(), compute=object(),
            factory_record=record, test_cmd="pytest", runner=object(), started_at=datetime.now(UTC),
        )
    monkeypatch.setattr(orchestrator.WorklinkRunner, "_run_factory_070_locked", finish_under_production_locks)
    task = asyncio.create_task(runner.run_epic(1983))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        from mimir_cultivate.worklink.factory_state import factory_issue_resource_lock, factory_checkout_interlock
        with factory_issue_resource_lock(tmp_path, 1983) as acquired:
            assert not acquired
        with factory_checkout_interlock(tmp_path, pruning=True) as acquired:
            assert not acquired
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    with factory_issue_resource_lock(tmp_path, 1983) as acquired:
        assert acquired
    with factory_checkout_interlock(tmp_path, pruning=True) as acquired:
        assert acquired


def test_opencode_task_id_resume_after_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real OpenCode CLI/schema and a local deterministic model endpoint."""
    binary = shutil.which("opencode")
    if binary is None:
        if os.environ.get("MIMIR_REQUIRE_OPENCODE_RESUME") == "1":
            pytest.fail("required OpenCode resume prerequisite is unavailable")
        pytest.skip("OpenCode executable is unavailable")
    requests: list[dict] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda name, **fields: events.append((name, fields)))
    tool_step = 0
    child_id = None

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            nonlocal tool_step
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            if request.get("tools") and tool_step < 2:
                step = tool_step
                tool_step += 1
                function = (
                    {"name": "task", "arguments": json.dumps({
                        "description": "Child continuity fixture", "subagent_type": "general",
                        "prompt": "Use bash to print the current directory",
                        **({"task_id": child_id} if child_id else {}),
                    })}
                    if step == 0 else
                    {"name": "bash", "arguments": json.dumps({"command": "pwd; printf 'child-resume-second-turn'" if child_id else "pwd", "description": "Print working directory"})}
                )
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                        {"index": 0, "id": f"call_fixture_{step}", "type": "function", "function": function},
                    ]}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                ]
            else:
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "session resumed with tool output"}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            record, db = _record(tmp_path, _dead_handle())
            checkout = Path(record.sandbox).parent.parent
            config = {
                "model": "fixture/fixture", "provider": {"fixture": {
                    "npm": "@ai-sdk/openai-compatible", "name": "Fixture",
                    "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "fixture"},
                    "models": {"fixture": {"name": "Fixture", "limit": {"context": 32000, "output": 2048}}},
                }},
            }
            env = dict(os.environ, XDG_DATA_HOME=str(db.parent.parent),
                       XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_CACHE_HOME=str(tmp_path / "cache"),
                       OPENCODE_CONFIG_CONTENT=json.dumps(config))

            def run(*args: str) -> subprocess.CompletedProcess[str]:
                result = subprocess.run([binary, *args, "--pure", "--dir", str(checkout)],
                                        env=env, capture_output=True, text=True, timeout=50)
                assert result.returncode == 0, (result.stdout, result.stderr)
                return result

            first = run("run", "Use bash to print the current directory", "--format", "json")
            task_ids = [json.loads(line)["sessionID"] for line in first.stdout.splitlines()
                        if line.startswith("{") and "sessionID" in json.loads(line)]
            assert task_ids and db.exists() and len(requests) >= 2
            with sqlite3.connect(db) as conn:
                children = conn.execute("SELECT id FROM session WHERE parent_id = ?", (task_ids[0],)).fetchall()
                assert len(children) == 1
                child_id = children[0][0]
                child_messages_before = conn.execute("SELECT count(*) FROM message WHERE session_id = ?", (child_id,)).fetchone()[0]
                assert conn.execute("SELECT count(*) FROM part WHERE json_extract(data, '$.type') = 'tool'").fetchone()[0] >= 2
                before = conn.execute("SELECT count(*) FROM event WHERE type IN ('message.part.updated', 'message.part.updated.1')").fetchone()[0]
                retained = conn.execute("SELECT count(*) FROM event WHERE type NOT IN ('message.part.updated', 'message.part.updated.1')").fetchone()[0]
                assert before > 0
            metrics = prune_factory_store(record)
            assert metrics is not None and metrics["rows_deleted"] == before
            assert events == [("worklink_factory_opencode_pruned", {
                "issue_id": 1983, "attempt": 1, "run_id": "chainlink-1983", **metrics,
            })]
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT count(*) FROM event WHERE type IN ('message.part.updated', 'message.part.updated.1')").fetchone() == (0,)
                assert conn.execute("SELECT count(*) FROM event").fetchone() == (retained,)
                assert conn.execute("SELECT count(*) FROM session WHERE id = ?", (task_ids[0],)).fetchone() == (1,)
            request_count = len(requests)
            tool_step = 0  # Call task again using the retained child task_id.
            run("run", "Recall the tool output", "--session", task_ids[0], "--format", "json")
            history = json.dumps([message for request in requests[request_count:]
                                  for message in request.get("messages", [])])
            assert "Use bash to print the current directory" in history
            assert str(checkout) in history and '"role": "tool"' in history
            assert child_id in history
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT id FROM session WHERE parent_id = ?", (task_ids[0],)).fetchall() == [(child_id,)]
                assert conn.execute("SELECT count(*) FROM message WHERE session_id = ?", (child_id,)).fetchone()[0] > child_messages_before
                child_tools = [json.loads(row[0]) for row in conn.execute(
                    "SELECT data FROM part WHERE session_id = ? AND json_extract(data, '$.type') = 'tool'", (child_id,))]
                assert any(part.get("state", {}).get("status") == "completed" and
                           "child-resume-second-turn" in part.get("state", {}).get("output", "")
                           for part in child_tools)
                assert any("child-resume-second-turn" in str(message.get("content", ""))
                           for request in requests[request_count:] for message in request.get("messages", [])
                           if message.get("role") == "tool")
        finally:
            server.shutdown()
            thread.join(timeout=5)
