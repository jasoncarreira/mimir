"""Liveness beat + out-of-process watchdog (chainlink #507)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mimir.liveness import (
    beat_age_seconds,
    liveness_path,
    read_beat,
    run_watchdog,
    watchdog_has_sink,
    write_beat,
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    return home


# ── beat ────────────────────────────────────────────────────────────

def test_write_and_read_beat(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home, started_at=1000.0)
    beat = read_beat(home)
    assert isinstance(beat, dict)
    assert isinstance(beat["ts"], (int, float))
    assert beat["pid"] > 0
    assert beat["started_at"] == 1000.0
    age = beat_age_seconds(home)
    assert age is not None and 0 <= age < 5


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
async def test_once_stale_alerts(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_beat(home, ts=time.time() - 9999)
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    down = await run_watchdog(home, once=True, stale_after=60, _post=fake_post)
    assert down is True
    assert len(calls) == 1
    assert "🔴" in calls[0]["title"]
    assert calls[0]["dedupe_key"] == "agent-liveness-down"


@pytest.mark.asyncio
async def test_once_missing_beat_alerts(tmp_path: Path) -> None:
    home = _home(tmp_path)  # state/ exists but no beat file
    calls: list[dict] = []

    async def fake_post(**kw):
        calls.append(kw)

    down = await run_watchdog(home, once=True, stale_after=60, _post=fake_post)
    assert down is True
    assert len(calls) == 1


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
