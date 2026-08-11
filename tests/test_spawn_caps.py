from __future__ import annotations

import asyncio
import os

import pytest

from mimir.tools.registry import (
    _SPAWN_DEPTH_ENV,
    _SPAWN_GUARD,
    _spawn_acquire_rate_slot,
    _spawn_guard_init,
    _spawn_release_rate_slot,
    _spawn_reset_for_tests,
)


@pytest.fixture(autouse=True)
def reset(monkeypatch: pytest.MonkeyPatch) -> None:
    _spawn_reset_for_tests()
    for name in (
        _SPAWN_DEPTH_ENV,
        "MIMIR_SPAWN_MAX_CONCURRENT",
        "MIMIR_SPAWN_MAX_PER_HOUR",
        "MIMIR_SPAWN_MAX_DEPTH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_guard_defaults() -> None:
    guard = _spawn_guard_init()
    assert guard.max_concurrent == 3
    assert guard.max_per_hour == 20
    assert guard.max_depth == 2


def test_guard_operator_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_SPAWN_MAX_CONCURRENT", "1")
    monkeypatch.setenv("MIMIR_SPAWN_MAX_PER_HOUR", "4")
    monkeypatch.setenv("MIMIR_SPAWN_MAX_DEPTH", "3")
    guard = _spawn_guard_init()
    assert (guard.max_concurrent, guard.max_per_hour, guard.max_depth) == (1, 4, 3)


def test_invalid_caps_use_defaults_and_zero_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_SPAWN_MAX_CONCURRENT", "invalid")
    monkeypatch.setenv("MIMIR_SPAWN_MAX_PER_HOUR", "0")
    guard = _spawn_guard_init()
    assert guard.max_concurrent == 3
    assert guard.max_per_hour == 1


@pytest.mark.asyncio
async def test_rate_cap_refuses_after_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_SPAWN_MAX_PER_HOUR", "2")
    guard = _spawn_guard_init()
    first, error = await _spawn_acquire_rate_slot(guard, "spawn_open_code")
    assert first is not None and error is None
    second, error = await _spawn_acquire_rate_slot(guard, "spawn_open_code")
    assert second is not None and error is None
    token, error = await _spawn_acquire_rate_slot(guard, "spawn_open_code")
    assert token is None
    assert "per-hour cap" in error


@pytest.mark.asyncio
async def test_release_removes_only_own_rate_token() -> None:
    guard = _spawn_guard_init()
    first, _ = await _spawn_acquire_rate_slot(guard, "spawn_open_code")
    second, _ = await _spawn_acquire_rate_slot(guard, "spawn_open_code")
    await _spawn_release_rate_slot(guard, first)
    assert list(guard.recent) == [second]


@pytest.mark.asyncio
async def test_semaphore_enforces_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_SPAWN_MAX_CONCURRENT", "1")
    guard = _spawn_guard_init()
    assert guard.sem is not None
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def holder(label: str) -> None:
        assert guard.sem is not None
        async with guard.sem:
            order.append(label)
            if label == "first":
                entered.set()
                await release.wait()

    first = asyncio.create_task(holder("first"))
    await entered.wait()
    second = asyncio.create_task(holder("second"))
    await asyncio.sleep(0)
    assert order == ["first"]
    release.set()
    await asyncio.gather(first, second)
    assert order == ["first", "second"]


def test_reset_clears_loop_bound_state() -> None:
    guard = _spawn_guard_init()
    guard.recent.extend((1.0, 2.0))
    _spawn_reset_for_tests()
    assert _SPAWN_GUARD.sem is None
    assert _SPAWN_GUARD.rate_lock is None
    assert not _SPAWN_GUARD.recent
