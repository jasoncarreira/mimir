"""Tests for the dep-injection wiring fixes in PR #181 review.

Specifically:
* ``_channel_from_config_or_state`` precedence (LangGraph configurable
  vs module-global _STATE vs explicit arg).
* ``Agent.__init__`` accepts and stores ``commitments_store``.
* ``spawn_claude_code`` is async (no event-loop block) and wraps
  subprocess.run in to_thread.
* ``set_commitments_store`` / ``set_spawn_config`` actually populate
  the module-global _STATE so the four commitment_* tools + the spawn
  tool can resolve their dependencies.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mimir.tools.registry import (
    _STATE,
    _channel_from_config_or_state,
    set_commitments_store,
    set_current_channel_id,
    set_spawn_config,
    spawn_claude_code,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    snapshot = dict(_STATE)
    yield
    _STATE.clear()
    _STATE.update(snapshot)


# ─── channel_id resolution ─────────────────────────────────────────


class TestChannelFromConfigOrState:
    def test_explicit_arg_wins(self) -> None:
        # Even with _STATE and config populated, explicit arg dominates.
        _STATE["current_channel_id"] = "state-chan"
        config = {"configurable": {"channel_id": "config-chan"}}
        assert _channel_from_config_or_state("explicit-chan", config) == "explicit-chan"

    def test_config_wins_over_state(self) -> None:
        # Empty/None arg + populated config → config used. This is the
        # canonical path post-fix; concurrent turns on different
        # channels each see their own configurable.
        _STATE["current_channel_id"] = "state-chan"
        config = {"configurable": {"channel_id": "config-chan"}}
        assert _channel_from_config_or_state(None, config) == "config-chan"
        assert _channel_from_config_or_state("", config) == "config-chan"
        assert _channel_from_config_or_state("   ", config) == "config-chan"

    def test_state_used_when_config_empty(self) -> None:
        # Back-compat fallback for tests / scripts that still call
        # set_current_channel_id directly. Production dispatcher path
        # uses config (above).
        _STATE["current_channel_id"] = "state-chan"
        assert _channel_from_config_or_state(None, None) == "state-chan"
        assert _channel_from_config_or_state(None, {"configurable": {}}) == "state-chan"

    def test_all_empty_returns_empty_string(self) -> None:
        _STATE["current_channel_id"] = None
        assert _channel_from_config_or_state(None, None) == ""
        assert _channel_from_config_or_state("", {"configurable": {}}) == ""

    def test_set_current_channel_id_back_compat(self) -> None:
        set_current_channel_id("legacy-chan")
        assert _channel_from_config_or_state(None, None) == "legacy-chan"
        set_current_channel_id(None)
        assert _channel_from_config_or_state(None, None) == ""

    def test_state_resolves_when_config_is_empty_runnableconfig(self) -> None:
        # Regression guard for the claude-code path: the langchain-
        # claude-code SDK shim calls ``tool._arun(**args,
        # config=RunnableConfig())`` with an empty config, so the
        # RunnableConfig route can't see channel_id. _STATE must
        # carry the channel through that gap. Pre-181-G run_turn
        # never set _STATE under the new RunnableConfig design;
        # send_message would fail with "no channel_id".
        set_current_channel_id("from-state")
        # Simulate the patch's empty RunnableConfig: a dict with no
        # ``configurable`` key (or an empty configurable).
        for empty_cfg in ({}, {"configurable": {}}, None):
            assert _channel_from_config_or_state(None, empty_cfg) == "from-state"


# ─── Agent.__init__ stores commitments_store ───────────────────────


def test_agent_init_accepts_commitments_store(tmp_path: Path) -> None:
    from mimir.agent import Agent
    from mimir.config import Config
    from mimir.history import MessageBuffer
    from mimir.index import IndexGenerator
    from mimir.turn_logger import TurnLogger

    # Build a minimal Config via from_env-equivalent path.
    import os

    os.environ.setdefault("MIMIR_HOME", str(tmp_path))
    cfg = Config.from_env()

    fake_store = object()
    agent = Agent(
        cfg,
        TurnLogger(path=tmp_path / "turns.jsonl"),
        MessageBuffer(history_path=tmp_path / "history.jsonl"),
        IndexGenerator(home=cfg.home),
        commitments_store=fake_store,
    )
    # The attribute is the contract that server.py:_on_startup reads
    # via getattr(agent, "_commitments", None).
    assert agent._commitments is fake_store


# ─── set_commitments_store / set_spawn_config populate _STATE ──────


def test_set_commitments_store_populates_state() -> None:
    fake = object()
    set_commitments_store(fake)
    assert _STATE["commitments_store"] is fake


def test_set_spawn_config_populates_state(tmp_path: Path) -> None:
    cfg = {"default_cwd": tmp_path}
    set_spawn_config(cfg)
    assert _STATE["spawn_config"] is cfg


# ─── spawn_claude_code async + to_thread ───────────────────────────


def test_spawn_claude_code_is_coroutine() -> None:
    # Pre-fix this was a sync function — deepagents would await a
    # synchronous callable, freezing the event loop for ``timeout_s``
    # seconds. langchain's @tool decorator routes async functions to
    # ``coroutine`` (not ``func``); verify the async path is wired.
    assert spawn_claude_code.coroutine is not None
    assert asyncio.iscoroutinefunction(spawn_claude_code.coroutine)
    # Sync path should be unset, otherwise deepagents would prefer it.
    assert spawn_claude_code.func is None


@pytest.mark.asyncio
async def test_spawn_claude_code_does_not_block_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verify the blocking subprocess.run is dispatched to a thread by
    # checking that other coroutines on the loop still make progress
    # while the "subprocess" is "running" (mocked as a sleep).
    set_spawn_config({"default_cwd": tmp_path})

    from mimir.tools import registry

    sleep_started = asyncio.Event()
    other_tick_seen = asyncio.Event()

    def _slow_run(argv: list[str], cwd: str | None, timeout_s: int) -> tuple[int, str, str]:
        # Simulate a blocking subprocess that takes time. If this runs
        # on the main loop, `other_tick_seen` will never get set.
        sleep_started.set()
        import time
        time.sleep(0.5)
        return 0, json.dumps({"result": "ok", "total_cost_usd": 0, "num_turns": 0}), ""

    monkeypatch.setattr(registry, "_run_claude_subprocess", _slow_run)

    async def _tick() -> None:
        await sleep_started.wait()
        # If spawn_claude_code is actually running on the loop thread,
        # this `asyncio.sleep(0)` would block; instead it should yield
        # back immediately because the subprocess.run is in a thread.
        await asyncio.sleep(0.05)
        other_tick_seen.set()

    tick_task = asyncio.create_task(_tick())
    result_str = await spawn_claude_code.ainvoke(
        {"prompt": "hello", "timeout_s": 30}
    )
    await asyncio.wait_for(tick_task, timeout=2.0)

    assert other_tick_seen.is_set()
    result = json.loads(result_str)
    assert result["result"] == "ok"


@pytest.mark.asyncio
async def test_spawn_claude_code_handles_missing_config(tmp_path: Path) -> None:
    set_spawn_config(None)
    msg = await spawn_claude_code.ainvoke({"prompt": "x"})
    assert "no spawn config" in msg


@pytest.mark.asyncio
async def test_spawn_claude_code_rejects_empty_prompt(tmp_path: Path) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    msg = await spawn_claude_code.ainvoke({"prompt": "   "})
    assert "prompt is required" in msg


@pytest.mark.asyncio
async def test_spawn_claude_code_surfaces_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})

    from mimir.tools import registry

    def _timeout_run(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(registry, "_run_claude_subprocess", _timeout_run)
    msg = await spawn_claude_code.ainvoke({"prompt": "x", "timeout_s": 1})
    assert "timed out" in msg


@pytest.mark.asyncio
async def test_spawn_claude_code_handles_missing_claude_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})

    from mimir.tools import registry

    def _missing_cli(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        raise FileNotFoundError("claude not on PATH")

    monkeypatch.setattr(registry, "_run_claude_subprocess", _missing_cli)
    msg = await spawn_claude_code.ainvoke({"prompt": "x"})
    assert "not on PATH" in msg
