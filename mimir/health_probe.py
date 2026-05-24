"""Bind-mount staleness health probe + self-restart.

Detects the VirtioFS stale-inode failure mode (host-side bind mount
target gets unlinked while the guest still holds it open) and triggers
container self-restart so Docker's ``restart: unless-stopped`` policy
re-establishes the bind cleanly.

See ``docs/internal/BIND_MOUNT_HEALTH_PROBE.md`` for the full spec
(detection mechanism, why kill PID 1, restart-loop guard, scheduling
cadence, edge cases).

Key design points worth remembering when reading this:

- The probe is a fresh ``subprocess.run(["pwd"], cwd=home)``. The
  Python parent's cwd is on a different bind mount (/workspace/mimir),
  so a parent-side ``os.path.exists(home)`` would lie. Subprocess
  inherits the current kernel resolution of ``home``, which is the
  stale inode when the bind is broken — pwd then exits 1 with
  "current working directory was deleted" on stderr.

- Recovery is ``os.kill(1, SIGTERM)`` from the mimir UID (1000) which
  owns PID 1 (``uv run mimir run``). Docker's restart policy brings
  the container back with a fresh bind mount.

- A sliding-window restart counter (default: 3 in 60 min) prevents
  thrashing when the underlying staleness persists across restarts.
  Past that threshold we emit ``bind_mount_stale_persistent`` and
  stop self-restarting until operator action.

- VirtioFS-only: the probe is a no-op when the host environment isn't
  running the failure-prone bind-mount layer (bare-metal Linux, OrbStack
  without virtiofs, CI runners). Detection: ``/proc/self/mountinfo``
  contains a ``virtiofs`` entry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .event_logger import log_event

log = logging.getLogger(__name__)


# ─── tunables ──────────────────────────────────────────────────────────

# Subprocess timeout for the pwd probe. ~5s is generous — pwd is
# microseconds when healthy and ENOENT on stale; only a kernel hang
# would push past 5s, and we want to bail fast in that case.
PROBE_SUBPROCESS_TIMEOUT_S = 5.0

# Default sliding-window guard: don't auto-restart more than 3 times
# in any rolling 60-minute window. Sized for "single-shot recovery in
# practice" (both observed VirtioFS incidents recovered on the first
# restart). 3-in-60-min means the third restart in an hour is the
# signal that something more is wrong than VirtioFS dentry staleness.
DEFAULT_MAX_RESTARTS_PER_HOUR = 3

# How long after container start to defer the first probe. Bind mount
# may not be fully ready in the first ~1s; 30s is a safety margin
# without delaying meaningful coverage. Read from ``/proc/uptime``
# rather than tracking it ourselves so it survives across module
# reloads in tests.
STARTUP_GRACE_S = 30.0

# Bookkeeping file path. Lives under MIMIR_HOME — and yes, that's the
# very directory whose health we're probing. The implementation must
# treat write failures here as "first restart" rather than refusing to
# restart, because a stuck bookkeeping write is the exact failure mode
# we're trying to recover from.
BOOKKEEPING_RELPATH = ".mimir/health-probe-restarts.jsonl"

# Cap the bookkeeping file at this many lines (rotate by trimming to
# the most recent half). We only ever read the last hour's worth so
# old entries are dead weight. 1000 picks a comfortable ceiling — at
# the 3-per-hour cap we'd take ~13 days to fill it.
BOOKKEEPING_MAX_LINES = 1000


# ─── result types ──────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    """Outcome of one probe + (possibly) restart pass.

    ``stale`` reflects the probe verdict; ``acted`` is True if the
    probe actually triggered a restart signal (i.e. stale AND not
    blocked by the guard AND not in startup grace). ``recovered`` is
    True if the previous probe was stale and this one passed — the
    one place we emit ``bind_mount_recovered`` from.
    """

    stale: bool
    acted: bool = False
    recovered: bool = False
    skipped_reason: str | None = None  # set when probe is a no-op
    detail: str = ""


# ─── module state (small) ──────────────────────────────────────────────


@dataclass
class _ProbeState:
    """Per-process tracking that survives across cron ticks within a
    single container run. Reset on container restart by virtue of
    being module-global."""

    last_probe_was_stale: bool = False
    # Memoize the virtiofs-detection result; mountinfo doesn't change
    # at runtime so we read it once per process.
    is_virtiofs_host: bool | None = None


_state = _ProbeState()


# ─── virtiofs detection ────────────────────────────────────────────────


def _read_mountinfo(path: Path = Path("/proc/self/mountinfo")) -> str | None:
    """Read the kernel's mountinfo for this process. Returns None when
    the file doesn't exist (non-Linux, container without /proc, etc.)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def is_virtiofs_environment(mountinfo_path: Path = Path("/proc/self/mountinfo")) -> bool:
    """True when the container is running under VirtioFS (Docker
    Desktop on macOS, some Lima setups). The probe is a no-op
    elsewhere — the failure mode being addressed is specific to the
    VirtioFS dentry-cache forwarding."""
    text = _read_mountinfo(mountinfo_path)
    if text is None:
        return False
    # Each mountinfo line contains the filesystem type as the field
    # after the separator " - ". Cheap substring check is sufficient;
    # we don't need to parse every field.
    return "virtiofs" in text


