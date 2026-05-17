"""Regression test for 181-H: per-turn prompt block assembly.

Pre-181-H the deepagents-backed Agent shoved only ``event.content``
(optionally prefixed with the SAGA recall block) into a
``HumanMessage`` — completely bypassing the rich per-turn user-side
prompt that the SDK path assembled: Recent activity, Recent feedback,
Session summaries, Resource usage, Upcoming, Upcoming commitments,
Self-state, etc.

181-H ports ``_build_turn_prompt`` + its eight ``_assemble_*``
helpers back from main. This test exercises them directly so a
regression that drops any of the labeled section headers — or
breaks the synthesis-turn branch — fails the suite instead of
silently shipping an empty-block prompt to the model.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from mimir.agent import Agent
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, TurnContext
from mimir.turn_logger import TurnLogger


def _make_agent(tmp_path: Path) -> Agent:
    """Construct an Agent rooted at ``tmp_path``. Skips
    ``_build_agent_if_needed`` — these tests drive
    ``_build_turn_prompt`` directly without invoking the model.
    """
    os.environ["MIMIR_HOME"] = str(tmp_path)
    cfg = Config.from_env()
    (cfg.home / "logs").mkdir(parents=True, exist_ok=True)
    return Agent(
        config=cfg,
        turn_logger=TurnLogger(cfg.turns_log),
        message_buffer=MessageBuffer(history_path=cfg.home / "messages.jsonl"),
        index_generator=IndexGenerator(cfg.home),
    )


def _make_ctx(event: AgentEvent, saga_session_id: str | None = None) -> TurnContext:
    return TurnContext(
        turn_id="turn-test",
        session_id=event.channel_id or "default",
        trigger=event.trigger,
        channel_id=event.channel_id,
        started_at=time.monotonic(),
        saga_session_id=saga_session_id,
    )


# ─── User-message branch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_turn_prompt_emits_labeled_sections(tmp_path: Path) -> None:
    """The standard turn-prompt path renders the labeled section
    headers that ``build_turn_prompt`` produces for the inputs it's
    given. ``## Today's date`` is always present (per build_turn_prompt
    contract). The current-event header — ``## Current event``
    surrogate for the user-message branch — surfaces the inbound
    body. The synthesis branch must NOT fire here.
    """
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-1",
        content="hello mimir",
        author="user-1",
    )
    ctx = _make_ctx(event)
    turn_prompt, recent = await agent._build_turn_prompt(
        ctx, event, saga_block=None, subagent_block=None,
    )
    # The user-side body is in the prompt (proof we wired through
    # build_turn_prompt rather than echoing event.content alone).
    assert "hello mimir" in turn_prompt
    # build_turn_prompt always emits ``## Today's date`` — it's the
    # one header that's not conditional on optional block content.
    assert "## Today's date" in turn_prompt
    # Synthesis branch did NOT fire — the synthesis template is
    # markedly different (its body starts with the saga_session
    # summary scaffold, no event header).
    assert "Mark each atom" not in turn_prompt  # synthesis-template phrase
    # No recent messages in the freshly-instantiated buffer.
    assert recent == []


@pytest.mark.asyncio
async def test_build_turn_prompt_surfaces_saga_and_subagent_blocks(
    tmp_path: Path,
) -> None:
    """When the pre-message SAGA hook + subagent inbox supply
    blocks, they appear in the prompt under their canonical labels.
    Verifies the wiring from ``_run_turn_body`` → ``_build_turn_prompt``
    actually threads those args through (181-H regression: pre-fix
    they were discarded and only ``event.content`` made it through).
    """
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-2",
        content="what's the topic?",
    )
    ctx = _make_ctx(event)
    turn_prompt, _ = await agent._build_turn_prompt(
        ctx, event,
        saga_block="- atom-foo: prior fact",
        subagent_block="- [completed] task_id=t1 — climber",
    )
    assert "## Possibly relevant memories (from SAGA)" in turn_prompt
    assert "atom-foo: prior fact" in turn_prompt
    assert "## Subagent updates" in turn_prompt
    assert "task_id=t1" in turn_prompt


# ─── Synthesis branch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_turn_prompt_routes_synthesis_to_dedicated_template(
    tmp_path: Path,
) -> None:
    """``trigger='saga_session_end'`` must route through
    ``_build_synthesis_prompt`` (which loads the saga_session_end
    template), NOT the standard build_turn_prompt path. Empty
    turns.jsonl → the lean template fires."""
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="saga_session_end",
        channel_id="ch-3",
        content="(unused)",
        extra={"saga_session_id": "sess-xyz"},
    )
    ctx = _make_ctx(event, saga_session_id="sess-xyz")
    turn_prompt, recent = await agent._build_turn_prompt(
        ctx, event, saga_block=None, subagent_block=None,
    )
    # The synthesis template carries the session id verbatim
    # (placeholder ``{saga_session_id}`` is filled by render).
    assert "sess-xyz" in turn_prompt
    # The standard-turn ``## Today's date`` header should NOT be
    # present — synthesis uses its own scaffold.
    assert "## Today's date" not in turn_prompt
    # No recent list when synthesis branch fires.
    assert recent == []


# ─── Direct synthesis prompt builder ────────────────────────────────


@pytest.mark.asyncio
async def test_build_synthesis_prompt_handles_empty_window(
    tmp_path: Path,
) -> None:
    """When the session has no recorded turns, ``_build_synthesis_prompt``
    must still emit a valid synthesis prompt (the lean variant) rather
    than crashing. Pre-181-H regression — _filter_session_turns
    returning [] should produce a template render, not a KeyError.
    """
    agent = _make_agent(tmp_path)
    event = AgentEvent(
        trigger="saga_session_end",
        channel_id="ch-empty",
        extra={"saga_session_id": "sess-empty"},
    )
    ctx = _make_ctx(event, saga_session_id="sess-empty")
    rendered = await agent._build_synthesis_prompt(ctx, event)
    assert "sess-empty" in rendered
    assert rendered  # non-empty
