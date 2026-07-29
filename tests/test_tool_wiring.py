"""Tests for the dep-injection wiring fixes in PR #181 review.

Specifically:
* ``_channel_from_config_or_state`` precedence (LangGraph configurable
  vs module-global _STATE vs explicit arg).
* ``Agent.__init__`` accepts and stores ``commitments_store``.
* ``set_commitments_store`` / ``set_spawn_config`` actually populate
  the module-global _STATE so the four commitment_* tools + the spawn
  tool can resolve their dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mimir.tools.registry import (
    _STATE,
    _channel_from_config_or_state,
    fetch_channel_history,
    react,
    reset_current_channel_id,
    send_message,
    set_channel_registry,
    set_commitments_store,
    set_current_channel_id,
    set_spawn_config,
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
        # Even with the contextvar and config populated, explicit arg dominates.
        tok = set_current_channel_id("contextvar-chan")
        try:
            config = {"configurable": {"channel_id": "config-chan"}}
            assert _channel_from_config_or_state("explicit-chan", config) == "explicit-chan"
        finally:
            reset_current_channel_id(tok)

    def test_config_wins_over_contextvar(self) -> None:
        # Empty/None arg + populated config → config used. This is the
        # canonical path; concurrent turns on different channels each
        # see their own configurable.
        tok = set_current_channel_id("contextvar-chan")
        try:
            config = {"configurable": {"channel_id": "config-chan"}}
            assert _channel_from_config_or_state(None, config) == "config-chan"
            assert _channel_from_config_or_state("", config) == "config-chan"
            assert _channel_from_config_or_state("   ", config) == "config-chan"
        finally:
            reset_current_channel_id(tok)

    def test_contextvar_used_when_config_empty(self) -> None:
        # ContextVar fallback for the claude-code path (S2-1 fix).
        # Production dispatcher path uses config (above).
        tok = set_current_channel_id("contextvar-chan")
        try:
            assert _channel_from_config_or_state(None, None) == "contextvar-chan"
            assert _channel_from_config_or_state(None, {"configurable": {}}) == "contextvar-chan"
        finally:
            reset_current_channel_id(tok)

    def test_all_empty_returns_empty_string(self) -> None:
        # No explicit arg, no config, no contextvar set → empty string.
        assert _channel_from_config_or_state(None, None) == ""
        assert _channel_from_config_or_state("", {"configurable": {}}) == ""

    def test_set_current_channel_id_back_compat(self) -> None:
        tok = set_current_channel_id("legacy-chan")
        assert _channel_from_config_or_state(None, None) == "legacy-chan"
        reset_current_channel_id(tok)
        assert _channel_from_config_or_state(None, None) == ""

    def test_contextvar_resolves_when_config_is_empty_runnableconfig(self) -> None:
        # Regression guard for the claude-code path: the langchain-
        # claude-code SDK shim calls ``tool._arun(**args,
        # config=RunnableConfig())`` with an empty config, so the
        # RunnableConfig route can't see channel_id. ContextVar carries
        # the channel through that gap (S2-1 fix). Pre-181-G run_turn
        # never set _STATE under the new RunnableConfig design;
        # send_message would fail with "no channel_id".
        tok = set_current_channel_id("from-contextvar")
        try:
            # Simulate the patch's empty RunnableConfig: a dict with no
            # ``configurable`` key (or an empty configurable).
            for empty_cfg in ({}, {"configurable": {}}, None):
                assert _channel_from_config_or_state(None, empty_cfg) == "from-contextvar"
        finally:
            reset_current_channel_id(tok)


# ─── Agent.__init__ stores commitments_store ───────────────────────


def test_agent_init_accepts_commitments_store(tmp_path: Path) -> None:
    from mimir.agent import Agent
    from mimir.config import Config
    from mimir.history import MessageBuffer
    from mimir.index import IndexGenerator
    from mimir.turn_logger import TurnLogger

    # Build a minimal Config via from_env-equivalent path.
    import os

    os.environ["MIMIR_HOME"] = str(tmp_path)
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


def _minimal_agent(tmp_path: Path, saga_client=None):
    from mimir.agent import Agent
    from mimir.config import Config
    from mimir.history import MessageBuffer
    from mimir.index import IndexGenerator
    from mimir.turn_logger import TurnLogger
    import os

    os.environ["MIMIR_HOME"] = str(tmp_path)
    cfg = Config.from_env()
    return Agent(
        cfg,
        TurnLogger(path=tmp_path / "turns.jsonl"),
        MessageBuffer(history_path=tmp_path / "history.jsonl"),
        IndexGenerator(home=cfg.home),
        saga_client=saga_client,
    )


def test_agent_stashes_sagastore_for_skill_memory(tmp_path: Path) -> None:
    """chainlink #266: _try_inject_memory_client peels the wrapper chain to
    a concrete SagaStore and stashes it on _saga_store for the skill-memory
    load injection."""
    from mimir.saga.client import SagaStore

    store = SagaStore(db_path=tmp_path / "saga.db", embedding_dim=4)

    # Wrapped, as in production (RecordingSagaClient-style _inner chain).
    class _Wrap:
        def __init__(self, inner):
            self._inner = inner

    agent = _minimal_agent(tmp_path, saga_client=_Wrap(store))
    assert agent._saga_store is store


def test_agent_saga_store_none_when_no_sagastore(tmp_path: Path) -> None:
    """A non-SagaStore client (legacy / stub) leaves _saga_store None, so
    the skill-memory injection cleanly no-ops."""
    agent = _minimal_agent(tmp_path, saga_client=object())
    assert agent._saga_store is None


# ─── set_commitments_store / set_spawn_config populate _STATE ──────


def test_set_commitments_store_populates_state() -> None:
    fake = object()
    set_commitments_store(fake)
    assert _STATE["commitments_store"] is fake


def test_set_spawn_config_populates_state(tmp_path: Path) -> None:
    cfg = {"default_cwd": tmp_path}
    set_spawn_config(cfg)
    assert _STATE["spawn_config"] is cfg


# ─── InjectedToolArg schema fix (chainlink #147) ───────────────────


class TestChannelToolsInjectedToolArg:
    """``config: Annotated[RunnableConfig | None, InjectedToolArg]`` on the
    three channel tools must exclude ``config`` from the tool_call_schema
    that the LangChain / LangGraph agent sends to the LLM.  Without the
    annotation the LLM can include ``config`` in its tool call args, which
    then collides with LangChain's internal ``config`` injection inside
    ``StructuredTool.arun`` → "got multiple values for keyword argument
    'config'" (observed post-PR-#181, filed as chainlink #147).
    """

    def test_send_message_config_not_in_tool_call_schema(self) -> None:
        # tool_call_schema is what LangChain exposes to the LLM. config must
        # be absent so the model never includes it in its tool call args.
        schema = send_message.tool_call_schema.model_json_schema()
        props = list(schema.get("properties", {}).keys())
        assert "config" not in props, f"config leaked into tool_call_schema: {props}"
        assert "text" in props
        assert "channel_id" in props

    def test_react_config_not_in_tool_call_schema(self) -> None:
        schema = react.tool_call_schema.model_json_schema()
        props = list(schema.get("properties", {}).keys())
        assert "config" not in props, f"config leaked into tool_call_schema: {props}"
        assert "emoji" in props

    def test_fetch_channel_history_config_not_in_tool_call_schema(self) -> None:
        schema = fetch_channel_history.tool_call_schema.model_json_schema()
        props = list(schema.get("properties", {}).keys())
        assert "config" not in props, f"config leaked into tool_call_schema: {props}"
        assert "limit" in props

    def test_send_message_filter_injected_args_removes_config(self) -> None:
        # _filter_injected_args is used in StructuredTool.arun for callbacks
        # and also confirms InjectedToolArg is properly recognized.
        raw = {"text": "hello", "channel_id": "test", "config": None}
        filtered = send_message._filter_injected_args(raw)
        assert "config" not in filtered
        assert filtered["text"] == "hello"
        assert filtered["channel_id"] == "test"

    @pytest.mark.asyncio
    async def test_send_message_ainvoke_without_config_in_args(self) -> None:
        # Normal invocation: model never passes config (correct post-fix behavior).
        # Should succeed (or return a diagnostic string from the tool itself),
        # not raise TypeError about missing required argument.
        from mimir.channel_registry import ChannelRegistry

        set_channel_registry(ChannelRegistry())
        set_current_channel_id("test-chan")
        result = await send_message.ainvoke(
            {"text": "hello", "channel_id": "test-chan"}
        )
        # Tool returns a string result (no bridge configured → diagnostic msg)
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_send_message_ainvoke_with_config_in_args_no_collision(self) -> None:
        # Guard against regression: if config somehow ends up in the tool call
        # args (e.g., from an old schema cached by the LLM), ainvoke must not
        # raise "got multiple values for keyword argument 'config'".
        from langchain_core.runnables import RunnableConfig
        from mimir.channel_registry import ChannelRegistry

        set_channel_registry(ChannelRegistry())
        set_current_channel_id("test-chan")
        # Pass config=None explicitly in the args dict (simulates a model that
        # was trained on the old schema). Must not collide with LangChain's
        # internal config injection in StructuredTool.arun.
        result = await send_message.ainvoke(
            {"text": "hello", "channel_id": "test-chan", "config": None},
            config=RunnableConfig(),
        )
        assert isinstance(result, str)
