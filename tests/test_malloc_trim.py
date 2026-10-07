"""glibc heap trim support and the mimir run scheduler wiring."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from mimir import malloc_trim
from mimir.scheduler import Scheduler


class FakeLibc:
    def __init__(self, *, error: OSError | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.error = error

    def malloc_trim(self, pad: int) -> int:
        self.calls.append((pad, threading.get_ident()))
        if self.error:
            raise self.error
        return 1


@pytest.fixture
def linux_glibc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(malloc_trim.sys, "platform", "linux")
    monkeypatch.delenv("LD_PRELOAD", raising=False)


def rss_values(*values: int):
    readings = iter(values)
    return lambda: next(readings)


def test_trim_calls_libc_once_and_reports_rss(linux_glibc) -> None:
    libc = FakeLibc()
    result = malloc_trim.trim_heap(libc=libc, rss_reader=rss_values(100_000_000, 70_000_000))
    assert result == malloc_trim.TrimResult(100_000_000, 70_000_000, 1)
    assert [pad for pad, _ in libc.calls] == [0]


def test_non_linux_skips_trim(linux_glibc, monkeypatch) -> None:
    monkeypatch.setattr(malloc_trim.sys, "platform", "darwin")
    libc = FakeLibc()
    assert malloc_trim.trim_heap(libc=libc, rss_reader=lambda: pytest.fail("RSS read")) is None
    assert libc.calls == []


def test_non_linux_probe_never_loads_libc(linux_glibc, monkeypatch) -> None:
    monkeypatch.setattr(malloc_trim.sys, "platform", "darwin")
    monkeypatch.setattr(
        malloc_trim.ctypes, "CDLL", lambda _name: pytest.fail("loaded libc on macOS"),
    )
    assert malloc_trim.trim_supported() is False


def test_missing_symbol_skips_trim(linux_glibc) -> None:
    assert malloc_trim.trim_heap(libc=object(), rss_reader=lambda: pytest.fail("RSS read")) is None


@pytest.mark.parametrize("preload", ["/usr/lib/libjemalloc.so", "/opt/LibJemalloc.so"])
def test_jemalloc_preload_skips_trim(linux_glibc, monkeypatch, preload: str) -> None:
    monkeypatch.setenv("LD_PRELOAD", preload)
    libc = FakeLibc()
    assert malloc_trim.trim_heap(libc=libc, rss_reader=lambda: pytest.fail("RSS read")) is None
    assert libc.calls == []


def test_tcmalloc_preload_skips_trim(linux_glibc, monkeypatch) -> None:
    monkeypatch.setenv("LD_PRELOAD", "/usr/lib/libtcmalloc.so")
    libc = FakeLibc()
    assert malloc_trim.trim_heap(libc=libc, rss_reader=lambda: pytest.fail("RSS read")) is None
    assert libc.calls == []


def test_trim_error_is_best_effort(linux_glibc) -> None:
    libc = FakeLibc(error=OSError("malloc_trim failed"))
    assert malloc_trim.trim_heap(libc=libc, rss_reader=lambda: 100) is None
    assert [pad for pad, _ in libc.calls] == [0]


@pytest.mark.parametrize("reading", [1, 2])
def test_rss_error_is_best_effort(linux_glibc, reading: int) -> None:
    libc = FakeLibc()
    calls = 0

    def broken_rss() -> int:
        nonlocal calls
        calls += 1
        if calls == reading:
            raise OSError("statm unreadable")
        return 100

    assert malloc_trim.trim_heap(libc=libc, rss_reader=broken_rss) is None
    assert len(libc.calls) == reading - 1


def test_symbol_lookup_failure_is_best_effort(linux_glibc, monkeypatch) -> None:
    def broken_cdll(_name):
        raise OSError("no libc")

    monkeypatch.setattr(malloc_trim.ctypes, "CDLL", broken_cdll)
    assert malloc_trim.trim_supported() is False
    assert malloc_trim.trim_heap() is None


async def _noop_enqueue(_event):
    return True


def make_scheduler(tmp_path: Path) -> Scheduler:
    return Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=_noop_enqueue)


def test_empty_cron_registers_nothing(tmp_path, monkeypatch) -> None:
    sched = make_scheduler(tmp_path)
    monkeypatch.setattr("mimir.scheduler.trim_supported", lambda: True)
    assert sched.add_malloc_trim_job("") is False
    assert sched._scheduler.get_job("malloc-trim") is None
    assert "malloc-trim" not in sched.registered_callables()


def test_unsupported_allocator_registers_nothing(tmp_path, linux_glibc, monkeypatch) -> None:
    monkeypatch.setenv("LD_PRELOAD", "libjemalloc.so")
    sched = make_scheduler(tmp_path)
    assert sched.add_malloc_trim_job("*/5 * * * *") is False
    assert sched._scheduler.get_job("malloc-trim") is None
    assert "malloc-trim" not in sched.registered_callables()


def test_default_cron_registers_single_coalesced_job(tmp_path, linux_glibc, monkeypatch) -> None:
    monkeypatch.setattr(malloc_trim.ctypes, "CDLL", lambda _name: FakeLibc())
    sched = make_scheduler(tmp_path)
    assert sched.add_malloc_trim_job("*/5 * * * *") is True
    job = sched._scheduler.get_job("malloc-trim")
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert "minute='*/5'" in str(job.trigger)


@pytest.mark.asyncio
@pytest.mark.parametrize("drop_mb,logged", [(16, True), (15, False)])
async def test_trim_runs_off_loop_and_logs_only_large_drops(
    tmp_path, linux_glibc, monkeypatch, drop_mb: int, logged: bool,
) -> None:
    import mimir.scheduler as scheduler_module

    libc = FakeLibc()
    before = 100 * 1024 * 1024
    monkeypatch.setattr(scheduler_module, "trim_supported", lambda: True)
    monkeypatch.setattr(
        scheduler_module, "trim_heap",
        lambda: malloc_trim.trim_heap(
            libc=libc, rss_reader=rss_values(before, before - drop_mb * 1024 * 1024),
        ),
    )
    events = []

    async def record(kind, **fields):
        events.append((kind, fields))

    monkeypatch.setattr(scheduler_module, "log_event", record)
    sched = make_scheduler(tmp_path)
    assert sched.add_malloc_trim_job("*/5 * * * *") is True
    loop_thread = threading.get_ident()
    await sched._callables["malloc-trim"].fn()

    assert len(libc.calls) == 1
    assert libc.calls[0][0] == 0
    assert libc.calls[0][1] != loop_thread
    if logged:
        assert events == [("malloc_trim", {
            "rss_before_mb": 100.0,
            "rss_after_mb": 100.0 - drop_mb,
            "released": 1,
        })]
    else:
        assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env_cron,expected_cron",
    [(None, "*/5 * * * *"), ("*/10 * * * *", "*/10 * * * *"), ("", "")],
    ids=["default-on", "env-override", "explicit-disable"],
)
async def test_server_passes_malloc_trim_cron(
    tmp_path, monkeypatch, env_cron: str | None, expected_cron: str,
) -> None:
    import mimir.server as server_module
    from tests.test_server import _controlled_server_app, _run_cleanup, _run_startup

    if env_cron is None:
        monkeypatch.delenv("MIMIR_MALLOC_TRIM_CRON", raising=False)
    else:
        monkeypatch.setenv("MIMIR_MALLOC_TRIM_CRON", env_cron)
    monkeypatch.setattr("mimir.scheduler.trim_supported", lambda: True)
    real_scheduler = make_scheduler(tmp_path)
    app, _control = _controlled_server_app(tmp_path, monkeypatch)
    recorded_crons = []

    def record_trim_job(self, *, cron_expr: str) -> bool:
        recorded_crons.append(cron_expr)
        return real_scheduler.add_malloc_trim_job(cron_expr)

    monkeypatch.setattr(
        server_module.Scheduler, "add_malloc_trim_job", record_trim_job, raising=False,
    )
    try:
        await _run_startup(app)
        assert recorded_crons == [expected_cron]
        job = real_scheduler._scheduler.get_job("malloc-trim")
        assert (job is not None) is bool(expected_cron)
        assert ("malloc-trim" in real_scheduler.registered_callables()) is bool(expected_cron)
    finally:
        await _run_cleanup(app)


@pytest.mark.asyncio
async def test_invalid_env_cron_logs_and_startup_continues(tmp_path, monkeypatch) -> None:
    from tests.test_server import (
        _ServerControl, _controlled_server_app, _run_cleanup, _run_startup,
    )

    monkeypatch.setenv("MIMIR_MALLOC_TRIM_CRON", "bad cron")
    control = _ServerControl()
    control.failures["scheduler:add_malloc_trim_job"] = ValueError("bad cron")
    app, control = _controlled_server_app(tmp_path, monkeypatch, control)
    await _run_startup(app)
    try:
        assert "scheduler:start" in control.events
        assert "scheduler:add_malloc_trim_job" in control.events
        assert ("scheduler_invalid_cron", {
            "error": "bad cron", "job": "malloc-trim",
        }) in control.event_payloads
    finally:
        await _run_cleanup(app)


def test_invalid_cron_is_rejected_by_real_scheduler(tmp_path, linux_glibc, monkeypatch) -> None:
    monkeypatch.setattr(malloc_trim.ctypes, "CDLL", lambda _name: FakeLibc())
    with pytest.raises(ValueError, match="invalid cron expression"):
        make_scheduler(tmp_path).add_malloc_trim_job("bad cron")


def test_host_cron_is_cleared(monkeypatch) -> None:
    from tests.conftest import _clear_host_mimir_environment

    monkeypatch.setenv("MIMIR_MALLOC_TRIM_CRON", "0 0 * * *")
    fixture = _clear_host_mimir_environment.__wrapped__()
    next(fixture)
    try:
        assert "MIMIR_MALLOC_TRIM_CRON" not in os.environ
    finally:
        with pytest.raises(StopIteration):
            next(fixture)


def test_configuration_documents_trim_cron() -> None:
    docs = (Path(__file__).resolve().parents[1] / "docs/configuration.md").read_text()
    assert "| `MIMIR_MALLOC_TRIM_CRON` |" in docs
