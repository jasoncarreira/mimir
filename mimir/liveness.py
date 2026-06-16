"""Liveness beat + out-of-process dead-man's-switch watchdog (chainlink #507).

The problem this solves: a *hard* failure — OOM-kill, SIGKILL, a wedged
event loop, the container dying — can't push its own alert, because
``mimir/ntfy.py`` and the operator-alert channel only fire from a *live*
process. Nothing notices the **absence** of the agent.

Two halves, deliberately split across the process boundary:

* **Beat (in-agent).** ``liveness_beat_loop`` runs as a normal background
  asyncio task and atomically rewrites ``<home>/state/liveness.json`` every
  ``MIMIR_LIVENESS_BEAT_SECONDS``. Because it's an event-loop task, the beat
  also stops if the loop *wedges* — not just on a clean exit — so a hung
  agent looks the same as a dead one to the watcher (which is what we want).

* **Watch (out-of-process).** ``mimir watchdog`` (a separate process —
  a compose sidecar or a host cron, NOT a thread inside the agent) reads the
  beat's age and pushes an ntfy alarm when it goes stale. Being out-of-process
  is the whole point: it survives the agent dying.

The beat file is the primary signal (no network/port needed — the watcher
just reads the bind-mounted home). ``GET /health`` is a secondary signal a
hosted uptime monitor can poll; see ``docs/watchdog.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

LIVENESS_FILENAME = "liveness.json"
SESSION_FILENAME = "session.json"

# Out-of-band sinks. The watchdog must reach the operator WITHOUT the (dead)
# agent, so both sinks are external services: ntfy.sh (``NTFY_TOPIC``) and an
# optional generic webhook (``MIMIR_WATCHDOG_WEBHOOK_URL``) that POSTs
# ``{"text": "<title>\n<body>"}`` — the shape a Slack incoming webhook,
# PagerDuty/Opsgenie intake, or any custom endpoint accepts. Either, both, or
# (with a startup warning) neither. Hermes routes alerts through ~18 platforms;
# this keeps mimir's watchdog from being ntfy-locked without a platform layer.
_WEBHOOK_ENV = "MIMIR_WATCHDOG_WEBHOOK_URL"
_WEBHOOK_TIMEOUT_S = 8.0

# Alarm identity — ntfy.post_algedonic_alarm dedups by these keys within its
# own window, so a sustained outage alerts at most once per window (no spam).
_DOWN_KEY = "agent-liveness-down"
_RECOVERED_KEY = "agent-liveness-recovered"
_RESTART_KEY = "agent-unclean-restart"
_SERVICE_KEY = "mimir-service-failure"
_CATEGORY = "agent-liveness"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    """Atomically write ``payload`` as JSON to ``path`` (tmp-file + rename, so
    a concurrent reader never sees a torn file). Soft-fail: returns ``False``
    on ``OSError`` and never raises — a state-file write must never disrupt the
    agent (or the watchdog). The temp name is pid-scoped so two writers don't
    clobber each other's tmp."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.debug("atomic json write to %s failed: %s", path, exc)
        return False


def liveness_path(home: Path) -> Path:
    return home / "state" / LIVENESS_FILENAME


def write_beat(
    home: Path,
    *,
    started_at: float | None = None,
    ts: float | None = None,
) -> None:
    """Atomically rewrite ``<home>/state/liveness.json`` with the current
    timestamp. Cheap (a few bytes); tmp-file + rename so the watcher never
    reads a torn file. ``ts`` is injectable for tests."""
    now = time.time() if ts is None else ts
    path = liveness_path(home)
    payload = {
        "ts": now,
        "iso": _iso(now),
        "pid": os.getpid(),
    }
    if started_at is not None:
        payload["started_at"] = started_at
        payload["uptime_s"] = round(now - started_at, 1)
    _atomic_write_json(path, payload)


def read_beat(home: Path) -> dict[str, Any] | None:
    """Return the parsed beat, or ``None`` if missing/unreadable/garbage."""
    path = liveness_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def beat_age_seconds(home: Path, *, now: float | None = None) -> float | None:
    """Seconds since the last beat, or ``None`` if there is no readable beat.
    A future-dated or non-numeric ``ts`` is treated as unreadable (``None``)."""
    beat = read_beat(home)
    if not beat:
        return None
    ts = beat.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    age = (time.time() if now is None else now) - float(ts)
    return age if age >= 0 else 0.0


async def liveness_beat_loop(
    home: Path,
    *,
    interval: float,
    started_at: float | None = None,
) -> None:
    """Background task: write a beat every ``interval`` seconds, forever.
    Stops (and the beat goes stale) if the event loop dies or wedges — which
    is exactly the signal the watchdog keys on."""
    started_at = time.time() if started_at is None else started_at
    write_beat(home, started_at=started_at)  # first beat immediately
    while True:
        await asyncio.sleep(interval)
        write_beat(home, started_at=started_at)


