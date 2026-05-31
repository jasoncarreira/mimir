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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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

# Post-update digest sidecar. Written by ``apply_pending_update`` after a
# successful pip install (pre-execv), drained by ``consume_update_digest``
# on the next boot. Captures the diff between the prior deployment state
# and the newly installed version so the operator gets a one-line summary
# of what changed (new scheduler ticks, drifted skills, missing env vars).
_UPDATE_DIGEST_BASENAME = "post-update-digest.json"


# ---------------------------------------------------------------------------
# UpdateDigest — post-update diff surfaces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateDigest:
    """Snapshot of the deployment diff produced during ``apply_pending_update``.

    Written to a sidecar file pre-execv, consumed on the next boot by
    ``consume_update_digest`` which emits a ``mimir_update_digest`` event.

    Attributes
    ----------
    prior_version:
        ``mimir.__version__`` captured *before* pip ran.  The new version
        is read from the freshly-installed package at consume time.
    new_version:
        ``mimir.__version__`` after the install (populated at digest
        creation, after pip succeeds but before execv).
    scheduler_delta:
        Tick names present in the bundled ``scheduler_template.yaml``
        but absent from the live ``<home>/scheduler.yaml``.  These ticks
        shipped with the new version but won't activate until the
        operator (or the agent) adds them to the live scheduler.
    skills_drift:
        Names of optional skills whose installed copy differs from the
        bundled source.  Non-empty means the skill needs ``mimir skills
        update --apply`` to pick up source changes.
    env_gaps:
        ``(skill_name, env_key)`` pairs where a bundled skill declares
        a required env var that isn't set in the current environment.
        Surfaced so the operator knows what to wire up after the upgrade.
    """

    prior_version: str
    new_version: str
    scheduler_delta: list[str] = field(default_factory=list)
    skills_drift: list[str] = field(default_factory=list)
    env_gaps: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-serialisable representation. ``env_gaps`` uses lists
        (JSON has no tuple type) and is round-tripped back via
        ``UpdateDigest.from_dict``."""
        return {
            "prior_version": self.prior_version,
            "new_version": self.new_version,
            "scheduler_delta": list(self.scheduler_delta),
            "skills_drift": list(self.skills_drift),
            "env_gaps": [[s, k] for s, k in self.env_gaps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UpdateDigest":
        return cls(
            prior_version=str(data.get("prior_version") or ""),
            new_version=str(data.get("new_version") or ""),
            scheduler_delta=list(data.get("scheduler_delta") or []),
            skills_drift=list(data.get("skills_drift") or []),
            env_gaps=[(str(s), str(k)) for s, k in (data.get("env_gaps") or [])],
        )


def _current_version() -> str:
    """Return ``mimir.__version__``.  Falls back to
    ``importlib.metadata`` when the package is installed but the
    in-process import hasn't reloaded yet (rare during the re-exec
    cycle).  Returns ``"unknown"`` on any failure — the digest is
    still useful even without a precise version string."""
    try:
        from . import __version__
        return __version__
    except Exception:
        pass
    try:
        import importlib.metadata
        return importlib.metadata.version("mimir-agent")
    except Exception:
        return "unknown"


def _scheduler_delta(home: Path) -> list[str]:
    """Return tick names present in the bundled ``scheduler_template.yaml``
    but absent from the live ``<home>/scheduler.yaml``.

    Only surfaces *additions* (ticks new to the template) — operator-
    added or operator-removed entries in the live file are intentional
    customisations and should not be surfaced as drift.

    Returns an empty list if either file is missing or unreadable
    (fresh install / template not packaged — handled gracefully).
    """
    try:
        import yaml  # lazy — not always needed
        from .skill_defs import _BUNDLED_SCHEDULER  # type: ignore[attr-defined]
    except Exception:
        return []

    def _tick_names(path: Path) -> set[str]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            if isinstance(data, list):
                return {e["name"] for e in data if isinstance(e, dict) and "name" in e}
        except Exception:
            pass
        return set()

    template_names = _tick_names(_BUNDLED_SCHEDULER)
    live_names = _tick_names(home / "scheduler.yaml")
    return sorted(template_names - live_names)


def _env_gaps(home: Path) -> list[tuple[str, str]]:
    """Scan the installed package's bundled skills for ``env: required:``
    blocks and return ``(skill_name, env_key)`` pairs whose key is absent
    from the current environment.

    Reads directly from the installed package's ``mimir/skills/*/SKILL.md``
    (mirroring how ``_scheduler_delta`` reads the bundled
    ``scheduler_template.yaml``) rather than from the home-seeded
    ``<home>/.mimir_builtin_skills/``.  This matters because
    ``_compute_update_digest`` runs inside ``apply_pending_update`` — the
    *first* thing in ``server.main()`` — before ``os.execv`` is called.
    The home-seeded ``.mimir_builtin_skills/`` is only refreshed from the
    newly-installed package *after* execv, during the new code's boot.
    Reading from the package directly ensures a skill added in this
    update (with a new required env var) is surfaced on the very update
    that introduces it, not silently missed.

    Returns an empty list on any read / parse error.
    """
    try:
        from .skill_defs import _BUNDLED_ROOT
        from .skill_md import parse_env_block
    except Exception:
        return []

    gaps: list[tuple[str, str]] = []
    builtin_root = _BUNDLED_ROOT
    if not builtin_root.is_dir():
        return []

    for skill_dir in sorted(builtin_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
            required, _ = parse_env_block(text)
        except Exception:
            continue
        for spec in required:
            key = spec.get("name", "")
            if key and key not in os.environ:
                gaps.append((skill_dir.name, key))

    return gaps


def _compute_update_digest(home: Path, prior_version: str) -> UpdateDigest:
    """Compute the post-update diff digest.

    Called from ``apply_pending_update`` *after* a successful pip install
    but *before* ``os.execv``.  At this point the newly-installed code is
    on disk (pip replaced the package) but the running process image still
    uses the old imports — hence ``_current_version()`` reads the metadata
    from the *installed* package (importlib.metadata) rather than the
    in-process ``mimir.__version__`` (which still reflects the old version
    until after execv reloads everything).

    Pure function in terms of observable side-effects: reads files and
    the environment, creates no new state.

    Parameters
    ----------
    home:
        Agent home directory.
    prior_version:
        ``mimir.__version__`` captured *before* pip ran.
    """
    # After pip install, importlib.metadata reflects the new version;
    # the in-process mimir.__version__ still reflects the old one.
    # Force the importlib.metadata path so we get the new version number.
    try:
        import importlib.metadata
        new_version = importlib.metadata.version("mimir-agent")
    except Exception:
        new_version = _current_version()

    # detect_skill_drift only covers optional (home/skills/) installs;
    # returns [] when no optional skills are installed — safe to call always.
    skills_drift_names: list[str] = []
    try:
        from .skill_install import detect_skill_drift
        drift_results = detect_skill_drift(home)
        skills_drift_names = sorted(r.name for r in drift_results if not r.is_clean)
    except Exception as exc:
        log.warning("_compute_update_digest: skill drift check failed: %s", exc)

    return UpdateDigest(
        prior_version=prior_version,
        new_version=new_version,
        scheduler_delta=_scheduler_delta(home),
        skills_drift=skills_drift_names,
        env_gaps=_env_gaps(home),
    )


def _write_update_digest_sidecar(home: Path, digest: UpdateDigest) -> None:
    """Persist the post-update digest to ``<home>/.mimir/post-update-digest.json``
    so it survives the ``os.execv`` that follows. Consumed on the next boot by
    ``consume_update_digest``.

    Overwrites any prior sidecar (shouldn't exist at this point, but defensive
    against a mid-install crash that left a stale file).  Best-effort: filesystem
    failure here doesn't abort the install — the digest is informational.
    """
    sidecar = home / _FLAG_DIRNAME / _UPDATE_DIGEST_BASENAME
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(digest.to_dict(), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("post-update digest sidecar write failed: %s", exc)


async def consume_update_digest(
    home: Path,
    async_log_event: Callable[..., Awaitable[None]],
) -> int:
    """Drain the post-update digest sidecar, emitting a ``mimir_update_digest``
    event into ``events.jsonl``.  Returns 1 if drained, 0 if absent.

    Called from ``server._on_startup`` AFTER ``init_logger`` is up, alongside
    ``consume_startup_events``.  The sidecar is written by ``apply_pending_update``
    pre-execv; if no update ran this boot, the file won't exist (no-op).

    Idempotency: the sidecar is deleted after a successful emit.  A crash between
    emit and delete means the next restart re-emits one extra algedonic line
    (acceptable).  A crash before emit means the digest was never surfaced — also
    acceptable for informational content.
    """
    sidecar = home / _FLAG_DIRNAME / _UPDATE_DIGEST_BASENAME
    if not sidecar.is_file():
        return 0
    try:
        raw = sidecar.read_text(encoding="utf-8")
        data = json.loads(raw)
        digest = UpdateDigest.from_dict(data)
        await async_log_event("mimir_update_digest", **digest.to_dict())
        try:
            sidecar.unlink()
        except OSError as exc:
            log.warning("post-update digest sidecar unlink failed: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 — drain is best-effort
        log.exception("post-update digest drain failed")
        return 0


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


def _truncate_startup_events(home: Path) -> None:
    """Clear any stale startup-events sidecar before a new install
    attempt's events get appended.

    Defense against the rare case where ``consume_startup_events``
    drained successfully on a prior boot but its ``sidecar.unlink()``
    call failed (full disk, weird FS state, etc.). Without this
    truncate, the prior boot's stale events would get appended to by
    the current boot's writes — next drain would replay both old +
    new entries, producing duplicate ``mimir_update_applied`` events
    in ``events.jsonl``.

    Called from ``apply_pending_update`` ONLY when a pending-update
    flag is present (i.e., we're about to start writing). Boots
    without a flag don't touch the sidecar — preserves the
    consume-side guarantee that an existing sidecar represents
    work this boot actually did.

    Best-effort: if the truncate itself fails (rare), the append
    falls back to the prior-boot behavior (potential duplicates on
    drain) — still better than crashing the startup pre-flight.
    """
    sidecar = home / _FLAG_DIRNAME / _STARTUP_EVENTS_BASENAME
    try:
        sidecar.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("startup-events sidecar truncate failed: %s", exc)


def _make_emit(home: Path, log_event: Callable[..., None]) -> Callable[..., None]:
    """Combine the in-process log + the persistent sidecar into a
    single emit function. Used internally so callsites don't have to
    remember both paths. Tests can still inspect what was logged via
    the ``log_event`` parameter — both channels fire on every call."""
    def _emit(event_kind: str, **fields) -> None:
        log_event(event_kind, **fields)
        _record_startup_event(home, event_kind, **fields)
    return _emit


async def consume_startup_events(
    home: Path,
    async_log_event: Callable[..., Awaitable[None]],
) -> int:
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

    # We're about to write events to the sidecar; clear any stale
    # entries from a prior boot whose drain succeeded but unlink
    # failed. Keeps the next drain from replaying both old + new.
    _truncate_startup_events(home)

    parsed = _read_flag(path)
    pkg = _pypi_package_name()
    spec = _install_spec(pkg, parsed)
    prior_version = _current_version()
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

    # Compute + persist the deployment diff before execv so the next boot
    # can surface "what changed" to the operator via the algedonic block.
    try:
        digest = _compute_update_digest(home, prior_version)
        _write_update_digest_sidecar(home, digest)
    except Exception as exc:  # noqa: BLE001 — digest is informational
        log.warning("post-update digest computation failed: %s", exc)

    emit("mimir_update_applied", spec=spec, approved_at=parsed.approved_at)
    # Re-exec to pick up the new code. Same PID — supervisor stays
    # quiet. The argv carries over verbatim so e.g. ``--home`` flags
    # passed in survive the re-exec.
    exec_fn(sys.executable, [sys.executable, *sys.argv])
    # Unreachable in production; only the test stub path returns here.
    return True
