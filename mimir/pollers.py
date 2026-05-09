"""Pollers framework — subprocess-shaped external-state watchers.

chainlink #3. Pollers live alongside skills under
``<home>/.claude/skills/<name>/`` with a ``pollers.json`` manifest that
declares one or more poller scripts. The scheduler discovers them at
startup (and on the ``mcp__mimir__reload_pollers`` MCP tool call), runs
each on its declared cron, and parses the script's stdout as JSONL.
Each emitted line becomes an ``AgentEvent`` that wakes mimir on a
known channel, exactly like an inbound bridge message.

Why a separate framework from ``register_callable``: in-process
callables are mimir-internal maintenance (saga consolidation, OAuth
quota poll, etc.) — they mutate in-memory state and run regardless
of whether anything changed. Pollers are user-facing watch jobs
that emit events when external state changes; they're isolated as
subprocesses (any language, no mimir import path coupling) and
silence-on-no-change is the filter. New pollers ship as a skill
directory drop, no mimir release required.

Ported from open-strix's ``open_strix.scheduler._discover_pollers`` /
``_on_poller_fire`` (2026-04 vintage).

Output contract (matches open-strix):
- **stdout**: JSONL, one ``{"poller": str, "prompt": str, ...}`` per
  actionable event. Other keys (``source_platform``, etc.) flow
  through to the AgentEvent's ``extra``.
- **stderr**: free-form diagnostic output. Captured and emitted as
  a ``poller_stderr`` event for observability; not forwarded to the
  agent.
- **exit 0**: success (zero events is fine — silence means nothing
  to report). **Non-zero**: error, surfaces as ``poller_nonzero_exit``.

Subprocess gets these env vars injected automatically:
- ``STATE_DIR`` — the skill directory (writable, cursor/state files).
- ``POLLER_NAME`` — the poller's name from pollers.json.
- The host process's environment, plus ``env`` overrides from the
  poller's pollers.json entry.

The 60-second timeout is hard-capped; longer-running pollers should
either run faster or restructure as ``async-tasks``-style background
jobs that emit on completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .event_logger import log_event
from .models import AgentEvent

log = logging.getLogger(__name__)

POLLER_TIMEOUT_SECONDS = 60
# Cap stderr text recorded in events.jsonl so a chatty poller doesn't
# blow the algedonic stream's storage budget.
POLLER_STDERR_LOG_CHARS = 2000
# Cap the per-line stdout payload kept in events.jsonl on parse error
# (bad JSON line). Truncated for the same reason.
POLLER_INVALID_LINE_CHARS = 500


@dataclass
class PollerConfig:
    """One poller declared in a skill's ``pollers.json``.

    ``skill_dir`` is the absolute path to the skill directory; the
    subprocess runs with that as its cwd and ``STATE_DIR``. ``env``
    are extra env vars from the json entry's ``env`` map (already
    coerced to ``dict[str, str]``)."""

    name: str
    command: str
    cron: str
    env: dict[str, str]
    skill_dir: Path

    def channel_id(self) -> str:
        """Synthetic channel for emitted events. Mirrors the
        ``scheduler:<name>`` convention used for null-channel
        scheduler.yaml jobs — keeps poller events queue-isolated
        per-poller (parallel across pollers, serialized within)."""
        return f"poller:{self.name}"


def discover_pollers(skills_dir: Path) -> list[PollerConfig]:
    """Walk ``skills_dir/**/pollers.json`` and parse out poller configs.

    Sync — called from the Scheduler at startup before the event loop
    spins up. Per-file failures (bad JSON, missing required fields)
    log a stderr-visible warning but don't abort the walk; one bad
    skill shouldn't take the whole framework down. Returns an empty
    list when ``skills_dir`` doesn't exist (most installs).
    """
    pollers: list[PollerConfig] = []
    if not skills_dir.exists():
        return pollers

    for pollers_file in sorted(skills_dir.rglob("pollers.json")):
        skill_dir = pollers_file.parent
        try:
            raw = json.loads(pollers_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "poller_invalid_json: %s — %s", pollers_file, exc,
            )
            continue

        if not isinstance(raw, dict) or "pollers" not in raw:
            log.warning(
                "poller_invalid_format: %s — expected dict with 'pollers' key",
                pollers_file,
            )
            continue
        entries = raw.get("pollers")
        if not isinstance(entries, list):
            log.warning(
                "poller_invalid_format: %s — 'pollers' must be a list",
                pollers_file,
            )
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            command = str(entry.get("command", "")).strip()
            cron = str(entry.get("cron", "")).strip()
            if not name or not command or not cron:
                log.warning(
                    "poller_missing_fields: %s — entry %r",
                    pollers_file, entry,
                )
                continue
            env_raw = entry.get("env", {})
            if not isinstance(env_raw, dict):
                env_raw = {}
            pollers.append(
                PollerConfig(
                    name=name,
                    command=command,
                    cron=cron,
                    env={str(k): str(v) for k, v in env_raw.items()},
                    skill_dir=skill_dir,
                ),
            )
    return pollers


async def run_poller(
    poller: PollerConfig,
    *,
    enqueue: Callable[[AgentEvent], Awaitable[bool]],
    timeout: float = POLLER_TIMEOUT_SECONDS,
) -> int:
    """Run one poller subprocess; parse its stdout JSONL; enqueue
    each emitted event. Returns the count of events emitted (0 on
    timeout / error / silence).

    Always logs a ``poller_complete`` event at the end so the operator
    can audit "did the poll cycle run?" even when nothing was emitted.
    """
    env = {**os.environ, **poller.env}
    env["STATE_DIR"] = str(poller.skill_dir)
    env["POLLER_NAME"] = poller.name

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            poller.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(poller.skill_dir),
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await log_event(
            "poller_timeout",
            poller=poller.name,
            timeout_seconds=int(timeout),
        )
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return 0
    except Exception as exc:  # noqa: BLE001 — never let a poller break the scheduler
        await log_event(
            "poller_exec_error",
            poller=poller.name,
            error=f"{type(exc).__name__}: {exc}",
        )
        return 0

    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    if stderr_text:
        await log_event(
            "poller_stderr",
            poller=poller.name,
            stderr=stderr_text[:POLLER_STDERR_LOG_CHARS],
        )

    if proc.returncode != 0:
        await log_event(
            "poller_nonzero_exit",
            poller=poller.name,
            returncode=proc.returncode,
        )
        return 0

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        await log_event(
            "poller_complete",
            poller=poller.name,
            events_emitted=0,
        )
        return 0

    event_count = 0
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            await log_event(
                "poller_invalid_line",
                poller=poller.name,
                line=line[:POLLER_INVALID_LINE_CHARS],
            )
            continue

        if not isinstance(parsed, dict):
            continue
        prompt = str(parsed.get("prompt", "")).strip()
        if not prompt:
            continue

        # Strip the framework-required keys before stuffing the rest
        # into AgentEvent.extra so downstream prompt rendering can
        # surface platform-specific metadata (source_platform, urls,
        # etc.) without colliding with the AgentEvent dataclass shape.
        extras = {
            k: v for k, v in parsed.items()
            if k not in ("prompt", "poller")
        }

        event = AgentEvent(
            trigger="poller",
            channel_id=poller.channel_id(),
            content=prompt,
            source="poller",
            source_id=f"poller:{poller.name}:{event_count}",
            extra={"poller_name": poller.name, **extras},
        )
        try:
            accepted = await enqueue(event)
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "poller_enqueue_error",
                poller=poller.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        if accepted:
            event_count += 1

    await log_event(
        "poller_complete",
        poller=poller.name,
        events_emitted=event_count,
    )
    return event_count


__all__ = (
    "PollerConfig",
    "discover_pollers",
    "run_poller",
    "POLLER_TIMEOUT_SECONDS",
)