async def _post_webhook(title: str, body: str) -> None:
    """POST ``{"text": title + body}`` to ``MIMIR_WATCHDOG_WEBHOOK_URL`` if
    set. Slack-incoming-webhook compatible; soft-fail, never raises."""
    url = os.environ.get(_WEBHOOK_ENV, "").strip()
    if not url:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=_WEBHOOK_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"text": f"{title}\n{body}"}) as resp:
                if resp.status >= 400:
                    log.warning("watchdog webhook POST returned %s", resp.status)
    except Exception as exc:  # noqa: BLE001 — never crash the watchdog
        log.warning("watchdog webhook POST failed: %s", exc)


async def _post_ntfy(
    title: str, body: str, priority: int, tags: list[str] | None,
) -> None:
    """Direct ntfy.sh push to ``NTFY_TOPIC`` if set. Self-contained — does
    NOT go through mimir.ntfy/event_logger, so the watchdog has zero
    dependency on the (possibly-broken) agent home or its logging. The
    rich/emoji text rides in the body (UTF-8); the Title header stays
    header-safe ASCII and ``tags`` carry the emoji ntfy renders. Soft-fail."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    headers = {"Title": "mimir watchdog", "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        timeout = aiohttp.ClientTimeout(total=_WEBHOOK_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://ntfy.sh/{topic}", data=f"{title}\n{body}", headers=headers,
            ) as resp:
                if resp.status >= 400:
                    log.warning("watchdog ntfy POST returned %s", resp.status)
    except Exception as exc:  # noqa: BLE001 — never crash the watchdog
        log.warning("watchdog ntfy POST failed: %s", exc)


async def _default_alert(
    *, category: str, title: str, body: str, dedupe_key: str,
    priority: int = 4, tags: list[str] | None = None,
) -> None:
    """Fan the alert out to every configured out-of-band sink (ntfy +
    optional webhook). No-ops cleanly when a sink is unset. ``category`` /
    ``dedupe_key`` are accepted for call-site symmetry; the watchdog's own
    ``alerted`` flag handles de-duplication, so they aren't re-used here."""
    await _post_ntfy(title, body, priority, tags)
    await _post_webhook(title, body)


def watchdog_has_sink() -> bool:
    """True if at least one out-of-band sink is configured."""
    return bool(
        os.environ.get("NTFY_TOPIC", "").strip()
        or os.environ.get(_WEBHOOK_ENV, "").strip()
    )


# ───────────────────────────────────────────────────────────────────────────
# Clean-shutdown marker (in-process restart-notify)
#
# The watchdog above catches the agent dying *while nobody's home* — but it
# needs a separate process. This second, complementary mechanism needs no
# sidecar: the agent itself reports, on its *next boot*, that the previous run
# died uncleanly. ``mark_session_running`` writes a ``clean: false`` marker at
# startup; the graceful-shutdown path (``_on_cleanup``, which only runs on a
# SIGTERM/SIGINT-initiated stop) flips it to ``clean: true``. If a boot finds
# the prior marker still ``clean: false``, the last run was killed/crashed/OOM'd
# (or wedged then killed) without cleanup → surface it. Catches everything that
# *comes back* (Docker ``restart:`` brings it up, and it tells you); a host that
# stays down is the watchdog's / an external monitor's job, not this.
# ───────────────────────────────────────────────────────────────────────────


def session_marker_path(home: Path) -> Path:
    return home / "state" / SESSION_FILENAME


