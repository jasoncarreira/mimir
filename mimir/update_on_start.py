"""Pending-update flag: operator-approved auto-install at next restart.

The propose/approve gate for ``memory/core/`` edits has a natural
analogue for the mimir package itself: the operator should approve
updates explicitly, but ``mimir update --apply`` from a shell session
is friction (containerized deployments require an exec / docker-cp /
ssh dance). This module adds a flag-file checkpoint instead:

1. The daily PyPI version-check cron fires
   ``mimir_update_available``; the algedonic block surfaces it.
2. The agent surfaces the update + the approval phrasing to the
   operator in chat.
3. The operator approves ("yes, do the update on next restart").
4. The agent calls the ``request_mimir_update`` tool, which writes
   ``<home>/.mimir/pending-update.flag`` with the target version.
5. The operator restarts the container.
6. ``apply_pending_update`` runs as the FIRST thing in ``server.main``
   — before asyncio setup, logging config, anything. If the flag is
   present:

   - Run ``python -m pip install --upgrade <pkg>[==target]`` in a
     subprocess.
   - On success: delete the flag, log ``mimir_update_applied``, and
     ``os.execv`` to re-exec on the new code. The supervisor doesn't
     see a restart (same PID), but Python re-imports everything.
   - On failure: delete the flag (so we don't loop on a broken
     install), log ``mimir_update_failed``, continue startup on the
     OLD version. The operator sees the failure in the next-turn
     algedonic block and can investigate.

Design choices
==============

**Why a flag file rather than an env var or a saga atom?** The check
has to happen before the agent's own infrastructure boots — saga
isn't loaded yet, event_logger isn't initialized, no asyncio loop.
Filesystem state is the most primitive surface we can rely on at
that point. Also: the operator can manually create / delete the flag
to override the agent's request (touch to approve, rm to cancel).

**Why ``os.execv`` rather than exit-and-let-supervisor-restart?** The
``execv`` replaces the process image in-place with the same PID, so
Docker / systemd / launchd don't perceive a restart. Without it, the
supervisor sees an exit, restarts, finds the flag is gone (we
deleted it post-install), runs normally. Both paths work, but
``execv`` is cleaner (one restart-event, not two) and avoids the
edge case where the supervisor has a restart-rate-limit that would
back off.

**Why delete the flag on failure too?** Loop avoidance. A flag that
sticks around through restarts would re-attempt the broken install
on every boot, leaving the operator with a perpetually-degraded
agent. Failing once and falling back to the old version is more
recoverable: the algedonic ``mimir_update_failed`` event surfaces
the diagnostic, the operator investigates, and re-approves once
they've identified the issue (network, dep conflict, broken
upstream release).

**The flag is the approval.** Per ``persona-spec-framework`` (the
tri-zone boundary model), "update mimir" is an escalate-first
action. The flag file's existence IS the operator-approved signal —
the agent should not write it without explicit operator authorization
in the same conversation. This matches the existing pattern for
``memory/core/`` edits: the agent CAN write to the file (autonomous
authority on the filesystem) but the action category is
escalate-first per ``06-action-boundaries.md``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Flag-file path under the agent home. ``.mimir/`` is the same
# subdirectory the saga DB + metrics live in — a per-deployment
# state surface that survives container restarts when the home is
# bind-mounted / volume-mounted.
_FLAG_DIRNAME = ".mimir"
_FLAG_BASENAME = "pending-update.flag"

# Startup-events sidecar. ``apply_pending_update`` runs BEFORE
# ``init_logger`` has been called (it's the very first action of
# ``server.main``), so events emitted during the install can't go
# through the normal ``mimir.event_logger.log_event`` path —
# ``get_logger`` would raise. Instead we write a JSONL sidecar at
# the well-known path below; ``consume_startup_events`` drains it
# through the now-initialized event logger from inside
# ``server._on_startup``. Result: ``mimir_update_starting`` /
# ``_applied`` / ``_failed`` events DO land in ``events.jsonl``
# and surface in the algedonic feedback block on the first turn
# after the restart, even though the install itself ran pre-init.
_STARTUP_EVENTS_BASENAME = "startup-events.jsonl"

# pip-install timeout. The install itself averages ~30s on faiss-heavy
# stacks; 5 minutes covers slow mirrors + cold-start. After that we
# give up rather than hang the entire restart indefinitely.
_PIP_TIMEOUT_S = 300


@dataclass(frozen=True)
class PendingUpdate:
    """Parsed contents of the pending-update flag file.

    ``target_version`` empty (or absent) means "latest stable per the
    daily check"; an explicit value pins the install to that version
    (e.g., the operator wants the specific release they reviewed).

    ``include_prereleases`` lets the operator approve an
    explicitly-pre-release version (e.g. ``0.2.0rc1``) — the install
    command passes ``--pre`` so pip considers them.

    ``approved_at`` is a diagnostic only; not used in the install
    decision.
    """

    target_version: str
    include_prereleases: bool
    approved_at: Optional[str]


def flag_path(home: Path) -> Path:
    """Return the absolute path where the pending-update flag lives
    for the given agent home. Operators / scripts can manually
    ``touch`` or ``rm`` this path to override the agent's request."""
    return home / _FLAG_DIRNAME / _FLAG_BASENAME


