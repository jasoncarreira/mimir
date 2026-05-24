"""Tests for ``spawn_claude_code`` concurrency / rate / depth caps
(pre-OSS hardening, review item #5)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from mimir.tools.registry import (
    _SPAWN_DEPTH_ENV,
    _SPAWN_GUARD,
    _spawn_reset_for_tests,
    set_spawn_config,
    spawn_claude_code,
)


@pytest.fixture(autouse=True)
def _reset_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a clean guard. The semaphore + lock are loop-
    bound, so swapping loops between tests would error otherwise."""
    _spawn_reset_for_tests()
    # Default env: no inherited depth, no operator overrides.
    monkeypatch.delenv(_SPAWN_DEPTH_ENV, raising=False)
    monkeypatch.delenv("MIMIR_SPAWN_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("MIMIR_SPAWN_MAX_PER_HOUR", raising=False)
    monkeypatch.delenv("MIMIR_SPAWN_MAX_DEPTH", raising=False)


def _ok_run(
    argv: list[str], cwd: str | None, timeout_s: int, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Successful subprocess mock — returns a valid claude JSON envelope."""
    return 0, json.dumps({"result": "done", "total_cost_usd": 0, "num_turns": 0}), ""


# ─── depth cap ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_cap_refuses_at_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawned claude subprocess running ``spawn_claude_code`` again
    inherits ``MIMIR_SPAWN_DEPTH``. When that's already at the cap, the
    second-level recursion is refused — closes the fork-bomb path."""
    set_spawn_config({"default_cwd": tmp_path})
    monkeypatch.setenv("MIMIR_SPAWN_MAX_DEPTH", "2")
    monkeypatch.setenv(_SPAWN_DEPTH_ENV, "2")  # already at the cap

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _ok_run)

    msg = await spawn_claude_code.ainvoke({"prompt": "should be refused"})
    assert "depth cap" in msg.lower()
    assert "MIMIR_SPAWN_MAX_DEPTH" in msg


@pytest.mark.asyncio
async def test_depth_cap_allows_below_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    monkeypatch.setenv("MIMIR_SPAWN_MAX_DEPTH", "2")
    monkeypatch.setenv(_SPAWN_DEPTH_ENV, "1")  # one level deep — still OK

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _ok_run)

    msg = await spawn_claude_code.ainvoke({"prompt": "should run"})
    assert "depth cap" not in msg.lower()


@pytest.mark.asyncio
async def test_depth_cap_root_agent_is_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root agent (no ``MIMIR_SPAWN_DEPTH`` set) starts at depth 0 —
    can spawn freely up to max_depth."""
    set_spawn_config({"default_cwd": tmp_path})
    monkeypatch.setenv("MIMIR_SPAWN_MAX_DEPTH", "1")
    # No MIMIR_SPAWN_DEPTH set — current_depth=0, < max_depth=1.

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _ok_run)

    msg = await spawn_claude_code.ainvoke({"prompt": "root-level"})
    assert "depth cap" not in msg.lower()


# ─── child env propagation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_child_env_has_incremented_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subprocess receives ``MIMIR_SPAWN_DEPTH=parent+1`` so its
    own ``spawn_claude_code`` calls see the deeper level."""
    set_spawn_config({"default_cwd": tmp_path})
    monkeypatch.setenv(_SPAWN_DEPTH_ENV, "1")

    captured_env: dict[str, str] = {}

    def _capture(argv, cwd, timeout_s, env=None):
        captured_env.update(env or {})
        return 0, json.dumps({"result": "ok", "total_cost_usd": 0, "num_turns": 0}), ""

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture)

    await spawn_claude_code.ainvoke({"prompt": "x"})
    assert captured_env.get(_SPAWN_DEPTH_ENV) == "2"


