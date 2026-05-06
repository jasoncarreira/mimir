"""Tests for mimir/health_probe.py.

Covers the spec's test plan from BIND_MOUNT_HEALTH_PROBE.md:

- probe_pwd happy / stale / timeout / FileNotFoundError paths
- restart-loop guard (allows first, blocks at threshold, ages out)
- recovery event emission on stale → healthy transition
- VirtioFS gating (no-op on non-virtiofs hosts)
- startup-grace skip
- bookkeeping survives corrupt JSON
- SIGTERM is sent through the configured `send_restart` hook (never
  the real ``os.kill(1, ...)`` — tests would die)
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from mimir import health_probe as hp
from mimir.health_probe import (
    HealthProbeConfig,
    ProbeResult,
    _read_recent_restart_timestamps,
    _within_startup_grace,
    is_virtiofs_environment,
    probe_once,
    probe_pwd,
)


# ─── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset module-globals before and after each test so tests are
    order-independent."""
    hp._reset_state_for_tests()
    yield
    hp._reset_state_for_tests()


@pytest.fixture(autouse=True)
def _force_virtiofs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we're in a VirtioFS environment by default. Tests that
    care about non-VirtioFS gating override this explicitly."""
    monkeypatch.setattr(hp, "is_virtiofs_environment", lambda *_a, **_kw: True)
    hp._state.is_virtiofs_host = None  # invalidate memoization


@pytest.fixture(autouse=True)
def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace event_logger.log_event with a capturing async fn.

    log_event is imported into health_probe at module load — patch the
    module-local reference, not the source."""
    events: list[tuple[str, dict]] = []

    async def _cap(event_type: str, **payload: Any) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(hp, "log_event", _cap)
    return events


@pytest.fixture
def _no_op_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the events.jsonl fsync step — tests don't care about it
    and we don't want to chase a real path."""
    monkeypatch.setattr(hp, "_fsync_events_log", lambda _path: None)