# ─── uptime + startup grace ────────────────────────────────────────────


def _read_uptime_s(path: Path = Path("/proc/uptime")) -> float | None:
    """System uptime in seconds (first field of /proc/uptime). We use
    *system* uptime rather than process uptime: in containers PID 1
    starts within milliseconds of the cgroup init, and /proc/uptime in
    a container reflects the container's runtime, not the host's. Both
    are correct enough for our 30s grace check."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def _within_startup_grace(now_uptime_s: float | None) -> bool:
    """Return True if we should skip this probe iteration because the
    container is too young for the bind mount to have fully stabilized.
    Conservative: if uptime is unreadable, assume we're past grace
    (don't block probing forever on a /proc oddity)."""
    if now_uptime_s is None:
        return False
    return now_uptime_s < STARTUP_GRACE_S


# ─── bookkeeping (sliding-window restart counter) ──────────────────────


def _bookkeeping_path(home: Path) -> Path:
    return home / BOOKKEEPING_RELPATH


def _read_recent_restart_timestamps(
    bookkeeping_path: Path,
    *,
    now: float | None = None,
    window_seconds: float = 3600.0,
) -> list[float]:
    """Return restart timestamps from the bookkeeping file that fall
    within the last ``window_seconds``. Robust to corrupt JSON and
    missing files — both treated as "no recent restarts" (the
    safe-default for restart-on-stale, since refusing to restart on
    bookkeeping failure would defeat the whole recovery path).

    PR #113 review fix (item 2 closure): merges entries from the
    primary path AND ``_FALLBACK_RESTART_PATH`` so the rolling-
    window guard sees restarts that landed on the fallback path
    when the primary write failed. Pre-fix the writes were merged
    (``_append_restart_timestamp`` writes to /tmp on primary
    failure) but the reads only checked the primary — so a
    persistently-broken home would still produce an unbounded
    restart loop, exactly the thrash the guard was designed to
    prevent. Dedup keys on ``timestamp_unix`` so the same restart
    isn't double-counted if both paths happened to capture it.
    """
    if now is None:
        now = time.time()
    cutoff = now - window_seconds
    seen_ts: set[float] = set()

    def _read_one(path: Path) -> None:
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "bookkeeping read failed at %s: %s; treating as empty",
                path, exc,
            )
            return
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(
                    "bookkeeping line is corrupt JSON in %s; skipping",
                    path,
                )
                continue
            ts = (
                entry.get("timestamp_unix")
                if isinstance(entry, dict) else None
            )
            if isinstance(ts, (int, float)) and ts >= cutoff:
                seen_ts.add(float(ts))

    _read_one(bookkeeping_path)
    _read_one(_FALLBACK_RESTART_PATH)
    return sorted(seen_ts)


