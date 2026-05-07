"""End-to-end: subagent notifications flow from SDK stream → inbox → next
turn's prompt (SPEC §4.4)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
)

from mimir.agent import Agent
from mimir.config import Config
from mimir.event_logger import init_logger
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, make_process_session_id
from mimir.session_manager import SessionManager
from mimir.subagent_inbox import SubagentInbox
from mimir.turn_logger import TurnLogger

from ._fake_saga import FakeSaga


def _build_agent(tmp_path: Path) -> tuple[Agent, SubagentInbox]:
    cfg = replace(Config.from_env(), home=tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(cfg.events_log, make_process_session_id())
    turn_logger = TurnLogger(cfg.turns_log)
    buf = MessageBuffer(history_path=cfg.home / "messages" / "chat_history.jsonl")
    indexes = IndexGenerator(cfg.home)
    sessions = SessionManager(idle_minutes=60)
    inbox = SubagentInbox()
    agent = Agent(
        cfg,
        turn_logger,
        buf,
        indexes,
        indexer=None,
        saga_client=FakeSaga(),
        session_manager=sessions,
        scheduler=None,
        subagent_inbox=inbox,
    )
    return agent, inbox


def _started(task_id: str, description: str) -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description=description,
        uuid=f"u-{task_id}",
        session_id="s",
    )


def _notification(task_id: str, status: str, summary: str, output_file: str) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file=output_file,
        summary=summary,
        uuid=f"u-{task_id}",
        session_id="s",
    )


@pytest.mark.asyncio
async def test_task_notification_pushed_to_inbox(tmp_path: Path):
    agent, inbox = _build_agent(tmp_path)

    async def fake_query(*, prompt, options, session_id="default", transport=None):
        yield _started("t1", "research X")
        yield _notification("t1", "completed", "found Y", "/tmp/out.md")
        yield AssistantMessage(content=[TextBlock(text="kicked off")], model="claude")

    with patch("mimir.agent.query", new=fake_query):
        await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="go", author="alice")
        )

    pending = inbox.peek("c1")
    assert len(pending) == 1
    assert pending[0].task_id == "t1"
    assert pending[0].status == "completed"
    assert pending[0].summary == "found Y"
    assert pending[0].output_file == "/tmp/out.md"
    assert pending[0].description == "research X"


@pytest.mark.asyncio
async def test_inbox_drains_into_next_turn_prompt(tmp_path: Path):
    """Notification arrives in turn 1; turn 2's prompt must include the
    'Subagent updates' section."""
    agent, inbox = _build_agent(tmp_path)

    # Pre-load a notification as if turn 1 already happened.
    from mimir.subagent_inbox import SubagentResult
    await inbox.push(
        "c1",
        SubagentResult(
            task_id="t-prev",
            status="completed",
            summary="prior result",
            output_file="/tmp/prev.md",
            description="prior climb",
        ),
    )

    captured: dict = {}

    async def capturing_query(*, prompt, options, session_id="default", transport=None):
        captured["prompt"] = prompt
        yield AssistantMessage(content=[TextBlock(text="ok")], model="claude")

    with patch("mimir.agent.query", new=capturing_query):
        await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="next", author="alice")
        )

    assert "## Subagent updates" in captured["prompt"]
    assert "t-prev" in captured["prompt"]
    assert "prior result" in captured["prompt"]
    # Drain consumed the notification.
    assert inbox.peek("c1") == []


@pytest.mark.asyncio
async def test_dispatch_walk_handles_interleaved_task_streams(tmp_path: Path):
    """CR#13 regression: consolidated dispatch walk must preserve the
    task_descriptions invariant when two subagents are running
    concurrently and their TaskStarted/TaskNotification messages are
    interleaved in the SDK stream.

    Order in the test:
      Started(A) → Started(B) → Notification(A) → Notification(B)

    Both inbox pushes must carry the right `description` from
    task_descriptions, demonstrating that the dispatch loop's
    mid-walk lookup works for any task_id whose Started came
    earlier in the stream — including cross-pollination across
    interleaved tasks."""
    agent, inbox = _build_agent(tmp_path)

    async def fake_query(*, prompt, options, session_id="default", transport=None):
        yield _started("ta", "research A")
        yield _started("tb", "research B")
        yield _notification("ta", "completed", "result A", "/tmp/a.md")
        yield _notification("tb", "completed", "result B", "/tmp/b.md")
        yield AssistantMessage(content=[TextBlock(text="kicked off")], model="claude")

    with patch("mimir.agent.query", new=fake_query):
        await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="go", author="alice")
        )

    pending = inbox.peek("c1")
    assert len(pending) == 2
    by_id = {p.task_id: p for p in pending}
    assert by_id["ta"].description == "research A"
    assert by_id["tb"].description == "research B"
    assert by_id["ta"].summary == "result A"
    assert by_id["tb"].summary == "result B"


@pytest.mark.asyncio
async def test_failed_subagent_still_pushed(tmp_path: Path):
    agent, inbox = _build_agent(tmp_path)

    async def fake_query(*, prompt, options, session_id="default", transport=None):
        yield _started("t1", "explore")
        yield _notification("t1", "failed", "kaboom", "/tmp/err.md")
        yield AssistantMessage(content=[TextBlock(text="noted")], model="claude")

    with patch("mimir.agent.query", new=fake_query):
        await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="x", author="z")
        )

    pending = inbox.peek("c1")
    assert pending[0].status == "failed"