@pytest.fixture
def _past_startup_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend uptime is well past the grace window."""
    monkeypatch.setattr(hp, "_read_uptime_s", lambda *_a, **_kw: 9999.0)


@pytest.fixture
def cfg(tmp_path: Path, _no_op_fsync, _past_startup_grace) -> HealthProbeConfig:
    """Probe config pointing at a fresh temp home. Restart hook is a
    no-op recorder so we can assert call count without forking."""
    home = tmp_path / "home"
    home.mkdir()
    events_log = tmp_path / "events.jsonl"
    events_log.write_text("", encoding="utf-8")

    calls: list[None] = []

    def _record_restart() -> None:
        calls.append(None)

    cfg = HealthProbeConfig(
        home=home,
        events_log=events_log,
        max_restarts_per_hour=3,
        send_restart=_record_restart,
    )
    cfg._restart_calls = calls  # type: ignore[attr-defined]
    return cfg


# ─── probe_pwd: subprocess result handling ────────────────────────────


def test_probe_pwd_passes_when_subprocess_returns_zero(tmp_path: Path) -> None:
    def _fake(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=str(tmp_path) + "\n", stderr="",
        )

    stale, detail = probe_pwd(tmp_path, runner=_fake)
    assert stale is False
    assert "ok" in detail.lower()


def test_probe_pwd_detects_deleted_in_stderr(tmp_path: Path) -> None:
    """The exact failure-mode signature from the VirtioFS incident:
    pwd exits 1 with a 'deleted' message in stderr."""
    def _fake(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr=(
                "error: The current working directory was deleted, "
                "so that command didn't work."
            ),
        )

    stale, detail = probe_pwd(tmp_path, runner=_fake)
    assert stale is True
    assert "deleted" in detail.lower() or "exit=1" in detail


def test_probe_pwd_treats_timeout_as_stale(tmp_path: Path) -> None:
    def _fake(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    stale, detail = probe_pwd(tmp_path, runner=_fake)
    assert stale is True
    assert "timed out" in detail.lower()


def test_probe_pwd_treats_filenotfound_as_stale(tmp_path: Path) -> None:
    """If the cwd doesn't exist at all, the subprocess raises
    FileNotFoundError before pwd even gets to run. Same recovery path."""
    def _fake(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    stale, detail = probe_pwd(tmp_path, runner=_fake)
    assert stale is True
    assert "filenotfound" in detail.lower()


def test_probe_pwd_treats_oserror_as_stale(tmp_path: Path) -> None:
    """Generic OSError on subprocess spawn. Treat as stale."""
    def _fake(*args, **kwargs):
        raise OSError(13, "Permission denied")

    stale, detail = probe_pwd(tmp_path, runner=_fake)
    assert stale is True


def test_probe_pwd_real_subprocess_against_real_cwd(tmp_path: Path) -> None:
    """End-to-end smoke test using the actual subprocess.run. Only
    runs when ``pwd`` is in PATH (it always is on POSIX). Confirms
    we wired the runner default correctly."""
    home = tmp_path / "real-home"
    home.mkdir()
    stale, detail = probe_pwd(home)
    assert stale is False
    # On Linux the path may contain /private/ on macOS — accept both
    # by just checking the basename appears somewhere.
    assert "real-home" in detail


# ─── restart-loop guard ────────────────────────────────────────────────


def test_read_recent_restart_timestamps_returns_empty_for_missing_file(
    tmp_path: Path,
) -> None:
    out = _read_recent_restart_timestamps(tmp_path / "nope.jsonl")
    assert out == []


def test_read_recent_restart_timestamps_filters_by_window(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bk.jsonl"
    now = time.time()
    lines = [
        {"timestamp_unix": int(now - 7200)},   # 2h ago — outside window
        {"timestamp_unix": int(now - 1800)},   # 30min ago — inside
        {"timestamp_unix": int(now - 600)},    # 10min ago — inside
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    out = _read_recent_restart_timestamps(path, now=now, window_seconds=3600)
    assert len(out) == 2


def test_read_recent_restart_timestamps_skips_corrupt_lines(
    tmp_path: Path,
) -> None:
    """Mixed valid/invalid lines should produce just the valid ones,
    not raise."""
    path = tmp_path / "bk.jsonl"
    now = time.time()
    path.write_text(
        json.dumps({"timestamp_unix": int(now - 100)}) + "\n"
        + "this is not json\n"
        + json.dumps({"timestamp_unix": int(now - 50)}) + "\n",
        encoding="utf-8",
    )
    out = _read_recent_restart_timestamps(path, now=now)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_restart_guard_allows_first_restart(
    cfg: HealthProbeConfig, _capture_events: list[tuple[str, dict]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bookkeeping file → first restart fires, event emitted."""
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))
    result = await probe_once(cfg)
    assert result.stale is True
    assert result.acted is True
    assert len(cfg._restart_calls) == 1  # type: ignore[attr-defined]
    types = [t for t, _ in _capture_events]
    assert "bind_mount_stale_detected" in types


@pytest.mark.asyncio
async def test_restart_guard_blocks_after_threshold(
    cfg: HealthProbeConfig, _capture_events: list[tuple[str, dict]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bookkeeping with N restarts in last 60min ⇒ no further restart;
    bind_mount_stale_persistent fires instead."""
    bk = cfg.home / hp.BOOKKEEPING_RELPATH
    bk.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    bk.write_text(
        "\n".join(
            json.dumps({"timestamp_unix": int(now - i * 60)})
            for i in (5, 10, 20)  # 3 restarts in last hour
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))

    result = await probe_once(cfg)
    assert result.stale is True
    assert result.acted is False
    assert len(cfg._restart_calls) == 0  # type: ignore[attr-defined]
    types = [t for t, _ in _capture_events]
    assert "bind_mount_stale_persistent" in types
    assert "bind_mount_stale_detected" not in types


@pytest.mark.asyncio
async def test_restart_guard_lets_old_timestamps_age_out(
    cfg: HealthProbeConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bookkeeping with 5 timestamps from >2h ago + 1 from 30min ago
    ⇒ count is 1, restart fires."""
    bk = cfg.home / hp.BOOKKEEPING_RELPATH
    bk.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    old = [int(now - 7200 - i * 600) for i in range(5)]  # 2h+ ago
    recent = [int(now - 1800)]                            # 30min ago
    lines = [json.dumps({"timestamp_unix": ts}) for ts in old + recent]
    bk.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))

    result = await probe_once(cfg)
    assert result.stale is True
    assert result.acted is True
    assert len(cfg._restart_calls) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_restart_proceeds_even_if_bookkeeping_write_fails(
    cfg: HealthProbeConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case from the spec: bookkeeping write failures should NOT
    block restart, because a stuck-write is itself a symptom of the
    bind-mount pathology we're trying to recover from."""
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))
    monkeypatch.setattr(hp, "_append_restart_timestamp", lambda *_a, **_kw: False)

    result = await probe_once(cfg)
    assert result.acted is True
    assert len(cfg._restart_calls) == 1  # type: ignore[attr-defined]