_FALLBACK_RESTART_PATH = Path("/tmp/mimir-health-probe-restarts.jsonl")


def _append_restart_timestamp(
    bookkeeping_path: Path, *, now: float | None = None,
) -> bool:
    """Append a restart record. Returns True on success, False on any
    OSError. The caller should NOT block restart on a False return —
    a write failure here is itself a symptom of the bind-mount
    pathology we're trying to recover from.

    **CR2 (ops & observability) fix**: on primary-path failure, write
    the same record to ``/tmp/mimir-health-probe-restarts.jsonl`` as
    a fallback. The rolling-window guard in
    ``_read_recent_restart_timestamps`` reads only the primary path
    today (so persistent primary-write failure → unbounded restart
    loop, exactly the thrash the guard was designed to prevent).
    The fallback gives operator-visible breadcrumb trails AND, when
    we later teach the guard to read from both paths, defends the
    rate-limit invariant against the broken-home failure mode itself.
    """
    if now is None:
        now = time.time()
    record = {"timestamp_unix": int(now), "ts_iso": _iso(now)}
    try:
        bookkeeping_path.parent.mkdir(parents=True, exist_ok=True)
        with bookkeeping_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        _trim_bookkeeping_if_needed(bookkeeping_path)
        return True
    except OSError as exc:
        log.warning(
            "bookkeeping append failed: %s; restart will proceed anyway",
            exc,
        )
        # CR2 fallback: write a marker to /tmp so an operator looking
        # at a thrashing container can see the restart count even when
        # the bind-mounted home is broken. Best-effort — if /tmp is
        # also broken (rare), we just log and continue.
        try:
            with _FALLBACK_RESTART_PATH.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps({**record, "fallback_path": True}) + "\n"
                )
            log.warning(
                "wrote restart fallback marker to %s",
                _FALLBACK_RESTART_PATH,
            )
        except OSError as fb_exc:
            log.warning(
                "fallback restart marker write also failed: %s", fb_exc,
            )
        return False


def _trim_bookkeeping_if_needed(path: Path) -> None:
    """Rotate the bookkeeping file when it crosses BOOKKEEPING_MAX_LINES
    by keeping the most recent half. Best effort; logs and continues
    on any IO error."""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        if len(lines) <= BOOKKEEPING_MAX_LINES:
            return
        keep_count = BOOKKEEPING_MAX_LINES // 2
        kept = lines[-keep_count:]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("bookkeeping trim failed: %s", exc)


