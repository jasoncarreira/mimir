"""Server-side, model-free operator notices for retained factory parks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .._atomic import atomic_write_json
from ..bridges._mentions import neutralize_display_name
from ..event_logger import log_event
from ..ntfy import post_algedonic_alarm
from .autonomy import chainlink_bin
from .factory_state import FactoryRunRecord, list_factory_records


_STATE_FILE = "factory-park-alerts.json"


def park_dedupe_key(record: FactoryRunRecord) -> str:
    """Hash only the park identity, never the rest of the mutable status."""
    assert record.status is not None
    identity = json.dumps(
        {"attempt": record.attempt, "terminal_result": record.status.terminal_result},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return f"{record.run_id}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _state_path(home: Path) -> Path:
    return home / "state" / "worklink" / _STATE_FILE


def _load_state(home: Path) -> dict[str, list[str]]:
    path = _state_path(home)
    if not path.exists():
        return {}
    # Refuse damaged state rather than silently forgetting already-sent notices.
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("factory park alert state is unsafe")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, list)
        or any(sink not in {"channel", "ntfy", "unrouted"} for sink in value)
        for key, value in data.items()
    ):
        raise ValueError("factory park alert state is malformed")
    return data


def _save_state(home: Path, state: dict[str, list[str]]) -> None:
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state, mode=0o600)


def _issue_title(home: Path, issue_id: int) -> str:
    result = subprocess.run(
        [chainlink_bin(), "issue", "show", str(issue_id), "--json"],
        cwd=home, capture_output=True, text=True, check=False, timeout=15,
    )
    if result.returncode != 0:
        raise ValueError("chainlink issue lookup failed")
    data: Any = json.loads(result.stdout)
    if (not isinstance(data, dict) or type(data.get("id")) is not int
            or data["id"] != issue_id or not isinstance(data.get("title"), str)
            or not data["title"].strip()):
        raise ValueError("chainlink issue title or identity is invalid")
    return neutralize_display_name(data["title"], max_chars=200)


async def alert_parked_factory_runs(home: Path, channel: str, channels: Any) -> None:
    """Scan only live factory records; checkpoint each sink before revisiting it."""
    listing = await asyncio.to_thread(list_factory_records, home)
    for _ in listing.failures:
        await log_event("worklink_factory_park_record_skipped")
    try:
        state = await asyncio.to_thread(_load_state, home)
    except (OSError, ValueError, json.JSONDecodeError):
        await log_event("worklink_factory_park_state_unavailable")
        return

    channel = (channel or "").strip()
    ntfy_enabled = bool(os.environ.get("NTFY_TOPIC", "").strip())
    for record in listing.records:
        if record.controller_phase != "parked" or record.status is None or not record.status.is_parked:
            continue
        key = park_dedupe_key(record)
        done = set(state.get(key, []))
        sinks = ({"channel"} if channel else set()) | ({"ntfy"} if ntfy_enabled else set())
        if not sinks:
            if "unrouted" not in done:
                await log_event("worklink_factory_park_unrouted", issue_id=record.issue_id,
                                run_id=record.run_id, attempt=record.attempt)
                done.add("unrouted")
                state[key] = sorted(done)
                await asyncio.to_thread(_save_state, home, state)
            continue
        if sinks <= done:
            continue
        try:
            title = await asyncio.to_thread(_issue_title, home, record.issue_id)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            await log_event("worklink_factory_park_title_unavailable", issue_id=record.issue_id)
            continue
        body = (
            f"Factory epic chainlink #{record.issue_id}: {title}\n"
            f"Run: {record.run_id}; attempt: {record.attempt}; status: needs-human\n"
            f"Reason and requested decision: see the WORKLINK_BLOCKED comment on chainlink "
            f"#{record.issue_id}, or `mimir worklink status {record.issue_id}`"
        )
        if "channel" in sinks - done:
            try:
                result = await channels.send(channel, body, final=True)
                if not getattr(result, "sent", True):
                    raise RuntimeError("channel did not deliver")
            except Exception:  # noqa: BLE001 — retry this sink on the next scan
                await log_event("worklink_factory_park_delivery_failed", sink="channel",
                                issue_id=record.issue_id)
            else:
                done.add("channel")
                state[key] = sorted(done)
                await asyncio.to_thread(_save_state, home, state)
        if "ntfy" in sinks - done:
            try:
                await post_algedonic_alarm(
                    category="worklink-factory-parked", title=f"Factory epic #{record.issue_id}: {title}",
                    body=body, dedupe_key=key, priority=4,
                )
            except Exception:  # noqa: BLE001 — keep channel and ntfy independent
                await log_event("worklink_factory_park_delivery_failed", sink="ntfy",
                                issue_id=record.issue_id)
            else:
                # ntfy is best-effort and reports transport failures in its own events.
                done.add("ntfy")
                state[key] = sorted(done)
                await asyncio.to_thread(_save_state, home, state)
        if sinks <= done:
            await log_event("worklink_factory_park_alerted", issue_id=record.issue_id,
                            run_id=record.run_id, attempt=record.attempt, sinks=sorted(sinks))
