"""Liveness beat + out-of-process watchdog (chainlink #507)."""

from __future__ import annotations

import asyncio
import json
import signal
import time
from pathlib import Path

import pytest

from mimir.liveness import (
    _kill_agent,
    beat_age_seconds,
    detect_unclean_restart,
    liveness_beat_loop,
    liveness_path,
    mark_clean_shutdown,
    mark_session_running,
    notify_service_event,
    notify_unclean_restart,
    read_beat,
    read_session_marker,
    run_watchdog,
    watchdog_has_sink,
    write_beat,
)


@pytest.mark.asyncio
async def test_liveness_beat_loop_writes_off_loop(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    calls: list[tuple[object, tuple, dict]] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append((func, args, kwargs))
        result = func(*args, **kwargs)
        if len(calls) >= 2:
            raise asyncio.CancelledError
        return result

    async def fake_sleep(_interval: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await liveness_beat_loop(home, interval=60, started_at=1000.0)

    assert [call[0] for call in calls] == [write_beat, write_beat]
    assert all(call[1] == (home,) for call in calls)
    assert all(call[2]["started_at"] == 1000.0 for call in calls)
    assert all(call[2]["_on_error"] is not None for call in calls)
    beat = read_beat(home)
    assert beat is not None
    assert beat["started_at"] == 1000.0


async def _anoop(*_a, **_k):
    return None


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    (home / ".mimir").mkdir(parents=True)  # runtime path for the beat + session marker
    return home


# ── beat ────────────────────────────────────────────────────────────

def test_write_and_read_beat(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert write_beat(home, started_at=1000.0, ts=2000.0) is True
    beat = read_beat(home)
    assert isinstance(beat, dict)
    assert isinstance(beat["ts"], (int, float))
    assert beat["pid"] > 0
    assert beat["started_at"] == 1000.0
    assert beat_age_seconds(home, now=2003.0) == 3.0


@pytest.mark.asyncio
async def test_beat_loop_records_unwritable_marker_failure(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    from mimir.event_logger import EventLogger

    home = _home(tmp_path)
    marker_dir = liveness_path(home).parent
    marker_dir.chmod(0o500)
    real_write_text = Path.write_text

    def reject_marker_write(path: Path, *args, **kwargs):
        if path.parent == marker_dir:
            raise PermissionError(13, "Permission denied", str(path))
        return real_write_text(path, *args, **kwargs)

    async def stop_after_first(_interval: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "write_text", reject_marker_write)
    monkeypatch.setattr(asyncio, "sleep", stop_after_first)
    events_path = tmp_path / "logs" / "events.jsonl"
    event_logger = EventLogger(events_path, "test-session")
    try:
        with pytest.raises(asyncio.CancelledError):
            await liveness_beat_loop(
                home,
                interval=0,
                _event_logger=event_logger.log_durable_sync,
            )
    finally:
        marker_dir.chmod(0o700)

    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["type"] == "liveness_beat_write_failed"
    assert events[0]["path"] == str(liveness_path(home))
    assert "PermissionError" in events[0]["error"]
    assert "Permission denied" in events[0]["error"]
    assert "atomic json write" in caplog.text
    assert "Permission denied" in caplog.text


@pytest.mark.asyncio
async def test_beat_loop_emits_once_on_failure_and_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    import mimir.liveness as liveness

    home = _home(tmp_path)
    outcomes = iter([False, False, True, True])
    events: list[tuple[str, dict]] = []

    def fake_write_beat(*_args, **_kwargs) -> bool:
        return next(outcomes)

    sleeps = 0

    async def fake_sleep(_interval: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(liveness, "write_beat", fake_write_beat)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await liveness_beat_loop(
            home,
            interval=0,
            _event_logger=lambda event, **payload: events.append((event, payload)),
        )

    assert [event for event, _payload in events] == [
        "liveness_beat_write_failed",
        "liveness_beat_write_recovered",
    ]


@pytest.mark.asyncio
async def test_beat_loop_success_emits_no_events(tmp_path: Path, monkeypatch) -> None:
    import mimir.liveness as liveness

    home = _home(tmp_path)
    events: list[tuple[str, dict]] = []
    sleeps = 0

    async def fake_sleep(_interval: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(liveness, "write_beat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await liveness_beat_loop(
            home,
            interval=0,
            _event_logger=lambda event, **payload: events.append((event, payload)),
        )

    assert events == []


def test_beat_age_none_when_missing(tmp_path: Path) -> None:
    home = tmp_path / "agent"  # no state/ dir, no beat
    assert read_beat(home) is None
    assert beat_age_seconds(home) is None


def test_beat_age_none_on_garbage(tmp_path: Path) -> None:
    home = _home(tmp_path)
    liveness_path(home).write_text("not json", encoding="utf-8")
    assert beat_age_seconds(home) is None


def test_beat_age_clamps_future_to_zero(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home, ts=time.time() + 10_000)  # clock skew → future
    assert beat_age_seconds(home) == 0.0


# ── watchdog: --once ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_once_fresh_no_alert(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home)
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    down = await run_watchdog(home, once=True, stale_after=60, _post=fake_post)
    assert down is False
    assert calls == []


@pytest.mark.asyncio
async def test_once_stale_is_exit_code_only(tmp_path: Path) -> None:
    """--once is exit-code-only: down=True but it posts NOTHING (a fresh cron
    process can't dedupe, so paging is the cron monitor's job). PR #712 review."""
    home = _home(tmp_path)
    write_beat(home, ts=time.time() - 9999)
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    down = await run_watchdog(home, once=True, stale_after=60, _post=fake_post)
    assert down is True
    assert calls == []  # no post — the monitor pages off the exit code


@pytest.mark.asyncio
async def test_once_missing_beat_is_exit_code_only(tmp_path: Path) -> None:
    home = _home(tmp_path)  # no beat file
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    down = await run_watchdog(home, once=True, stale_after=60, _post=fake_post)
    assert down is True
    assert calls == []


@pytest.mark.asyncio
async def test_once_repeated_calls_never_post(tmp_path: Path) -> None:
    """The reproduction from the review: N consecutive --once calls against the
    same stale beat must produce ZERO posts (was N posts)."""
    home = _home(tmp_path)
    write_beat(home, ts=time.time() - 9999)
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    for _ in range(3):
        down = await run_watchdog(home, once=True, stale_after=60, _post=fake_post)
        assert down is True
    assert calls == []


# ── watchdog: loop transitions ─────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_alive_then_down_then_recovered(tmp_path: Path) -> None:
    """Sees the agent alive, then the beat goes stale (one down alert),
    then fresh again (one recovery notice) — no per-tick spam, and no
    cold-start false alarm because seen_alive gates the first down."""
    home = _home(tmp_path)
    write_beat(home)  # tick 1: fresh
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    # Drive the scenario from the sleep hook; raise to break the loop.
    steps = iter([
        lambda: write_beat(home, ts=time.time() - 9999),  # → stale before tick 2
        lambda: write_beat(home),                          # → fresh before tick 3
    ])

    async def fake_sleep(_secs):
        try:
            next(steps)()
        except StopIteration:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_watchdog(
            home, interval=0, stale_after=60, _post=fake_post, _sleep=fake_sleep,
        )

    downs = [c for c in calls if "🔴" in c["title"]]
    recovers = [c for c in calls if "✅" in c["title"]]
    assert len(downs) == 1, calls          # fired once, not per tick
    assert len(recovers) == 1, calls
    assert recovers[0]["dedupe_key"] == "agent-liveness-recovered"


@pytest.mark.asyncio
async def test_loop_no_alarm_before_first_beat(tmp_path: Path) -> None:
    """A watchdog started before the agent ever beats must NOT alarm
    (no alive→down transition yet) — only wait."""
    home = _home(tmp_path)  # never any beat
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    ticks = {"n": 0}

    async def fake_sleep(_secs):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_watchdog(
            home, interval=0, stale_after=60, _post=fake_post, _sleep=fake_sleep,
        )
    assert calls == []  # waiting for the first beat, never alarmed


# ── sink configuration ─────────────────────────────────────────────

def test_watchdog_has_sink(monkeypatch) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("MIMIR_WATCHDOG_WEBHOOK_URL", raising=False)
    assert watchdog_has_sink() is False
    monkeypatch.setenv("NTFY_TOPIC", "jcarreira_mimirbot")
    assert watchdog_has_sink() is True
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setenv("MIMIR_WATCHDOG_WEBHOOK_URL", "https://hooks.example/x")
    assert watchdog_has_sink() is True


# ── clean-shutdown marker / unclean-restart detection ──────────────

def test_session_marker_roundtrip(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert mark_session_running(home, started_at=1000.0)
    marker = read_session_marker(home)
    assert isinstance(marker, dict)
    assert marker["started_at"] == 1000.0
    assert marker["clean"] is False
    assert marker["pid"] > 0


def test_atomic_marker_write_fsyncs_file_before_replace_and_directory_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.liveness as liveness

    home = _home(tmp_path)
    calls: list[str] = []
    real_replace = liveness.os.replace
    real_fsync = liveness.os.fsync

    def track_fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def track_replace(src: Path, dst: Path) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(liveness.os, "fsync", track_fsync)
    monkeypatch.setattr(liveness.os, "replace", track_replace)

    assert mark_session_running(home, started_at=1000.0)

    assert calls == ["fsync", "replace", "fsync"]


def test_atomic_marker_write_reports_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fsync(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr("mimir.liveness.os.fsync", fail_fsync)

    assert not mark_session_running(_home(tmp_path), started_at=1000.0)


def test_first_boot_is_not_unclean(tmp_path: Path) -> None:
    """No prior marker (first boot ever) → no false unclean-restart alarm."""
    home = _home(tmp_path)
    assert detect_unclean_restart(home) is None


def test_clean_shutdown_then_boot_is_clean(tmp_path: Path) -> None:
    home = _home(tmp_path)
    mark_session_running(home, started_at=1000.0)
    assert mark_clean_shutdown(home)
    # Next boot inspects the prior marker — a graceful stop is not unclean.
    assert detect_unclean_restart(home) is None
    marker = read_session_marker(home)
    assert marker["clean"] is True
    assert "stopped_iso" in marker


def test_clean_shutdown_reports_marker_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    assert mark_session_running(home, started_at=1000.0)
    monkeypatch.setattr("mimir.liveness._atomic_write_json", lambda *args, **kwargs: False)

    assert mark_clean_shutdown(home) is False


def test_crash_without_cleanup_is_unclean(tmp_path: Path) -> None:
    """A run that wrote a clean=false marker and never flipped it (crash /
    OOM / SIGKILL) → next boot detects the unclean restart."""
    home = _home(tmp_path)
    mark_session_running(home, started_at=1000.0)
    # ... process killed; mark_clean_shutdown never ran ...
    prior = detect_unclean_restart(home)
    assert prior is not None
    assert prior["clean"] is False
    assert prior["started_at"] == 1000.0


def test_detect_then_remark_clears_for_next_cycle(tmp_path: Path) -> None:
    """Lifecycle: unclean prior → detect (truthy) → mark_session_running for
    the new session resets the marker so a *subsequent* clean stop is clean."""
    home = _home(tmp_path)
    mark_session_running(home, started_at=1000.0)  # session 1 (crashes)
    assert detect_unclean_restart(home) is not None  # session 2 boot sees it
    mark_session_running(home, started_at=2000.0)    # session 2 running
    mark_clean_shutdown(home)                          # session 2 clean stop
    assert detect_unclean_restart(home) is None        # session 3 boot is clean


def test_unclean_notify_ts_carries_forward(tmp_path: Path) -> None:
    """The session marker carries last_unclean_notify_ts across restarts so a
    crash-loop can coalesce notices (the storm-guard the server applies)."""
    from mimir.liveness import UNCLEAN_NOTIFY_WINDOW
    home = _home(tmp_path)
    # Boot 1 → crash. No notify ts yet, so a boot-2 detection would notify.
    mark_session_running(home, started_at=1000.0)
    prior = detect_unclean_restart(home)
    assert prior is not None and prior.get("last_unclean_notify_ts") is None
    # Boot 2 notifies (records ts=2000) and crashes again.
    mark_session_running(home, started_at=2000.0, last_unclean_notify_ts=2000.0)
    # Boot 3, 30s later: within window → suppress, carry the original ts.
    prior = detect_unclean_restart(home)
    last = prior.get("last_unclean_notify_ts")
    assert last == 2000.0 and (2030.0 - last) < UNCLEAN_NOTIFY_WINDOW
    mark_session_running(home, started_at=2030.0, last_unclean_notify_ts=last)
    # Boot 4, well past the window → would notify again.
    prior = detect_unclean_restart(home)
    assert prior.get("last_unclean_notify_ts") == 2000.0
    assert (2000.0 + UNCLEAN_NOTIFY_WINDOW + 10 - 2000.0) >= UNCLEAN_NOTIFY_WINDOW


def test_garbage_marker_is_not_unclean(tmp_path: Path) -> None:
    home = _home(tmp_path)
    from mimir.liveness import session_marker_path
    session_marker_path(home).write_text("not json", encoding="utf-8")
    # Unreadable marker reads as None → treated as first boot, not a crash.
    assert detect_unclean_restart(home) is None


@pytest.mark.asyncio
async def test_notify_unclean_restart_shape(tmp_path: Path) -> None:
    home = _home(tmp_path)
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    await notify_unclean_restart(
        home, prior={"pid": 42, "started_iso": "2026-06-16T00:00:00+00:00"},
        _post=fake_post,
    )
    assert len(calls) == 1
    assert "♻️" in calls[0]["title"]
    assert calls[0]["dedupe_key"] == "agent-unclean-restart"
    assert "42" in calls[0]["body"]


# ── --restart-on-stale: kill action ────────────────────────────────

@pytest.mark.asyncio
async def test_kill_agent_sigterm_then_sigkill(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home)  # records os.getpid() as the beat pid (> 1)
    sigs: list[int] = []

    def fake_kill(pid, sig):
        sigs.append(sig)  # never raises → probe reads 'alive' → escalates

    killed = await _kill_agent(
        home, grace=0, _kill=fake_kill, _sleep=_anoop, _check=lambda pid: True,
    )
    assert killed is True
    assert signal.SIGTERM in sigs and signal.SIGKILL in sigs


@pytest.mark.asyncio
async def test_kill_agent_no_beat_pid_is_noop(tmp_path: Path) -> None:
    home = _home(tmp_path)  # no beat → no pid to target
    sigs: list[int] = []
    res = await _kill_agent(
        home, grace=0, _kill=lambda p, s: sigs.append(s), _sleep=_anoop,
        _check=lambda pid: True,
    )
    assert res is False
    assert sigs == []


@pytest.mark.asyncio
async def test_kill_agent_skips_recycled_pid(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home)
    sigs: list[int] = []
    res = await _kill_agent(
        home, grace=0, _kill=lambda p, s: sigs.append(s), _sleep=_anoop,
        _check=lambda pid: False,  # /proc says it's not a mimir process
    )
    assert res is False
    assert sigs == []


@pytest.mark.asyncio
async def test_watchdog_restart_on_stale_invokes_kill(tmp_path: Path, monkeypatch) -> None:
    import mimir.liveness as liveness
    monkeypatch.setattr(liveness, "_proc_is_mimir", lambda pid: True)
    home = _home(tmp_path)
    write_beat(home, ts=time.time() - 9999)  # stale, but pid present
    sigs: list[int] = []
    down = await run_watchdog(
        home, once=True, stale_after=60, restart_on_stale=True,
        _post=_anoop, _sleep=_anoop, _kill=lambda pid, sig: sigs.append(sig),
    )
    assert down is True
    assert signal.SIGTERM in sigs


@pytest.mark.asyncio
async def test_watchdog_no_restart_when_fresh(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home)  # fresh
    sigs: list[int] = []
    await run_watchdog(
        home, once=True, stale_after=60, restart_on_stale=True,
        _post=_anoop, _sleep=_anoop, _kill=lambda pid, sig: sigs.append(sig),
    )
    assert sigs == []


@pytest.mark.asyncio
async def test_notify_service_event_shape() -> None:
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    await notify_service_event(unit="mimir.service", detail="exit 137", _post=fake_post)
    assert len(calls) == 1
    assert "🔴" in calls[0]["title"]
    assert calls[0]["category"] == "service-restart"
    assert "mimir.service" in calls[0]["body"]
    assert "exit 137" in calls[0]["body"]
