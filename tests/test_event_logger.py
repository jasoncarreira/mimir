"""events.jsonl writer (SPEC §10.1)."""

from __future__ import annotations

import asyncio
import fcntl
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from mimir.event_logger import EventLogger, safe_log_event, init_logger


def _append_from_process(
    path: Path,
    started: multiprocessing.synchronize.Event,
    finished: multiprocessing.synchronize.Event,
) -> None:
    logger = EventLogger(path, session_id="detached")
    started.set()
    logger.log_sync("detached_event", source="worklink")
    finished.set()


@pytest.mark.asyncio
async def test_log_appends_record_with_session_and_type(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")

    await logger.log("app_started", home="/h")
    await logger.log("tool_call", tool="echo", args={"text": "hi"}, turn_id="t1")

    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["type"] == "app_started"
    assert lines[0]["session_id"] == "proc-1"
    assert lines[0]["home"] == "/h"
    assert "timestamp" in lines[0]
    assert lines[1]["tool"] == "echo"


def test_durable_log_fsyncs_new_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = EventLogger(tmp_path / "events.jsonl", session_id="proc-1")
    fsynced: list[int] = []
    monkeypatch.setattr("mimir.event_logger.os.fsync", lambda fd: fsynced.append(fd))

    logger.log_durable_sync("startup_failed", phase="agent_runtime")

    expected = 2 if hasattr(os, "O_DIRECTORY") else 1
    assert len(fsynced) == expected


def test_durable_log_after_nondurable_creation_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")
    logger.log_sync("startup_progress", phase="agent_runtime")
    fsynced: list[int] = []
    monkeypatch.setattr("mimir.event_logger.os.fsync", lambda fd: fsynced.append(fd))

    logger.log_durable_sync("startup_failed", phase="scheduler_start")

    expected = 2 if hasattr(os, "O_DIRECTORY") else 1
    assert len(fsynced) == expected


def test_durable_log_propagates_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = EventLogger(tmp_path / "events.jsonl", session_id="proc-1")

    def fail_fsync(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr("mimir.event_logger.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        logger.log_durable_sync("startup_failed", phase="scheduler_start")


@pytest.mark.asyncio
async def test_concurrent_logs_do_not_interleave(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")

    await asyncio.gather(*(logger.log("tool_call", i=i) for i in range(50)))

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 50
    parsed = [json.loads(l) for l in lines]
    assert sorted(p["i"] for p in parsed) == list(range(50))


@pytest.mark.asyncio
async def test_max_events_trims(tmp_path: Path):
    """With hysteresis, trim fires when over cap by ≥10% (rounded up to
    at least 1 line). Between trims the file may sit between max and
    max+10%. The most-recent events are always kept."""
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1", max_events=3)

    for i in range(10):
        await logger.log("tool_call", i=i)

    parsed = [json.loads(l) for l in path.read_text().strip().splitlines()]
    # cap=3, hysteresis=max(3//10, 1)=1 → trigger at >4 lines, trim to 3.
    # The exact count at end depends on how many events landed since the
    # last trim cycle, but it's always ≤ trigger threshold and the most
    # recent events are preserved.
    assert len(parsed) <= 4
    # Recency invariant: whatever's left ends with the latest writes.
    last_i = parsed[-1]["i"]
    assert last_i == 9
    # And the kept range is contiguous (no gaps from out-of-order trim).
    indices = [p["i"] for p in parsed]
    assert indices == list(range(indices[0], indices[0] + len(indices)))


@pytest.mark.asyncio
async def test_async_log_offloads_append_io_to_worker_thread(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")
    loop_thread_id = None
    append_thread_id = None
    original_append = logger._append_record_sync

    async def capture_loop_thread():
        nonlocal loop_thread_id
        import threading

        loop_thread_id = threading.get_ident()

    def wrapped_append(record):
        nonlocal append_thread_id
        import threading

        append_thread_id = threading.get_ident()
        original_append(record)

    await capture_loop_thread()
    logger._append_record_sync = wrapped_append

    await logger.log("tool_call", i=1)

    assert append_thread_id is not None
    assert append_thread_id != loop_thread_id
    assert json.loads(path.read_text().strip())["i"] == 1


def test_log_sync_does_not_mkdir_after_initialization(tmp_path: Path, monkeypatch):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")

    def fail_mkdir(*args, **kwargs):
        raise AssertionError("mkdir should not run on the hot append path")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    logger.log_sync("startup", ok=True)

    assert json.loads(path.read_text().strip())["ok"] is True


@pytest.mark.asyncio
async def test_safe_log_event_writes_when_logger_is_initialized(tmp_path: Path):
    """safe_log_event delegates to log_event when the logger is initialized."""
    path = tmp_path / "events.jsonl"
    init_logger(path, session_id="proc-safe")

    await safe_log_event("test_event", key="value")

    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 1
    assert lines[0]["type"] == "test_event"
    assert lines[0]["key"] == "value"


@pytest.mark.asyncio
async def test_safe_log_event_swallows_errors_when_logger_not_initialized():
    """safe_log_event must not raise even if the global logger is not set up.

    This is the core contract: monitoring side-channels must never crash
    the primary work path regardless of logger state.
    """
    import mimir.event_logger as _el
    original = _el._logger
    try:
        _el._logger = None  # force the "not initialized" path
        # Should not raise — swallowed at DEBUG level
        await safe_log_event("orphan_event", x=1)
    finally:
        _el._logger = original  # restore so other tests aren't affected


@pytest.mark.asyncio
async def test_max_events_trim_eventually_lands_on_cap(tmp_path: Path):
    """Over a long enough run, the file does come back to cap after a
    trim cycle — verifies trim-back-to-max actually happens."""
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1", max_events=10)

    for i in range(100):
        await logger.log("tool_call", i=i)

    lines = path.read_text().strip().splitlines()
    # cap=10, hysteresis=max(10//10,1)=1 → trigger at >11 lines.
    # Bound is between 10 (right after trim) and 11 (right before).
    assert 10 <= len(lines) <= 11
    parsed = [json.loads(l) for l in lines]
    assert parsed[-1]["i"] == 99


@pytest.mark.asyncio
async def test_log_redacts_token_shaped_values_recursively(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")
    unprefixed_secret = "0123456789abcdef0123456789abcdef"

    await logger.log(
        "tool_error",
        error="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def failed",
        args={
            "env": "ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf12_3456-789xyz_long",
            "nested": [
                "token=github_pat_11ABCDEFG_xyz0123",
                f"> X-API-Key: {unprefixed_secret}",
                f"MIMIR_API_KEY: {unprefixed_secret}",
                f'{{"VOYAGE_API_KEY": "pa-{unprefixed_secret}"}}',
                "safe context",
            ],
        },
    )

    record = json.loads(path.read_text().strip())
    serialized = json.dumps(record)
    assert "eyJhbGciOiJIUzI1NiJ9" not in serialized
    assert "sk-ant-api03-" not in serialized
    assert "github_pat_" not in serialized
    assert unprefixed_secret not in serialized
    assert "pa-" + unprefixed_secret not in serialized
    assert record["error"] == "Authorization: Bearer [REDACTED] failed"
    assert record["args"]["nested"][0] == "token=[REDACTED]"
    assert record["args"]["nested"][1] == "> X-API-Key: [REDACTED]"
    assert record["args"]["nested"][2] == "MIMIR_API_KEY: [REDACTED]"
    assert record["args"]["nested"][3] == '{"VOYAGE_API_KEY": "[REDACTED]"}'
    assert record["args"]["nested"][4] == "safe context"


@pytest.mark.asyncio
async def test_part_a_log_payload_failures_never_reach_caller(tmp_path: Path):
    class RaisingRepr:
        def __str__(self) -> str:
            raise ValueError("cannot stringify")

        def __repr__(self) -> str:
            raise ValueError("cannot represent")

    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-bad-payload")
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    await logger.log("bad_repr", value=RaisingRepr())
    await logger.log("cyclic", value=cyclic)

    assert not path.exists()


@pytest.mark.asyncio
async def test_event_logger_redacts_yaml_block_scalars_without_erasing_context(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    secret = "s3cr3t" + "-event-block"
    content = (
        "ancestor:\n"
        "  items:\n"
        "    - password: &event_value !!str | # retained\n"
        f"        {secret}-implicit\n"
        "      sibling: keep-implicit-sibling\n"
        "    - ? password\n"
        "      : >-\n"
        f"        {secret}-explicit\n"
        "      sibling: keep-explicit-sibling\n"
        "    - keep-following-item\n"
        "  ancestor_sibling: keep-ancestor\n"
    )

    await EventLogger(path, session_id="proc-block").log("tool_result", content=content)

    persisted = json.loads(path.read_text())["content"]
    assert secret not in persisted
    for context in (
        "password: &event_value !!str | # retained",
        "? password",
        ": >-",
        "sibling: keep-implicit-sibling",
        "sibling: keep-explicit-sibling",
        "- keep-following-item",
        "ancestor:",
        "ancestor_sibling: keep-ancestor",
    ):
        assert context in persisted


def test_log_sync_redacts_token_shaped_values(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="proc-1")

    logger.log_sync("startup", stderr="OPENAI_API_KEY=sk-proj_AbCdEfGh1234567890_ijKlMnOpQrSt")

    record = json.loads(path.read_text().strip())
    assert "sk-proj_" not in record["stderr"]
    assert record["stderr"] == "OPENAI_API_KEY=[REDACTED]"


def test_log_sync_holds_io_lock(tmp_path):
    """chainlink #393: log_sync must acquire _io_lock so it can't write
    concurrently with _trim_sync's tail-read+rename (which would lose the
    record). Proof: while the test holds _io_lock, a log_sync on another thread
    blocks; once released it proceeds and the record lands."""
    import threading
    from mimir.event_logger import EventLogger

    logger = EventLogger(tmp_path / "events.jsonl", session_id="t")
    done = threading.Event()

    logger._io_lock.acquire()
    try:
        threading.Thread(
            target=lambda: (logger.log_sync("evt_x"), done.set()),
            daemon=True,
        ).start()
        # Blocked while we hold the lock (would NOT block pre-fix).
        assert not done.wait(timeout=0.4), "log_sync did not respect _io_lock"
    finally:
        logger._io_lock.release()

    assert done.wait(timeout=2.0), "log_sync did not proceed after lock release"
    assert '"type": "evt_x"' in (tmp_path / "events.jsonl").read_text()


def test_process_append_survives_trim_rename_window(tmp_path, monkeypatch):
    """A detached writer waits for trim's rename and lands on the new inode."""
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="server", max_events=4)
    for i in range(3):
        logger.log_sync("server_event", i=i)

    rename_reached = threading.Event()
    allow_rename = threading.Event()
    trim_finished = threading.Event()
    original_rename = Path.rename

    def paused_rename(source, target):
        rename_reached.set()
        assert allow_rename.wait(timeout=5.0), "test did not release trim rename"
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", paused_rename)

    def trim():
        logger._trim_sync()
        trim_finished.set()

    trim_thread = threading.Thread(target=trim, daemon=True)
    trim_thread.start()
    assert rename_reached.wait(timeout=2.0), "trim did not reach rename window"

    ctx = multiprocessing.get_context("spawn")
    append_started = ctx.Event()
    append_finished = ctx.Event()
    process = ctx.Process(
        target=_append_from_process,
        args=(path, append_started, append_finished),
    )
    process.start()
    try:
        assert append_started.wait(timeout=5.0), "detached writer did not start"
        assert not append_finished.wait(timeout=0.2), (
            "detached append was not serialized with trim"
        )
    finally:
        allow_rename.set()
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)

    trim_thread.join(timeout=2.0)
    assert trim_finished.is_set(), "trim did not finish"
    assert process.exitcode == 0
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(record["type"] == "detached_event" for record in records)
    assert len(records) <= logger._max_events


def test_process_lock_timeout_degrades_to_append(tmp_path, monkeypatch, caplog):
    """A stuck process lock cannot block an ordinary event indefinitely."""
    import mimir.event_logger as event_logger

    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="server")
    monkeypatch.setattr(event_logger, "PROCESS_LOCK_TIMEOUT_SECONDS", 0.05)
    clock = iter((100.0, 100.05))
    monkeypatch.setattr(
        event_logger.time, "monotonic", lambda: next(clock, 100.05),
    )
    logger._process_lock_path.touch()

    with logger._process_lock_path.open("a", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.log_sync("lock_degraded")

    assert json.loads(path.read_text())["type"] == "lock_degraded"
    assert "process lock timed out" in caplog.text
    assert "continuing with an unlocked append" in caplog.text


def test_durable_process_lock_timeout_fails_instead_of_claiming_success(
    tmp_path, monkeypatch, caplog,
):
    """A durable append must not race trim's rename by writing unlocked."""
    import mimir.event_logger as event_logger

    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, session_id="server")
    monkeypatch.setattr(event_logger, "PROCESS_LOCK_TIMEOUT_SECONDS", 0.05)
    logger._process_lock_path.touch()

    with logger._process_lock_path.open("a", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TimeoutError, match="durable append"):
            logger.log_durable_sync("startup_failed", phase="agent_runtime")

    assert not path.exists()
    assert "process lock timed out" in caplog.text
    assert "failing durable append" in caplog.text