def write_flag(
    home: Path,
    *,
    target_version: str = "",
    include_prereleases: bool = False,
) -> Path:
    """Create (or overwrite) the pending-update flag. Called by the
    ``request_mimir_update`` tool when the operator has approved an
    update in chat. Returns the path written.

    Empty ``target_version`` means "use whatever pip resolves as
    latest at install time" — the operator approved an open-ended
    update. A non-empty value pins to that exact release.
    """
    path = flag_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_version": target_version,
        "include_prereleases": include_prereleases,
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _read_flag(path: Path) -> PendingUpdate:
    """Parse the flag file's JSON. Tolerates an empty file (treats it
    as ``{}`` — bare ``touch`` of the path is a valid approval) and
    malformed JSON (logs + treats as ``{}``)."""
    try:
        raw = path.read_text().strip()
    except OSError as exc:
        log.warning("pending-update flag read failed: %s — treating as empty", exc)
        raw = ""
    data: dict = {}
    if raw:
        try:
            data = json.loads(raw) or {}
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "pending-update flag JSON parse failed: %s — proceeding "
                "with empty defaults", exc,
            )
    return PendingUpdate(
        target_version=str(data.get("target_version") or "").strip(),
        include_prereleases=bool(data.get("include_prereleases", False)),
        approved_at=data.get("approved_at"),
    )


def _pypi_package_name() -> str:
    """Defaults to ``"mimir-agent"``; ``MIMIR_PYPI_PACKAGE_NAME`` env
    overrides for forks / pre-release channels. Same env var the
    daily version-check uses, so an operator who sets it once gets
    consistent behavior across both surfaces."""
    return os.environ.get("MIMIR_PYPI_PACKAGE_NAME", "mimir-agent").strip() or "mimir-agent"


def _install_spec(pkg: str, parsed: PendingUpdate) -> str:
    """Build the pip install spec. ``mimir-agent`` for "latest stable",
    ``mimir-agent==0.2.0rc1`` for a pinned release. The
    ``include_prereleases`` flag is passed to pip as ``--pre`` via the
    argv builder (not embedded in the spec string itself)."""
    if parsed.target_version:
        return f"{pkg}=={parsed.target_version}"
    return pkg


def _run_pip_install(
    spec: str, include_pre: bool, emit: Callable[..., None],
) -> int:
    """Run ``python -m pip install --upgrade <spec>`` synchronously.
    Returns the exit code. Catches FileNotFoundError (no python on
    PATH — shouldn't happen, but defensive) and timeout (pip hung
    on a slow mirror) and translates to non-zero rc + event log.

    ``emit`` is the combined sidecar+stdout emit callable produced
    by ``_make_emit`` — every event fires through both paths so the
    operator sees the result in container logs immediately AND in
    the algedonic block on the next turn after restart.
    """
    argv = [
        sys.executable, "-m", "pip", "install", "--upgrade",
    ]
    if include_pre:
        argv.append("--pre")
    argv.append(spec)
    emit("mimir_update_starting", spec=spec, include_pre=include_pre)
    try:
        completed = subprocess.run(
            argv,
            check=False,
            timeout=_PIP_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        emit(
            "mimir_update_failed",
            spec=spec,
            error=f"{type(exc).__name__}: {exc}",
        )
        return 127
    except subprocess.TimeoutExpired:
        emit(
            "mimir_update_failed",
            spec=spec,
            error=f"pip install exceeded {_PIP_TIMEOUT_S}s",
        )
        return 124
    if completed.returncode != 0:
        # Truncate stderr — full pip output can be megabytes on a
        # resolver conflict; the event log isn't the right place
        # for that. Operator pulls the full log if they need it.
        tail = (completed.stderr or completed.stdout or "")[-500:]
        emit(
            "mimir_update_failed",
            spec=spec,
            rc=completed.returncode,
            stderr_tail=tail,
        )
    return completed.returncode


def _default_log_event(event_kind: str, **fields) -> None:
    """Fallback in-process logger used when ``apply_pending_update``
    runs before ``init_logger`` has been called. Writes through to
    stdout in the same JSON-ish shape the event logger uses, so a
    startup-time ``mimir_update_applied`` is still grep-able from
    container logs even when the real logger isn't up yet.

    Note: this is the in-process diagnostic path. Persistence into
    ``events.jsonl`` (so the event surfaces in the algedonic
    feedback block on the next turn) is handled separately via the
    sidecar (see ``_record_startup_event`` + ``consume_startup_events``).
    Both paths fire on every emit.
    """
    parts = [f"{k}={v}" for k, v in fields.items() if v not in (None, "")]
    log.info("event=%s %s", event_kind, " ".join(parts))


def _record_startup_event(home: Path, event_kind: str, **fields) -> None:
    """Append a JSONL line to the startup-events sidecar so the event
    can be drained into ``events.jsonl`` after ``init_logger`` is up.

    Append-only: each install attempt may emit multiple events
    (``mimir_update_starting`` then ``_applied`` / ``_failed``).
    Best-effort: filesystem failure here doesn't abort the install
    — the stdout log path still reports the outcome, and the next
    restart's ``consume_startup_events`` will find whatever did
    make it onto disk.
    """
    sidecar = home / _FLAG_DIRNAME / _STARTUP_EVENTS_BASENAME
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "type": event_kind,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **fields,
        }
        with sidecar.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError as exc:
        log.warning("startup-events sidecar write failed: %s", exc)