# ─── recovery event ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_event_after_restart(
    cfg: HealthProbeConfig, _capture_events: list[tuple[str, dict]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First probe stale, second probe healthy ⇒ bind_mount_recovered
    fires exactly once on the healthy probe."""
    # First call: stale.
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))
    await probe_once(cfg)

    # Second call: healthy.
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (False, "ok /home"))
    result = await probe_once(cfg)

    assert result.stale is False
    assert result.recovered is True
    types = [t for t, _ in _capture_events]
    assert types.count("bind_mount_recovered") == 1


@pytest.mark.asyncio
async def test_recovery_event_only_fires_after_prior_stale(
    cfg: HealthProbeConfig, _capture_events: list[tuple[str, dict]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Healthy → healthy should not emit a recovery event."""
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (False, "ok"))
    await probe_once(cfg)
    await probe_once(cfg)
    types = [t for t, _ in _capture_events]
    assert "bind_mount_recovered" not in types


# ─── VirtioFS gating ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_skipped_on_non_virtiofs(
    cfg: HealthProbeConfig, monkeypatch: pytest.MonkeyPatch,
    _capture_events: list[tuple[str, dict]],
) -> None:
    """When mountinfo doesn't show virtiofs, the probe should no-op
    even if pwd would have failed."""
    monkeypatch.setattr(hp, "is_virtiofs_environment", lambda *_a, **_kw: False)
    hp._state.is_virtiofs_host = None  # bust memoization
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))

    result = await probe_once(cfg)
    assert result.stale is False
    assert result.skipped_reason == "not_virtiofs"
    assert len(cfg._restart_calls) == 0  # type: ignore[attr-defined]
    types = [t for t, _ in _capture_events]
    assert "bind_mount_stale_detected" not in types


def test_is_virtiofs_environment_reads_mountinfo(tmp_path: Path) -> None:
    """Sanity-check the mountinfo parser. Real mountinfo lines look
    like '... - virtiofs mac rw' from the field after ' - '."""
    mounts = tmp_path / "mountinfo"
    mounts.write_text(
        "1079 1 0:33 /workspace /workspace rw,relatime - ext4 /dev/sda1 rw\n"
        "1088 1079 0:34 /Users/x/state/home /mimir-home rw,relatime - virtiofs mac rw\n",
        encoding="utf-8",
    )
    assert is_virtiofs_environment(mountinfo_path=mounts) is True


def test_is_virtiofs_environment_returns_false_on_pure_ext4(tmp_path: Path) -> None:
    mounts = tmp_path / "mountinfo"
    mounts.write_text(
        "1079 1 0:33 / / rw,relatime - ext4 /dev/sda1 rw\n",
        encoding="utf-8",
    )
    assert is_virtiofs_environment(mountinfo_path=mounts) is False


def test_is_virtiofs_environment_returns_false_when_missing(tmp_path: Path) -> None:
    assert is_virtiofs_environment(mountinfo_path=tmp_path / "missing") is False


# ─── startup grace ────────────────────────────────────────────────────


