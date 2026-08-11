from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from mimir.tools.registry import (
    _SPAWN_DEPTH_ENV,
    _SPAWN_GUARD,
    _spawn_acquire_rate_slot,
    _spawn_guard_init,
    _spawn_release_rate_slot,
    _spawn_reset_for_tests,
    set_spawn_config,
    spawn_open_code,
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


def _surface_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mimir import contained_checkout, contained_execution
    from mimir.contained_execution import CollectedExecutionResult

    home = tmp_path / "home"
    seed = tmp_path / "seed"
    home.mkdir()
    seed.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:test-model")
    auth = home / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "test"}}))
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"], check=True)
    (seed / "README").write_text("seed")
    subprocess.run(["git", "-C", str(seed), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
    set_spawn_config({"default_cwd": tmp_path, "artifact_root": home / "artifacts"})
    calls = {"factory": 0, "envs": []}

    class Checkout:
        def __init__(self, path):
            self.path = path
            self.capability = SimpleNamespace(path=path)
            self.base_tree = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD^{tree}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        def close(self):
            pass

    def factory(source, **kwargs):
        calls["factory"] += 1
        destination = tmp_path / f"issued-{calls['factory']}"
        subprocess.run(["git", "clone", "-q", str(source), str(destination)], check=True)
        return Checkout(destination)

    async def runner(argv, directory, worker_env, projections=(), **kwargs):
        calls["envs"].append(dict(worker_env))
        return CollectedExecutionResult(0, b"", b"", False, False, 0, 0)

    monkeypatch.setattr(contained_checkout, "create_opencode_checkout", factory)
    monkeypatch.setattr(contained_execution, "execute_contained", runner)
    return seed, calls, contained_execution


@pytest.mark.asyncio
async def test_tool_depth_refusal_and_below_cap_propagation(tmp_path, monkeypatch):
    seed, calls, _module = _surface_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MIMIR_SPAWN_MAX_DEPTH", "2")
    monkeypatch.setenv(_SPAWN_DEPTH_ENV, "2")
    refused = await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(seed)})
    assert "depth cap" in refused
    assert not calls["envs"]

    _spawn_reset_for_tests()
    monkeypatch.setenv(_SPAWN_DEPTH_ENV, "1")
    payload = json.loads(await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(seed)}))
    assert payload["status"] == "succeeded"
    assert calls["envs"][-1][_SPAWN_DEPTH_ENV] == "2"


@pytest.mark.asyncio
async def test_tool_semaphore_bounds_concurrent_contained_spawns(tmp_path, monkeypatch):
    from mimir.contained_execution import CollectedExecutionResult
    seed, calls, contained_execution = _surface_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MIMIR_SPAWN_MAX_CONCURRENT", "1")
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def runner(argv, directory, worker_env, projections=(), **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        await release.wait()
        active -= 1
        return CollectedExecutionResult(0, b"", b"", False, False, 0, 0)

    monkeypatch.setattr(contained_execution, "execute_contained", runner)
    first = asyncio.create_task(spawn_open_code.ainvoke({"prompt": "one", "cwd": str(seed)}))
    await entered.wait()
    second = asyncio.create_task(spawn_open_code.ainvoke({"prompt": "two", "cwd": str(seed)}))
    await asyncio.sleep(0.05)
    assert maximum == 1
    release.set()
    await asyncio.gather(first, second)
    assert maximum == 1