def _iso(unix_ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


# ─── probe + restart core ──────────────────────────────────────────────


def probe_pwd(
    home: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Run ``pwd`` in ``home`` via a fresh subprocess. Returns
    ``(stale, detail)``: ``stale`` is True when pwd's exit code is
    nonzero or "deleted" appears in stderr; ``detail`` is a short
    human-readable string for logging.

    The ``runner`` parameter exists so tests can swap in a fake without
    monkeypatching ``subprocess.run`` globally."""
    try:
        result = runner(
            ["pwd"],
            cwd=str(home),
            capture_output=True,
            text=True,
            timeout=PROBE_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return True, "pwd subprocess timed out"
    except FileNotFoundError as exc:
        # cwd doesn't exist at all (totally misconfigured home, or
        # bind mount was never attached). Treat as stale — same
        # operator-recovery path applies.
        return True, f"pwd subprocess FileNotFoundError: {exc}"
    except OSError as exc:
        # Any other OS-level error spawning the subprocess. Stale.
        return True, f"pwd subprocess OSError: {exc}"

    stderr = (result.stderr or "").lower()
    if result.returncode != 0 or "deleted" in stderr:
        return True, (
            f"pwd exit={result.returncode} "
            f"stderr={(result.stderr or '').strip()[:200]!r}"
        )
    return False, f"pwd ok: {(result.stdout or '').strip()}"


def _send_restart_signal() -> None:
    """Hard-restart the container by signaling PID 1. Wrapped in its
    own function so tests can monkeypatch a no-op without forking the
    test runner.

    SIGTERM not SIGKILL: PID 1 is mimir's own ``uv run mimir run``
    which has aiohttp cleanup handlers we want to run (drain the
    dispatcher, close saga, disconnect bridges). The cleanup window
    is short (~1s) and Docker waits 10s before SIGKILL anyway via the
    default stop-grace.

    The ESRCH catch handles the race where the container is already
    shutting down for an unrelated reason — we don't want a spurious
    OSError to bubble out of the cron job."""
    try:
        os.kill(1, signal.SIGTERM)
    except ProcessLookupError:
        # ESRCH: PID 1 already gone (shutdown in progress). No-op.
        log.warning("PID 1 already gone when restart was requested")
    except PermissionError as exc:
        # EPERM: we're not running as a UID that can signal PID 1.
        # Shouldn't happen in normal mimirbot deployment (we run as
        # the same UID that started PID 1), but if it does we want a
        # loud message rather than a silent failure.
        log.error(
            "EPERM signaling PID 1: %s. health probe cannot self-restart.",
            exc,
        )


def _fsync_events_log(events_log: Path) -> None:
    """Force the bind_mount_stale_detected event we just wrote to
    actually land on disk before we kill PID 1.

    log_event() writes through Python's buffered file open; the bytes
    sit in OS page cache after close(). On a graceful SIGTERM Docker
    eventually flushes that cache, but we don't want to depend on it
    — if something about the bind mount staleness is preventing the
    flush, our explanation of the restart goes missing and the
    operator sees an unexplained reboot.

    A short open + fsync is cheap (microseconds) and idempotent.

    **POSIX semantics + virtiofs caveat (CR2-#7 doc clarification).**
    POSIX ``fsync(2)`` flushes the **inode** referred to by the fd —
    NOT just "this fd's writes." So a readonly-fd fsync IS standards-
    compliant for "flush any pending writes to this file": the kernel
    has a single page cache per inode, and fsync drives all dirty
    pages for that inode to disk regardless of which fd opened it.
    Linux and macOS both follow this. The original review at
    code-review-2026-05-09.md flagged this as undefined / no-op; that
    framing was overcautious — see the Re-grades section in the
    review doc.

    Real residual concern: virtiofs / Docker-on-macOS has weaker
    fsync guarantees than direct-attached storage. ``fsync`` returns
    success once the data reaches the host's filesystem driver but
    not necessarily the disk hardware buffer. On macOS specifically,
    full durability requires ``fcntl(F_FULLFSYNC)``. Mimir's deploy
    target is Linux containers (where standard fsync is sufficient),
    and the failure mode this function defends against is
    bind-mount staleness — itself an OS / virtiofs concern. If
    durability becomes load-bearing under macOS dev, swap the fsync
    here for an F_FULLFSYNC equivalent."""
    try:
        fd = os.open(str(events_log), os.O_RDONLY)
    except OSError as exc:
        log.warning("events.jsonl fsync open failed: %s", exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.warning("events.jsonl fsync failed: %s", exc)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@dataclass
class HealthProbeConfig:
    """Narrow config slice — keeps tests free of the full Config.

    ``events_log`` is needed for the fsync-before-restart step (which
    pushes the algedonic event onto disk before we signal PID 1)."""

    home: Path
    events_log: Path
    max_restarts_per_hour: int = DEFAULT_MAX_RESTARTS_PER_HOUR
    # Test/operator override hook for the actual signal call. Never
    # invoked by ``probe_once`` directly except through this attribute,
    # so tests can substitute a no-op recorder.
    send_restart: Callable[[], None] = field(default=_send_restart_signal)


async def probe_once(cfg: HealthProbeConfig) -> ProbeResult:
    """One probe pass: detect, log, restart-if-warranted. Never raises;
    failures surface as events. Returns a ProbeResult for tests /
    introspection."""
    # Skip on non-VirtioFS hosts (memoized).
    if _state.is_virtiofs_host is None:
        _state.is_virtiofs_host = is_virtiofs_environment()
    if not _state.is_virtiofs_host:
        return ProbeResult(
            stale=False,
            skipped_reason="not_virtiofs",
            detail="not running on a VirtioFS-backed bind mount",
        )

    # Skip during startup grace window (bind not fully ready).
    uptime = _read_uptime_s()
    if _within_startup_grace(uptime):
        return ProbeResult(
            stale=False,
            skipped_reason="startup_grace",
            detail=f"uptime {uptime:.1f}s < {STARTUP_GRACE_S}s grace",
        )

    stale, detail = probe_pwd(cfg.home)

    # Healthy path: record recovery if we were stale last tick.
    if not stale:
        recovered = False
        if _state.last_probe_was_stale:
            await log_event(
                "bind_mount_recovered",
                home=str(cfg.home),
                detail=detail,
            )
            recovered = True
        _state.last_probe_was_stale = False
        return ProbeResult(stale=False, recovered=recovered, detail=detail)

    # Stale path. Decide whether to actually restart based on the
    # rolling-window guard.
    _state.last_probe_was_stale = True
    bookkeeping = _bookkeeping_path(cfg.home)
    recent = _read_recent_restart_timestamps(bookkeeping)
    restart_count = len(recent)

    if restart_count >= cfg.max_restarts_per_hour:
        # Guard tripped: stop thrashing and surface to the operator.
        await log_event(
            "bind_mount_stale_persistent",
            home=str(cfg.home),
            recent_restarts=restart_count,
            window_minutes=60,
            detail=detail,
        )
        return ProbeResult(
            stale=True,
            acted=False,
            detail=(
                f"{restart_count} prior restarts in last 60min — guard tripped, "
                f"not restarting. {detail}"
            ),
        )

    # Stale + within budget: log, fsync, restart.
    await log_event(
        "bind_mount_stale_detected",
        home=str(cfg.home),
        recent_restarts=restart_count,
        max_restarts_per_hour=cfg.max_restarts_per_hour,
        detail=detail,
    )

    # Also surface to stderr so the operator sees it via ``docker
    # compose logs`` even if the events.jsonl write didn't make it to
    # disk — the bind-mount staleness can swallow writes targeted at
    # the very dir we're probing.
    log.error(
        "bind-mount stale-inode detected (%s); restarting (count=%d/%d in 60min). %s",
        cfg.home, restart_count + 1, cfg.max_restarts_per_hour, detail,
    )
    print(
        f"mimir.health_probe: bind-mount stale at {cfg.home}; "
        f"triggering self-restart ({restart_count + 1}/"
        f"{cfg.max_restarts_per_hour} in 60min). {detail}",
        file=sys.stderr,
        flush=True,
    )

    # Best-effort fsync of events.jsonl so the algedonic line survives.
    _fsync_events_log(cfg.events_log)

    # Record this restart in bookkeeping BEFORE sending the signal.
    # If the bookkeeping write fails (the very condition we're
    # recovering from), we still proceed — the alternative is to never
    # restart, which is worse.
    _append_restart_timestamp(bookkeeping)

    # Trigger the actual restart. In tests this is monkeypatched to a
    # no-op that records the call.
    cfg.send_restart()

    return ProbeResult(
        stale=True,
        acted=True,
        detail=f"restart triggered (count={restart_count + 1}). {detail}",
    )


# ─── reset hook for tests ──────────────────────────────────────────────


def _reset_state_for_tests() -> None:
    """Reset module-global state. Used by the test suite between
    cases; not called from production code."""
    _state.last_probe_was_stale = False
    _state.is_virtiofs_host = None