def test_within_startup_grace_when_uptime_low() -> None:
    assert _within_startup_grace(5.0) is True
    assert _within_startup_grace(29.9) is True


def test_within_startup_grace_after_grace() -> None:
    assert _within_startup_grace(31.0) is False
    assert _within_startup_grace(9999.0) is False


def test_within_startup_grace_handles_unreadable_uptime() -> None:
    """If uptime can't be read, don't block probing forever — assume
    we're past grace."""
    assert _within_startup_grace(None) is False


@pytest.mark.asyncio
async def test_probe_skipped_during_startup_grace(
    cfg: HealthProbeConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hp, "_read_uptime_s", lambda *_a, **_kw: 5.0)
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))

    result = await probe_once(cfg)
    assert result.skipped_reason == "startup_grace"
    assert len(cfg._restart_calls) == 0  # type: ignore[attr-defined]


# ─── algedonic event payloads ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_detected_event_carries_count_and_cap(
    cfg: HealthProbeConfig, _capture_events: list[tuple[str, dict]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer in feedback.py uses recent_restarts +
    max_restarts_per_hour to render 'count: N/M in last 60min' — make
    sure the event payload carries both."""
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))
    await probe_once(cfg)

    detected = [p for t, p in _capture_events if t == "bind_mount_stale_detected"]
    assert len(detected) == 1
    payload = detected[0]
    assert payload["recent_restarts"] == 0
    assert payload["max_restarts_per_hour"] == 3
    assert payload["home"] == str(cfg.home)


@pytest.mark.asyncio
async def test_persistent_event_carries_recent_count(
    cfg: HealthProbeConfig, _capture_events: list[tuple[str, dict]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bk = cfg.home / hp.BOOKKEEPING_RELPATH
    bk.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    bk.write_text(
        "\n".join(
            json.dumps({"timestamp_unix": int(now - i)}) for i in (60, 120, 180)
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hp, "probe_pwd", lambda *_a, **_kw: (True, "stale"))

    await probe_once(cfg)

    persistent = [p for t, p in _capture_events if t == "bind_mount_stale_persistent"]
    assert len(persistent) == 1
    assert persistent[0]["recent_restarts"] == 3


# ─── feedback.py renderer round-trip ──────────────────────────────────


def test_feedback_renderer_for_bind_mount_stale() -> None:
    """The renderer composes a count string from event fields. Verify
    the wiring matches what probe_once emits."""
    from mimir.feedback import _render_event_line

    line = _render_event_line(
        "bind_mount_stale",
        {
            "recent_restarts": 0,
            "max_restarts_per_hour": 3,
            "home": "/mimir-home",
        },
    )
    assert "stale-inode" in line.lower()
    assert "1/3" in line
    assert "/mimir-home" in line


def test_feedback_renderer_for_bind_mount_persistent() -> None:
    from mimir.feedback import _render_event_line

    line = _render_event_line(
        "bind_mount_persistent",
        {"recent_restarts": 3},
    )
    assert "persists" in line.lower()
    assert "3" in line
    assert "operator action" in line.lower()


def test_feedback_renderer_for_bind_mount_recovered() -> None:
    from mimir.feedback import _render_event_line

    line = _render_event_line("bind_mount_recovered", {})
    assert "healthy" in line.lower()
    assert "auto-restart" in line.lower()


# ─── send_restart hook is never the real os.kill in tests ─────────────


def test_default_send_restart_is_module_function() -> None:
    """Sanity: the default of HealthProbeConfig.send_restart is the
    module-level _send_restart_signal, not lambda or None. Tests must
    explicitly override (we do via the cfg fixture); production code
    calls the real one, which goes through ``os.kill(1, SIGTERM)``.
    Without this guard, a forgotten fixture override would let a
    runaway test SIGTERM PID 1 of the test runner."""
    from mimir.health_probe import HealthProbeConfig, _send_restart_signal

    default = HealthProbeConfig(home=Path("/tmp"), events_log=Path("/tmp/x"))
    assert default.send_restart is _send_restart_signal
