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
    )
    return record, db


def _dead_handle() -> LaunchHandle:
    process = subprocess.Popen(["true"])
    process.wait()
    return LaunchHandle("local_subprocess", str(process.pid), 1, process.pid)


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
    with pytest.raises(RuntimeError, match="outside a checkout attempt"):
        clear_prunable_runtime(valid_name)
    assert (valid_name / ".factory-runtime" / "keep").read_text() == "evidence"


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


def test_runtime_prune_refuses_unverified_matching_record(tmp_path: Path) -> None:
    attempt = tmp_path / "1983-1"
    (attempt / "checkout").mkdir(parents=True)
    (attempt / ".factory-runtime").mkdir()
    record, _ = _record(tmp_path / "elsewhere", LaunchHandle("local_subprocess", "invalid", None))
    assert not autonomy._prepare_factory_runtime_prune(attempt, [record])
    assert (attempt / ".factory-runtime").is_dir()


def test_factory_runtime_removed_with_prunable_checkout(tmp_path: Path) -> None:
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


def test_opencode_task_id_resume_after_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real OpenCode CLI/schema and a local deterministic model endpoint."""
    binary = shutil.which("opencode")
    if binary is None:
        pytest.skip("OpenCode executable is unavailable")
    requests: list[dict] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda name, **fields: events.append((name, fields)))
    tool_issued = False

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            nonlocal tool_issued
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            if request.get("tools") and not tool_issued:
                tool_issued = True
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                        {"index": 0, "id": "call_fixture", "type": "function", "function": {
                            "name": "bash", "arguments": json.dumps({"command": "pwd", "description": "Print working directory"}),
                        }},
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
                assert conn.execute("SELECT count(*) FROM part WHERE json_extract(data, '$.type') = 'tool'").fetchone()[0] >= 1
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
            run("run", "Recall the tool output", "--session", task_ids[0], "--format", "json")
            history = json.dumps([message for request in requests[request_count:]
                                  for message in request.get("messages", [])])
            assert "Use bash to print the current directory" in history
            assert str(checkout) in history and '"role": "tool"' in history
        finally:
            server.shutdown()
            thread.join(timeout=5)