def _make_emit(home: Path, log_event: Callable[..., None]) -> Callable[..., None]:
    """Combine the in-process log + the persistent sidecar into a
    single emit function. Used internally so callsites don't have to
    remember both paths. Tests can still inspect what was logged via
    the ``log_event`` parameter — both channels fire on every call."""
    def _emit(event_kind: str, **fields) -> None:
        log_event(event_kind, **fields)
        _record_startup_event(home, event_kind, **fields)
    return _emit


async def consume_startup_events(home: Path, async_log_event) -> int:
    """Drain the startup-events sidecar through the now-initialized
    event logger. Returns the number of events drained.

    Called from ``server._on_startup`` AFTER ``init_logger`` has set
    up the real ``mimir.event_logger.log_event``. Each line in the
    sidecar is replayed as a real event so it lands in ``events.jsonl``
    and surfaces in the algedonic feedback block on the next turn.

    The sidecar is deleted on success so subsequent restarts don't
    re-emit stale events. On parse error of an individual line, the
    line is skipped (corrupt line shouldn't block the rest). If the
    sidecar doesn't exist (the common case — no install attempted
    on this restart), returns 0 silently.
    """
    sidecar = home / _FLAG_DIRNAME / _STARTUP_EVENTS_BASENAME
    if not sidecar.is_file():
        return 0
    try:
        raw = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("startup-events sidecar read failed: %s", exc)
        return 0
    drained = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            log.warning("startup-events sidecar: skipping malformed line: %r", line[:120])
            continue
        kind = payload.pop("type", None)
        payload.pop("ts", None)  # the event logger stamps its own ts
        if not isinstance(kind, str) or not kind:
            continue
        try:
            await async_log_event(kind, **payload)
            drained += 1
        except Exception:  # noqa: BLE001 — drain is best-effort
            log.exception("startup-events drain failed for %s", kind)
    try:
        sidecar.unlink()
    except OSError as exc:
        log.warning("startup-events sidecar unlink failed: %s", exc)
    return drained


def apply_pending_update(
    home: Path,
    log_event: Callable[..., None] | None = None,
    *,
    _exec: Callable[..., None] | None = None,
) -> bool:
    """Pre-flight check: if a pending-update flag exists, install the
    requested version and re-exec the process. Called as the very
    first action of ``server.main()``.

    Returns ``True`` if a flag was processed (install attempted —
    success or failure), ``False`` if no flag was found and startup
    should proceed normally. The ``True`` path normally doesn't
    return (it ``execv``'s away), but on install failure we delete
    the flag and return so startup can proceed on the OLD version.

    ``_exec`` is an injection seam for tests — defaults to
    ``os.execv``. Test path passes a stub that records the call.
    """
    log_event = log_event or _default_log_event
    exec_fn = _exec or os.execv
    emit = _make_emit(home, log_event)

    path = flag_path(home)
    if not path.is_file():
        return False

    parsed = _read_flag(path)
    pkg = _pypi_package_name()
    spec = _install_spec(pkg, parsed)
    rc = _run_pip_install(spec, parsed.include_prereleases, emit)

    # Always delete the flag — success means we don't re-attempt;
    # failure means we don't loop on a broken install.
    try:
        path.unlink()
    except OSError as exc:
        log.warning("pending-update flag unlink failed: %s", exc)

    if rc != 0:
        # Continue startup on the old version. The
        # ``mimir_update_failed`` event was already emitted inside
        # ``_run_pip_install`` (and written to the sidecar so the
        # algedonic block surfaces it on next turn).
        return True

    emit("mimir_update_applied", spec=spec, approved_at=parsed.approved_at)
    # Re-exec to pick up the new code. Same PID — supervisor stays
    # quiet. The argv carries over verbatim so e.g. ``--home`` flags
    # passed in survive the re-exec.
    exec_fn(sys.executable, [sys.executable, *sys.argv])
    # Unreachable in production; only the test stub path returns here.
    return True