def read_session_marker(home: Path) -> dict[str, Any] | None:
    """Return the parsed session marker, or ``None`` if missing/garbage."""
    try:
        data = json.loads(session_marker_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def mark_session_running(
    home: Path, *, started_at: float, ts: float | None = None,
) -> None:
    """Record that a session is live and has NOT shut down cleanly yet."""
    now = time.time() if ts is None else ts
    _atomic_write_json(session_marker_path(home), {
        "started_at": started_at,
        "started_iso": _iso(started_at),
        "pid": os.getpid(),
        "clean": False,
        "updated_iso": _iso(now),
    })


def mark_clean_shutdown(home: Path, *, ts: float | None = None) -> None:
    """Flip the current session marker to ``clean: true``. Called from the
    graceful-shutdown path — i.e. an *intended* stop. A hard kill (OOM /
    SIGKILL) never reaches here, so the marker stays ``clean: false`` and the
    next boot reports an unclean restart."""
    now = time.time() if ts is None else ts
    marker = read_session_marker(home) or {}
    marker["clean"] = True
    marker["stopped_iso"] = _iso(now)
    _atomic_write_json(session_marker_path(home), marker)


def detect_unclean_restart(home: Path) -> dict[str, Any] | None:
    """Inspect the prior session marker. Returns the prior marker when the
    last run did NOT shut down cleanly; ``None`` on a clean prior stop OR a
    first-ever boot (no marker). Call this BEFORE ``mark_session_running``
    overwrites the marker for the new session."""
    prior = read_session_marker(home)
    if prior is None:
        return None  # first boot — nothing to compare against
    if prior.get("clean") is True:
        return None  # previous run stopped gracefully
    return prior


async def notify_unclean_restart(
    home: Path, *, prior: dict[str, Any],
    _post: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Push an out-of-band notice that the agent came back after an unclean
    shutdown. Same sinks as the watchdog (ntfy + webhook), so the in-process
    restart-notify and the out-of-process dead-man's-switch land on one
    channel. Soft-fail; no sink → no-op."""
    post = _post or _default_alert
    started = prior.get("started_iso") or "unknown"
    pid = prior.get("pid", "?")
    await post(
        category=_CATEGORY,
        title="♻️ mimir restarted after an unclean shutdown",
        body=(
            f"The previous run (pid {pid}, started {started}) did not shut down "
            f"cleanly — likely a crash, OOM-kill, hard restart, or a wedge that "
            f"was killed. The agent is back up now. home={home}"
        ),
        dedupe_key=_RESTART_KEY,
        priority=4,
        tags=["recycle", "warning"],
    )


async def notify_service_event(
    *, unit: str | None = None, detail: str | None = None,
    _post: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Push an out-of-band 'service failed/restarting' alert. Meant for a
    systemd ``OnFailure=`` hook (``mimir notify-restart``), so it is
    self-contained — it reaches ntfy / webhook directly, with no dependency on
    a live agent process or its event logger. Soft-fail."""
    post = _post or _default_alert
    unit_str = unit or "mimir"
    body = f"systemd reported a failure for unit '{unit_str}'."
    if detail:
        body += f" {detail}"
    body += f" The service manager is handling restart; check `journalctl -u {unit_str}`."
    await post(
        category="service-restart",
        title="🔴 mimir service failed (systemd OnFailure)",
        body=body,
        dedupe_key=_SERVICE_KEY,
        priority=5,
        tags=["rotating_light"],
    )


async def run_watchdog(
    home: Path,
    *,
    interval: float = 60.0,
    stale_after: float = 180.0,
    once: bool = False,
    _post: Callable[..., Awaitable[None]] | None = None,
    _sleep: Callable[[float], Awaitable[None]] | None = None,
) -> bool:
    """Out-of-process dead-man's-switch loop.

    Each tick reads the beat age and decides ``down`` (no readable beat, or
    age > ``stale_after``). In loop mode it only fires the first ``down``
    alarm *after* it has seen the agent alive at least once — so a watchdog
    started before the agent (or a cold home with no beat yet) doesn't
    false-alarm; it detects the alive→absent transition. On recovery it
    pushes a back-up notice. ``--once`` (cron mode) skips the transition
    gate and simply reports/alerts on the current state.

    Returns the last ``down`` value (useful for ``--once``). ``_post`` /
    ``_sleep`` are injection seams for tests.
    """
    post = _post or _default_alert
    sleep = _sleep or asyncio.sleep
    if _post is None and not watchdog_has_sink():
        log.warning(
            "mimir watchdog has no out-of-band sink configured — set NTFY_TOPIC "
            "and/or %s, or it can't alert anyone. Watching anyway.", _WEBHOOK_ENV,
        )
    seen_alive = False
    alerted = False
    down = False

    while True:
        age = beat_age_seconds(home)
        down = (age is None) or (age > stale_after)

        if not down:
            seen_alive = True
            if alerted:
                await post(
                    category=_CATEGORY,
                    title="✅ mimir liveness recovered",
                    body=f"Liveness beat is fresh again (age {age:.0f}s). home={home}",
                    dedupe_key=_RECOVERED_KEY,
                    priority=3,
                    tags=["white_check_mark"],
                )
                alerted = False
        elif (seen_alive or once) and not alerted:
            age_str = "no beat file" if age is None else f"{age:.0f}s stale"
            await post(
                category=_CATEGORY,
                title="🔴 mimir liveness beat stale — agent may be down",
                body=(
                    f"No fresh liveness beat ({age_str}; threshold {stale_after:.0f}s). "
                    f"The agent process may be dead, OOM-killed, or wedged. home={home}"
                ),
                dedupe_key=_DOWN_KEY,
                priority=5,
                tags=["rotating_light"],
            )
            alerted = True

        if once:
            return down
        await sleep(interval)


def _iso(unix_ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