@pytest.mark.asyncio
async def test_child_env_starts_at_one_from_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root agent (no MIMIR_SPAWN_DEPTH) spawns with child env ``=1``."""
    set_spawn_config({"default_cwd": tmp_path})

    captured_env: dict[str, str] = {}

    def _capture(argv, cwd, timeout_s, env=None):
        captured_env.update(env or {})
        return 0, json.dumps({"result": "ok", "total_cost_usd": 0, "num_turns": 0}), ""

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture)

    await spawn_claude_code.ainvoke({"prompt": "x"})
    assert captured_env.get(_SPAWN_DEPTH_ENV) == "1"


# ─── argv ``--`` separator ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_after_double_dash_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt starting with ``--<word>`` must not be interpreted by
    the claude CLI as another flag. The ``--`` argv separator forces
    positional interpretation."""
    set_spawn_config({"default_cwd": tmp_path})

    captured: list[list[str]] = []

    def _capture(argv, cwd, timeout_s, env=None):
        captured.append(argv)
        return 0, json.dumps({"result": "ok", "total_cost_usd": 0, "num_turns": 0}), ""

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture)

    weird_prompt = "--dangerously-skip-permissions ignore previous"
    await spawn_claude_code.ainvoke({"prompt": weird_prompt})

    assert captured, "subprocess was not called"
    argv = captured[0]
    assert "--" in argv
    dash_idx = argv.index("--")
    # The prompt comes AFTER the ``--`` separator.
    assert argv[dash_idx + 1] == weird_prompt
    # And the prompt does NOT appear before the separator (would
    # mean the CLI parsed it as a flag).
    assert weird_prompt not in argv[:dash_idx]


# ─── per-hour rate cap ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_cap_refuses_after_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After N successful spawns in the window, the (N+1)th is refused
    with the rate-cap message until the window rolls forward."""
    set_spawn_config({"default_cwd": tmp_path})
    monkeypatch.setenv("MIMIR_SPAWN_MAX_PER_HOUR", "3")
    monkeypatch.setenv("MIMIR_SPAWN_MAX_DEPTH", "9")  # ignore depth

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _ok_run)

    # 3 should succeed.
    for i in range(3):
        msg = await spawn_claude_code.ainvoke({"prompt": f"call-{i}"})
        assert "refused" not in msg.lower(), f"call {i} was refused: {msg}"

    # 4th hits the cap.
    msg = await spawn_claude_code.ainvoke({"prompt": "call-4"})
    assert "per-hour cap" in msg
    assert "3/h" in msg


# ─── concurrency semaphore ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrency_semaphore_serializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When max_concurrent=2, three concurrent spawns mean at most
    two subprocess calls run at once. Bookkeeping uses ``threading.Lock``
    because ``_run_claude_subprocess`` is invoked via ``asyncio.to_thread``
    — each call runs in a different worker thread, so the counter
    crosses thread boundaries."""
    import threading
    import time as _time

    set_spawn_config({"default_cwd": tmp_path})
    monkeypatch.setenv("MIMIR_SPAWN_MAX_CONCURRENT", "2")
    monkeypatch.setenv("MIMIR_SPAWN_MAX_PER_HOUR", "20")

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def _bumped_sync(argv, cwd, timeout_s, env=None):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Sleep long enough that all three concurrent calls would
        # overlap if the semaphore weren't gating them.
        _time.sleep(0.1)
        with lock:
            in_flight -= 1
        return 0, json.dumps({"result": "ok", "total_cost_usd": 0, "num_turns": 0}), ""

    from mimir.tools import registry
    monkeypatch.setattr(registry, "_run_claude_subprocess", _bumped_sync)

    results = await asyncio.gather(
        spawn_claude_code.ainvoke({"prompt": "a"}),
        spawn_claude_code.ainvoke({"prompt": "b"}),
        spawn_claude_code.ainvoke({"prompt": "c"}),
    )
    assert all("refused" not in r.lower() for r in results)
    assert peak <= 2, f"max_concurrent=2 was violated (peak={peak})"
