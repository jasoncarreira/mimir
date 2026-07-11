"""Dedicated coverage for mimir/tools/registry.py (chainlink #247, slice 4/5).

Covers: DI setters, send_message, react, fetch_channel_history, scheduler
tools (add_schedule, remove_schedule, reload_pollers), and commitment tools
(commitment_complete, commitment_snooze, commitment_dismiss, commitment_list).

Tools already covered in other files and NOT re-tested here:
  - test_tool_wiring.py: _channel_from_config_or_state precedence, spawn*,
    InjectedToolArg schema, set_commitments_store, set_spawn_config
  - test_spawn_caps.py: spawn depth/rate/concurrency/child-env
  - test_channel_id_contextvar.py: ContextVar isolation
  - test_list_schedules.py: list_schedules fully
  - test_saga_ops_wiring.py: saga_ops quartet
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.commitments.models import CommitmentStatus
from mimir.models import TurnContext, TurnInteractivity
from mimir.scheduler import SchedulerJob
from mimir.tools.registry import (
    _STATE,
    add_schedule,
    commitment_complete,
    commitment_dismiss,
    commitment_list,
    commitment_snooze,
    fetch_channel_history,
    react,
    reload_pollers,
    remove_schedule,
    reset_current_channel_id,
    reset_current_turn_interactive,
    send_message,
    set_channel_registry,
    set_commitments_store,
    set_current_channel_id,
    set_current_turn_interactive,
    set_dispatcher,
    set_poller_overrides,
    set_schedule_priority,
    set_scheduler,
)


# ────────────────────────────────────────────────────────────────────
# Fixture: reset _STATE after each test
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    snapshot = dict(_STATE)
    yield
    _STATE.clear()
    _STATE.update(snapshot)


# ────────────────────────────────────────────────────────────────────
# Stubs
# ────────────────────────────────────────────────────────────────────


class _SendResult:
    def __init__(self, message_id: str, sent: bool = True) -> None:
        self.message_id = message_id
        self.sent = sent
        self.error: str | None = None


class _StubBridge:
    """Minimal bridge recording calls and optionally raising."""

    def __init__(self) -> None:
        self.send_calls: list[dict] = []
        self.send_finals: list[bool] = []
        self.react_calls: list[dict] = []
        self.history_calls: list[dict] = []
        self.raise_on: str | None = None
        self._history: list[dict] = []
        # Bridge.react return value. None (default) mirrors stubs that
        # don't honor the bool contract; set False to exercise the
        # declined-reaction path.
        self.react_returns: bool | None = None
        # Control SendResult.sent — set False to exercise the soft
        # delivery-failure path (bridge returns sent=False, no raise).
        self.send_sent: bool = True

    async def send(self, cid: str, text: str, *, final: bool = True):
        if self.raise_on == "send":
            raise RuntimeError("send boom")
        self.send_calls.append({"cid": cid, "text": text})
        self.send_finals.append(final)
        return _SendResult(message_id="msg-42", sent=self.send_sent)

    async def react(self, cid: str, message_id: str | None, emoji: str):
        if self.raise_on == "react":
            raise RuntimeError("react boom")
        self.react_calls.append({"cid": cid, "message_id": message_id, "emoji": emoji})
        return self.react_returns

    async def fetch_history(self, cid: str, limit: int):
        if self.raise_on == "fetch_history":
            raise RuntimeError("history boom")
        self.history_calls.append({"cid": cid, "limit": limit})
        return self._history


class _StubBridgeNoHistory:
    """Bridge without fetch_history to verify graceful missing-attr error."""

    async def send(self, cid: str, text: str, *, final: bool = True):
        return _SendResult(message_id="msg-1")

    async def react(self, cid: str, message_id: str | None, emoji: str):
        pass


class _StubRegistry:
    """Minimal channel registry with .find()."""

    def __init__(self, bridge=None, *, channel_id: str = "chan-1") -> None:
        self._bridge = bridge
        self._channel_id = channel_id
        self.find_calls: list[str] = []

    def find(self, channel_id: str):
        self.find_calls.append(channel_id)
        if channel_id == self._channel_id:
            return self._bridge
        return None


class _StubScheduler:
    """Minimal scheduler stub for add_schedule / remove_schedule / reload_pollers."""

    def __init__(self, *, home: Path | None = None) -> None:
        self.add_calls: list[dict] = []
        self.remove_calls: list[str] = []
        self.reload_count: int = 0
        self.raise_on: str | None = None
        self._removed: bool = True  # default: job found and removed
        self._jobs: list[SchedulerJob] = []  # for list_jobs / set_schedule_priority
        self._home = home

    async def add_job(self, job: SchedulerJob) -> SchedulerJob:
        if self.raise_on == "add":
            raise RuntimeError("add boom")
        self.add_calls.append(
            {
                "name": job.name,
                "cron": job.cron,
                "prompt": job.prompt,
                "prompt_file": job.prompt_file,
                "channel_id": job.channel_id,
                "priority": job.priority,
            }
        )
        # Mirror the real add-or-replace so set_schedule_priority round-trips.
        self._jobs = [j for j in self._jobs if j.name != job.name] + [job]
        return job

    async def list_jobs(self) -> list[SchedulerJob]:
        if self.raise_on == "list":
            raise RuntimeError("list boom")
        return list(self._jobs)

    async def remove_job(self, name: str) -> bool:
        if self.raise_on == "remove":
            raise RuntimeError("remove boom")
        self.remove_calls.append(name)
        return self._removed

    async def reload_pollers(self) -> dict:
        if self.raise_on == "reload":
            raise RuntimeError("reload boom")
        self.reload_count += 1
        return {"total": 3, "registered": 2}


class _StubCommitmentsStore:
    """Minimal commitments store stub."""

    def __init__(self) -> None:
        self.complete_calls: list[dict] = []
        self.snooze_calls: list[dict] = []
        self.dismiss_calls: list[dict] = []
        self._items: list = []
        self.raise_on: str | None = None
        # The real store returns a bool (False when _can_apply rejects: unknown
        # id / already terminal). Set reject=True to exercise that path (#485).
        self.reject: bool = False

    async def complete(self, commitment_id: str, *, message_id=None):
        if self.raise_on == "complete":
            raise RuntimeError("complete boom")
        if self.reject:
            return False
        self.complete_calls.append({"id": commitment_id, "message_id": message_id})
        return True

    async def snooze(self, commitment_id: str, *, until_unix: float, reason=None):
        if self.raise_on == "snooze":
            raise RuntimeError("snooze boom")
        if self.reject:
            return False
        self.snooze_calls.append({"id": commitment_id, "until_unix": until_unix})
        return True

    async def dismiss(self, commitment_id: str, *, reason=None):
        if self.raise_on == "dismiss":
            raise RuntimeError("dismiss boom")
        if self.reject:
            return False
        self.dismiss_calls.append({"id": commitment_id, "reason": reason})
        return True

    def list(self):
        if self.raise_on == "list":
            raise RuntimeError("list boom")
        return list(self._items)


@dataclass
class _FakeCommitment:
    id: str
    text: str
    status: str
    channel_id: Optional[str] = None
    due_window_hint: Optional[str] = None
    due_window_end_unix: Optional[float] = None


# ────────────────────────────────────────────────────────────────────
# DI setters
# ────────────────────────────────────────────────────────────────────


class TestDISetters:
    def test_set_channel_registry(self) -> None:
        reg = _StubRegistry()
        set_channel_registry(reg)
        assert _STATE["channel_registry"] is reg

    def test_set_dispatcher(self) -> None:
        fake_dispatcher = object()
        set_dispatcher(fake_dispatcher)
        assert _STATE["dispatcher"] is fake_dispatcher

    def test_set_scheduler(self) -> None:
        sched = _StubScheduler()
        set_scheduler(sched)
        assert _STATE["scheduler"] is sched


# ────────────────────────────────────────────────────────────────────
# send_message
# ────────────────────────────────────────────────────────────────────


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_no_registry_returns_error(self) -> None:
        _STATE["channel_registry"] = None
        out = await send_message.ainvoke(
            {"text": "hello", "channel_id": "discord-123"}
        )
        assert "no channel registry configured" in out

    @pytest.mark.asyncio
    async def test_empty_text_returns_error(self, tmp_path) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": ""})
            assert "send_message rejected" in out
            assert "empty message" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_whitespace_text_returns_tool_error_and_event(self, tmp_path) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke(
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "send_message",
                    "args": {"text": "   ", "channel_id": "chan-1"},
                }
            )
            assert out.status == "error"
            assert "send_message rejected" in out.content
            assert "empty message" in out.content
        finally:
            reset_current_channel_id(tok)
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == "chan-1"
        assert event["reason"] == "empty_message"

    @pytest.mark.asyncio
    async def test_no_channel_id_returns_tool_error_and_event(self, tmp_path) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        # no contextvar set, no explicit channel_id
        out = await send_message.ainvoke(
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "send_message",
                "args": {"text": "hello"},
            }
        )
        assert out.status == "error"
        assert "send_message rejected" in out.content
        assert "not a deliverable channel" in out.content
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] is None
        assert event["reason"] == "not_deliverable_channel"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_id", ["poller:gmail-inbox", "scheduler:daily", "system", ""])
    async def test_non_deliverable_channel_returns_tool_error_and_event(
        self, tmp_path, channel_id,
    ) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id=channel_id))
        out = await send_message.ainvoke(
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "send_message",
                "args": {"text": "hello", "channel_id": channel_id},
            }
        )
        assert out.status == "error"
        assert "send_message rejected" in out.content
        assert "not a deliverable channel" in out.content
        assert bridge.send_calls == []
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == channel_id
        assert event["reason"] == "not_deliverable_channel"

    @pytest.mark.asyncio
    async def test_no_bridge_returns_error(self) -> None:
        # registry finds no bridge for this channel
        set_channel_registry(_StubRegistry(bridge=None, channel_id="chan-1"))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "hello", "channel_id": "chan-1"})
            assert "no bridge" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_bridge_raises_returns_error(self) -> None:
        bridge = _StubBridge()
        bridge.raise_on = "send"
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "hello", "channel_id": "chan-1"})
            assert "send_message failed" in out
            assert "boom" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "Hello world", "channel_id": "chan-1"})
            assert "send_message ok:" in out
            assert len(bridge.send_calls) == 1
            assert bridge.send_calls[0]["cid"] == "chan-1"
            assert bridge.send_calls[0]["text"] == "Hello world"
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_explicit_channel_id_used(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="explicit-chan"))
        out = await send_message.ainvoke(
            {"text": "Hi", "channel_id": "explicit-chan"}
        )
        assert "send_message ok:" in out
        assert bridge.send_calls[0]["cid"] == "explicit-chan"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_id", ["discord-123", "dm-discord-456", "web-jason"])
    async def test_bridge_channel_ids_are_unaffected(self, channel_id) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id=channel_id))
        out = await send_message.ainvoke(
            {"text": "Hi", "channel_id": channel_id}
        )
        assert out == f"send_message ok: channel={channel_id} message_id=msg-42"
        assert bridge.send_calls == [{"cid": channel_id, "text": "Hi"}]


# ────────────────────────────────────────────────────────────────────
# react
# ────────────────────────────────────────────────────────────────────


class TestReact:
    @pytest.mark.asyncio
    async def test_no_registry_returns_error(self) -> None:
        _STATE["channel_registry"] = None
        out = await react.ainvoke({"emoji": "👍"})
        assert "no channel registry configured" in out

    @pytest.mark.asyncio
    async def test_no_channel_id_returns_error(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        out = await react.ainvoke({"emoji": "👍"})
        assert "no channel_id" in out

    @pytest.mark.asyncio
    async def test_no_bridge_returns_error(self) -> None:
        set_channel_registry(_StubRegistry(bridge=None, channel_id="chan-1"))
        tok = set_current_channel_id("chan-1")
        try:
            out = await react.ainvoke({"emoji": "❤️"})
            assert "no bridge" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_bridge_raises_surfaces_error(self) -> None:
        bridge = _StubBridge()
        bridge.raise_on = "react"
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            # Explicit message_id so we reach the bridge (the None path
            # short-circuits on resolution before bridge.react).
            out = await react.ainvoke({"emoji": "👍", "message_id": "m-1"})
            assert "react failed" in out
            assert "boom" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await react.ainvoke({"emoji": "🎉", "message_id": "msg-99"})
            assert "react ok:" in out
            assert "message_id=msg-99" in out
            assert len(bridge.react_calls) == 1
            assert bridge.react_calls[0]["emoji"] == "🎉"
            assert bridge.react_calls[0]["message_id"] == "msg-99"
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_default_resolves_recent_message_from_buffer(
        self, tmp_path,
    ) -> None:
        """message_id omitted → resolve the most recent id-bearing
        message on the channel (kind-agnostic) from the history buffer."""
        from mimir.history import (
            MessageBuffer, get_global_buffer, set_global_buffer,
        )
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        buf = MessageBuffer(history_path=tmp_path / "h.jsonl")
        prev = get_global_buffer()
        set_global_buffer(buf)
        tok = set_current_channel_id("chan-1")
        try:
            # An inbound (user) message is a valid default target — the
            # common "acknowledge the last thing said" case.
            await buf.append(buf.make_message(
                channel_id="chan-1", kind="user_message",
                content="hi", msg_id="m-7",
            ))
            out = await react.ainvoke({"emoji": "👍"})
            assert "react ok:" in out
            assert "message_id=m-7" in out
            assert bridge.react_calls[0]["message_id"] == "m-7"
        finally:
            reset_current_channel_id(tok)
            set_global_buffer(prev)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_default_unresolvable_returns_error(self, tmp_path) -> None:
        """No message_id and nothing in the buffer to default to → a
        clear error, and the bridge is never called."""
        from mimir.history import (
            MessageBuffer, get_global_buffer, set_global_buffer,
        )
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        buf = MessageBuffer(history_path=tmp_path / "h.jsonl")  # empty
        prev = get_global_buffer()
        set_global_buffer(buf)
        tok = set_current_channel_id("chan-1")
        try:
            out = await react.ainvoke({"emoji": "👍"})
            assert "react failed" in out
            assert "no message_id" in out
            assert bridge.react_calls == []
        finally:
            reset_current_channel_id(tok)
            set_global_buffer(prev)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_bridge_declines_returns_failed(self) -> None:
        """A False bridge return (declined reaction) is surfaced as a
        failure rather than reported as ok."""
        bridge = _StubBridge()
        bridge.react_returns = False
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await react.ainvoke({"emoji": "👍", "message_id": "m-9"})
            assert "react failed" in out
            assert "declined" in out
        finally:
            reset_current_channel_id(tok)


# ────────────────────────────────────────────────────────────────────
# fetch_channel_history
# ────────────────────────────────────────────────────────────────────


class TestFetchChannelHistory:
    @pytest.mark.asyncio
    async def test_no_registry_returns_error(self) -> None:
        _STATE["channel_registry"] = None
        out = await fetch_channel_history.ainvoke({})
        assert "no channel registry" in out

    @pytest.mark.asyncio
    async def test_no_channel_id_returns_error(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        out = await fetch_channel_history.ainvoke({})
        assert "no channel_id" in out

    @pytest.mark.asyncio
    async def test_bridge_missing_fetch_history_attr(self) -> None:
        no_hist_bridge = _StubBridgeNoHistory()
        set_channel_registry(_StubRegistry(no_hist_bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await fetch_channel_history.ainvoke({})
            assert "doesn't support history" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_limit_clamped_zero_to_one(self) -> None:
        bridge = _StubBridge()
        bridge._history = [{"id": "m1", "text": "hi"}]
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            await fetch_channel_history.ainvoke({"limit": 0})
            assert bridge.history_calls[0]["limit"] == 1
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_limit_clamped_large_to_100(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            await fetch_channel_history.ainvoke({"limit": 200})
            assert bridge.history_calls[0]["limit"] == 100
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_bridge_raises_surfaces_error(self) -> None:
        bridge = _StubBridge()
        bridge.raise_on = "fetch_history"
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await fetch_channel_history.ainvoke({})
            assert "fetch_channel_history failed" in out
            assert "boom" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_happy_path_returns_json(self) -> None:
        bridge = _StubBridge()
        bridge._history = [
            {"id": "m1", "author": "alice", "text": "hello"},
            {"id": "m2", "author": "bob", "text": "world"},
        ]
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await fetch_channel_history.ainvoke({"limit": 10})
            parsed = json.loads(out)
            assert len(parsed) == 2
            assert parsed[0]["id"] == "m1"
            assert bridge.history_calls[0]["limit"] == 10
            assert bridge.history_calls[0]["cid"] == "chan-1"
        finally:
            reset_current_channel_id(tok)


# ────────────────────────────────────────────────────────────────────
# Scheduler tools
# ────────────────────────────────────────────────────────────────────


class TestAddSchedule:
    @pytest.mark.asyncio
    async def test_no_scheduler_returns_error(self) -> None:
        _STATE["scheduler"] = None
        out = await add_schedule.ainvoke(
            {"name": "tick", "cron": "0 * * * *", "prompt": "run"}
        )
        assert "add_schedule failed: no scheduler configured" in out

    @pytest.mark.asyncio
    async def test_scheduler_raises_returns_error(self) -> None:
        sched = _StubScheduler()
        sched.raise_on = "add"
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "tick", "cron": "0 * * * *", "prompt": "run"}
        )
        assert "add_schedule failed" in out
        assert "boom" in out

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self) -> None:
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {
                "name": "morning",
                "cron": "0 9 * * *",
                "prompt": "Good morning",
                "channel_id": "chan-1",
            }
        )
        assert "add_schedule ok:" in out
        assert "name=morning" in out
        assert "cron=0 9 * * *" in out
        assert len(sched.add_calls) == 1
        call = sched.add_calls[0]
        assert call["name"] == "morning"
        assert call["cron"] == "0 9 * * *"
        assert call["prompt"] == "Good morning"
        assert call["channel_id"] == "chan-1"
        # #656: default priority is normal when not specified.
        assert call["priority"] == "normal"

    @pytest.mark.asyncio
    async def test_priority_is_persisted(self) -> None:
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "watch", "cron": "*/5 * * * *", "prompt": "watch", "priority": "high"}
        )
        assert "add_schedule ok:" in out
        assert "priority=high" in out
        assert sched.add_calls[0]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_invalid_priority_rejected_without_adding(self) -> None:
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "watch", "cron": "*/5 * * * *", "prompt": "watch", "priority": "urgent"}
        )
        assert "add_schedule failed: invalid priority" in out
        assert "urgent" in out
        assert sched.add_calls == []  # rejected before persisting

    @pytest.mark.asyncio
    async def test_adds_with_prompt_file(self) -> None:
        # chainlink #666: prompt_file persists as prompt_file (no fabricated
        # inline prompt) so a tick can match the canonical bundled shape.
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {
                "name": "memory-hygiene",
                "cron": "0 8 * * 2",
                "prompt_file": "memory-hygiene.md",
            }
        )
        assert "add_schedule ok:" in out
        assert "prompt_file=memory-hygiene.md" in out
        call = sched.add_calls[0]
        assert call["prompt_file"] == "memory-hygiene.md"
        assert not call["prompt"]  # inline prompt not fabricated

    @pytest.mark.asyncio
    async def test_both_prompt_and_prompt_file_rejected(self) -> None:
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {
                "name": "x",
                "cron": "0 * * * *",
                "prompt": "run",
                "prompt_file": "x.md",
            }
        )
        assert "exactly one of prompt / prompt_file" in out
        assert "both" in out
        assert sched.add_calls == []

    @pytest.mark.asyncio
    async def test_neither_prompt_nor_prompt_file_rejected(self) -> None:
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke({"name": "x", "cron": "0 * * * *"})
        assert "exactly one of prompt / prompt_file" in out
        assert "neither" in out
        assert sched.add_calls == []

    @pytest.mark.asyncio
    async def test_prompt_file_missing_under_home_rejected(self, tmp_path) -> None:
        # When the scheduler knows the home, a prompt_file that doesn't exist
        # under <home>/prompts/ is rejected up front (avoids a tick that fires
        # an empty prompt via fire-time fallback).
        (tmp_path / "prompts").mkdir()
        sched = _StubScheduler()
        sched._home = tmp_path
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "x", "cron": "0 8 * * 2", "prompt_file": "missing.md"}
        )
        assert "add_schedule failed: prompt_file" in out
        assert "missing.md" in out
        assert sched.add_calls == []

    @pytest.mark.asyncio
    async def test_prompt_file_present_under_home_ok(self, tmp_path) -> None:
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "memory-hygiene.md").write_text("scan\n", encoding="utf-8")
        sched = _StubScheduler()
        sched._home = tmp_path
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "memory-hygiene", "cron": "0 8 * * 2", "prompt_file": "memory-hygiene.md"}
        )
        assert "add_schedule ok:" in out
        assert sched.add_calls[0]["prompt_file"] == "memory-hygiene.md"

    @pytest.mark.asyncio
    async def test_prompt_file_traversal_rejected(self, tmp_path) -> None:
        # mimir-carreira #865 review: validate with the fire-time resolver so a
        # path that escapes <home>/prompts can't persist a tick that then fires
        # an empty prompt. ``../state/x.md`` resolves outside prompts → rejected.
        (tmp_path / "prompts").mkdir()
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "x.md").write_text("escaped\n", encoding="utf-8")
        sched = _StubScheduler()
        sched._home = tmp_path
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "x", "cron": "0 8 * * 2", "prompt_file": "../state/x.md"}
        )
        assert "add_schedule failed: prompt_file" in out
        assert sched.add_calls == []

    @pytest.mark.asyncio
    async def test_prompt_file_absolute_path_rejected(self, tmp_path) -> None:
        (tmp_path / "prompts").mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("nope\n", encoding="utf-8")
        sched = _StubScheduler()
        sched._home = tmp_path
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "x", "cron": "0 8 * * 2", "prompt_file": str(outside)}
        )
        assert "add_schedule failed: prompt_file" in out
        assert sched.add_calls == []

    @pytest.mark.asyncio
    async def test_prompt_file_symlink_rejected(self, tmp_path) -> None:
        (tmp_path / "prompts").mkdir()
        real = tmp_path / "real.md"
        real.write_text("real\n", encoding="utf-8")
        (tmp_path / "prompts" / "link.md").symlink_to(real)
        sched = _StubScheduler()
        sched._home = tmp_path
        _STATE["scheduler"] = sched
        out = await add_schedule.ainvoke(
            {"name": "x", "cron": "0 8 * * 2", "prompt_file": "link.md"}
        )
        assert "add_schedule failed: prompt_file" in out
        assert sched.add_calls == []


class TestSetSchedulePriority:
    @pytest.mark.asyncio
    async def test_no_scheduler_returns_error(self) -> None:
        _STATE["scheduler"] = None
        out = await set_schedule_priority.ainvoke({"name": "x", "priority": "high"})
        assert "set_schedule_priority failed: no scheduler configured" in out

    @pytest.mark.asyncio
    async def test_invalid_priority_rejected(self) -> None:
        _STATE["scheduler"] = _StubScheduler()
        out = await set_schedule_priority.ainvoke({"name": "x", "priority": "URGENT"})
        assert "invalid priority" in out

    @pytest.mark.asyncio
    async def test_no_such_job(self) -> None:
        _STATE["scheduler"] = _StubScheduler()
        out = await set_schedule_priority.ainvoke({"name": "ghost", "priority": "high"})
        assert "no job named" in out and "ghost" in out

    @pytest.mark.asyncio
    async def test_updates_priority_preserving_prompt_file(self) -> None:
        sched = _StubScheduler()
        sched._jobs = [
            SchedulerJob(name="daily", prompt_file="daily.md", cron="0 0 * * *",
                         channel_id=None, priority="low"),
        ]
        _STATE["scheduler"] = sched
        out = await set_schedule_priority.ainvoke({"name": "daily", "priority": "high"})
        assert "set_schedule_priority ok:" in out and "priority=high" in out
        # Re-added with high priority, prompt_file preserved (not clobbered).
        call = sched.add_calls[-1]
        assert call["name"] == "daily"
        assert call["priority"] == "high"
        assert call["prompt_file"] == "daily.md"
        assert not call["prompt"]  # prompt not fabricated; prompt_file preserved

    @pytest.mark.asyncio
    async def test_refuses_callable_job(self) -> None:
        sched = _StubScheduler()
        sched._jobs = [
            SchedulerJob(name="saga", callable_name="saga-consolidate",
                         cron="0 4 * * *", channel_id=None),
        ]
        _STATE["scheduler"] = sched
        out = await set_schedule_priority.ainvoke({"name": "saga", "priority": "high"})
        assert "callable job" in out
        assert sched.add_calls == []  # not persisted


class TestRemoveSchedule:
    @pytest.mark.asyncio
    async def test_no_scheduler_returns_error(self) -> None:
        _STATE["scheduler"] = None
        out = await remove_schedule.ainvoke({"name": "tick"})
        assert "remove_schedule failed: no scheduler configured" in out

    @pytest.mark.asyncio
    async def test_scheduler_raises_returns_error(self) -> None:
        sched = _StubScheduler()
        sched.raise_on = "remove"
        _STATE["scheduler"] = sched
        out = await remove_schedule.ainvoke({"name": "tick"})
        assert "remove_schedule failed" in out
        assert "boom" in out

    @pytest.mark.asyncio
    async def test_removed_false_returns_not_found(self) -> None:
        sched = _StubScheduler()
        sched._removed = False
        _STATE["scheduler"] = sched
        out = await remove_schedule.ainvoke({"name": "ghost-job"})
        assert "no job named" in out
        assert "ghost-job" in out

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self) -> None:
        sched = _StubScheduler()
        sched._removed = True
        _STATE["scheduler"] = sched
        out = await remove_schedule.ainvoke({"name": "morning"})
        assert "remove_schedule ok:" in out
        assert sched.remove_calls == ["morning"]


class TestReloadPollers:
    @pytest.mark.asyncio
    async def test_no_scheduler_returns_error(self) -> None:
        _STATE["scheduler"] = None
        out = await reload_pollers.ainvoke({})
        assert "reload_pollers failed: no scheduler configured" in out

    @pytest.mark.asyncio
    async def test_scheduler_raises_returns_error(self) -> None:
        sched = _StubScheduler()
        sched.raise_on = "reload"
        _STATE["scheduler"] = sched
        out = await reload_pollers.ainvoke({})
        assert "reload_pollers failed" in out
        assert "boom" in out

    @pytest.mark.asyncio
    async def test_happy_path_returns_counts(self) -> None:
        sched = _StubScheduler()
        _STATE["scheduler"] = sched
        out = await reload_pollers.ainvoke({})
        assert "reload_pollers ok:" in out
        assert "total=3" in out
        assert "fresh=2" in out
        assert sched.reload_count == 1


class TestSetPollerOverrides:
    @pytest.mark.asyncio
    async def test_no_scheduler_returns_error(self) -> None:
        _STATE["scheduler"] = None
        out = await set_poller_overrides.ainvoke(
            {"poller_name": "gmail-inbox", "overrides": {"pass_env": ["GOG_ACCOUNT"]}},
        )
        assert "set_poller_overrides failed: no scheduler configured" in out

    @pytest.mark.asyncio
    async def test_writes_validated_home_file(self, tmp_path: Path) -> None:
        _STATE["scheduler"] = _StubScheduler(home=tmp_path)
        out = await set_poller_overrides.ainvoke(
            {
                "poller_name": "gmail-inbox",
                "overrides": {"pass_env": ["GOG_ACCOUNT"], "batch_size": 3},
            },
        )
        assert "set_poller_overrides ok: updated gmail-inbox" in out
        body = (tmp_path / "pollers-overrides.yaml").read_text(encoding="utf-8")
        assert "gmail-inbox:" in body
        assert "pass_env:" in body
        assert "GOG_ACCOUNT" in body

    @pytest.mark.asyncio
    async def test_rejects_unknown_override_field_without_writing(
        self,
        tmp_path: Path,
    ) -> None:
        _STATE["scheduler"] = _StubScheduler(home=tmp_path)
        out = await set_poller_overrides.ainvoke(
            {
                "poller_name": "gmail-inbox",
                "overrides": {"command": "rm -rf /"},
            },
        )
        assert "set_poller_overrides failed:" in out
        assert "poller_overrides_unknown_field" in out
        assert not (tmp_path / "pollers-overrides.yaml").exists()


# ────────────────────────────────────────────────────────────────────
# Commitment tools
# ────────────────────────────────────────────────────────────────────


class TestCommitmentComplete:
    @pytest.mark.asyncio
    async def test_no_store_returns_error(self) -> None:
        _STATE["commitments_store"] = None
        out = await commitment_complete.ainvoke({"commitment_id": "c-1"})
        assert "commitment_complete failed: no commitments store" in out

    @pytest.mark.asyncio
    async def test_store_raises_returns_error(self) -> None:
        store = _StubCommitmentsStore()
        store.raise_on = "complete"
        set_commitments_store(store)
        out = await commitment_complete.ainvoke({"commitment_id": "c-1"})
        assert "commitment_complete failed" in out
        assert "boom" in out

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self) -> None:
        store = _StubCommitmentsStore()
        set_commitments_store(store)
        out = await commitment_complete.ainvoke({"commitment_id": "c-42"})
        assert "commitment_complete ok:" in out
        assert "id=c-42" in out

    @pytest.mark.asyncio
    async def test_rejected_transition_returns_failed(self) -> None:
        # Store returns False (unknown id / already terminal) → must NOT report ok (#485).
        store = _StubCommitmentsStore()
        store.reject = True
        set_commitments_store(store)
        out = await commitment_complete.ainvoke({"commitment_id": "c-42"})
        assert "commitment_complete failed" in out
        assert "ok" not in out


class TestCommitmentSnooze:
    @pytest.mark.asyncio
    async def test_no_store_returns_error(self) -> None:
        _STATE["commitments_store"] = None
        out = await commitment_snooze.ainvoke(
            {"commitment_id": "c-1", "until_iso": "2030-01-01T00:00:00Z"}
        )
        assert "commitment_snooze failed: no commitments store" in out

    @pytest.mark.asyncio
    async def test_invalid_iso_returns_error(self) -> None:
        store = _StubCommitmentsStore()
        set_commitments_store(store)
        out = await commitment_snooze.ainvoke(
            {"commitment_id": "c-1", "until_iso": "not-a-date"}
        )
        assert "commitment_snooze failed" in out

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self) -> None:
        store = _StubCommitmentsStore()
        set_commitments_store(store)
        out = await commitment_snooze.ainvoke(
            {"commitment_id": "c-7", "until_iso": "2030-06-01T10:00:00Z"}
        )
        assert "commitment_snooze ok:" in out
        assert "id=c-7" in out
        assert "until=2030-06-01T10:00:00Z" in out

    @pytest.mark.asyncio
    async def test_rejected_transition_returns_failed(self) -> None:
        store = _StubCommitmentsStore()
        store.reject = True
        set_commitments_store(store)
        out = await commitment_snooze.ainvoke(
            {"commitment_id": "c-7", "until_iso": "2030-06-01T10:00:00Z"}
        )
        assert "commitment_snooze failed" in out
        assert "ok" not in out

    @pytest.mark.asyncio
    async def test_naive_iso_interpreted_as_utc(self) -> None:
        """#503: a naive ISO (no Z/offset) must be treated as UTC, not the
        server's local tz — otherwise the snooze lands hours off on a non-UTC
        host. The recorded until_unix must equal the UTC interpretation
        regardless of the host timezone."""
        from datetime import datetime, timezone

        store = _StubCommitmentsStore()
        set_commitments_store(store)
        out = await commitment_snooze.ainvoke(
            {"commitment_id": "c-9", "until_iso": "2030-06-01T10:00:00"}  # naive
        )
        assert "commitment_snooze ok:" in out
        expected = datetime(2030, 6, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        assert store.snooze_calls[-1]["until_unix"] == expected
        # And it equals the explicit-Z form (host-tz-independent).
        z_form = datetime.fromisoformat("2030-06-01T10:00:00+00:00").timestamp()
        assert store.snooze_calls[-1]["until_unix"] == z_form


class TestCommitmentDismiss:
    @pytest.mark.asyncio
    async def test_no_store_returns_error(self) -> None:
        _STATE["commitments_store"] = None
        out = await commitment_dismiss.ainvoke({"commitment_id": "c-1"})
        assert "commitment_dismiss failed: no commitments store" in out

    @pytest.mark.asyncio
    async def test_happy_path_with_reason(self) -> None:
        store = _StubCommitmentsStore()
        set_commitments_store(store)
        out = await commitment_dismiss.ainvoke(
            {"commitment_id": "c-5", "reason": "no longer relevant"}
        )
        assert "commitment_dismiss ok:" in out
        assert store.dismiss_calls[0]["id"] == "c-5"
        assert store.dismiss_calls[0]["reason"] == "no longer relevant"

    @pytest.mark.asyncio
    async def test_rejected_transition_returns_failed(self) -> None:
        store = _StubCommitmentsStore()
        store.reject = True
        set_commitments_store(store)
        out = await commitment_dismiss.ainvoke({"commitment_id": "c-5"})
        assert "commitment_dismiss failed" in out
        assert "ok" not in out


class TestCommitmentList:
    @pytest.mark.asyncio
    async def test_no_store_returns_error(self) -> None:
        _STATE["commitments_store"] = None
        out = await commitment_list.ainvoke({})
        assert "commitment_list failed: no commitments store" in out

    @pytest.mark.asyncio
    async def test_empty_active_items_returns_label(self) -> None:
        store = _StubCommitmentsStore()
        store._items = []
        set_commitments_store(store)
        out = await commitment_list.ainvoke({})
        assert "no active commitments" in out

    @pytest.mark.asyncio
    async def test_filters_out_terminal_statuses(self) -> None:
        store = _StubCommitmentsStore()
        now = time.time()
        store._items = [
            _FakeCommitment(
                id="c-completed",
                text="done thing",
                status=CommitmentStatus.COMPLETED.value,
                due_window_end_unix=now + 3600,
            ),
            _FakeCommitment(
                id="c-dismissed",
                text="dropped thing",
                status=CommitmentStatus.DISMISSED.value,
                due_window_end_unix=None,
            ),
            _FakeCommitment(
                id="c-active",
                text="active thing",
                status=CommitmentStatus.PENDING.value,
                due_window_end_unix=None,
            ),
        ]
        set_commitments_store(store)
        out = await commitment_list.ainvoke({"due_within_days": 0})
        parsed = json.loads(out)
        ids = [c["id"] for c in parsed]
        assert "c-completed" not in ids
        assert "c-dismissed" not in ids
        assert "c-active" in ids

    @pytest.mark.asyncio
    async def test_far_future_due_window_filtered_by_default(self) -> None:
        # The tool's cutoff is `now + due_within_days * 86400`. Items whose
        # due_window_end_unix is BEYOND the cutoff are excluded; items whose
        # window is within (or before) the cutoff are included.
        store = _StubCommitmentsStore()
        now = time.time()
        store._items = [
            _FakeCommitment(
                id="c-far",
                text="far future thing",
                status=CommitmentStatus.PENDING.value,
                # due more than 7 days in the future → beyond cutoff
                due_window_end_unix=now + 30 * 86400,
            ),
            _FakeCommitment(
                id="c-near",
                text="upcoming thing",
                status=CommitmentStatus.PENDING.value,
                due_window_end_unix=now + 3 * 86400,
            ),
        ]
        set_commitments_store(store)
        out = await commitment_list.ainvoke({"due_within_days": 7})
        parsed = json.loads(out)
        ids = [c["id"] for c in parsed]
        assert "c-far" not in ids
        assert "c-near" in ids

    @pytest.mark.asyncio
    async def test_none_due_window_always_included(self) -> None:
        store = _StubCommitmentsStore()
        store._items = [
            _FakeCommitment(
                id="c-unbound",
                text="no deadline thing",
                status=CommitmentStatus.SNOOZED.value,
                due_window_end_unix=None,
            ),
        ]
        set_commitments_store(store)
        out = await commitment_list.ainvoke({"due_within_days": 7})
        parsed = json.loads(out)
        assert parsed[0]["id"] == "c-unbound"

    @pytest.mark.asyncio
    async def test_due_within_days_zero_returns_all_active(self) -> None:
        store = _StubCommitmentsStore()
        now = time.time()
        store._items = [
            _FakeCommitment(
                id="c-past",
                text="long overdue",
                status=CommitmentStatus.PENDING.value,
                due_window_end_unix=now - 30 * 86400,
            ),
            _FakeCommitment(
                id="c-future",
                text="far future",
                status=CommitmentStatus.DELIVERED.value,
                due_window_end_unix=now + 365 * 86400,
            ),
        ]
        set_commitments_store(store)
        out = await commitment_list.ainvoke({"due_within_days": 0})
        parsed = json.loads(out)
        ids = [c["id"] for c in parsed]
        assert "c-past" in ids
        assert "c-future" in ids

    @pytest.mark.asyncio
    async def test_json_output_contains_expected_fields(self) -> None:
        store = _StubCommitmentsStore()
        store._items = [
            _FakeCommitment(
                id="c-1",
                text="review PR #42",
                status=CommitmentStatus.PENDING.value,
                channel_id="chan-1",
                due_window_hint="this week",
                due_window_end_unix=None,
            ),
        ]
        set_commitments_store(store)
        out = await commitment_list.ainvoke({"due_within_days": 0})
        parsed = json.loads(out)
        c = parsed[0]
        assert c["id"] == "c-1"
        assert c["text"] == "review PR #42"
        assert c["status"] == "pending"
        assert c["channel_id"] == "chan-1"
        assert c["due_window_hint"] == "this week"

    @pytest.mark.asyncio
    async def test_store_raises_returns_error(self) -> None:
        store = _StubCommitmentsStore()
        store.raise_on = "list"
        set_commitments_store(store)
        out = await commitment_list.ainvoke({})
        assert "commitment_list failed" in out
        assert "boom" in out


# ────────────────────────────────────────────────────────────────────
# send_message explicit-channel guard
# ────────────────────────────────────────────────────────────────────


class TestSendMessageInteractivityGuard:
    """send_message interactivity guard coverage for channel-less sends and
    non-interactive no-reply pseudo-channels."""

    def _turn_ctx(
        self,
        *,
        turn_id: str,
        session_id: str,
        trigger: str,
        channel_id: str,
        interactivity: TurnInteractivity | None,
        event_ingress: str | None = None,
    ) -> tuple[TurnContext, object]:
        ctx = TurnContext(
            turn_id=turn_id,
            session_id=session_id,
            trigger=trigger,
            channel_id=channel_id,
            started_at=0.0,
            agent_id="test",
            event_ingress=event_ingress,
            interactivity=interactivity,
        )
        return ctx, set_current_turn(ctx)

    @pytest.mark.asyncio
    async def test_non_interactive_no_channel_errors(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-poller",
            session_id="s-poller",
            trigger="poller",
            channel_id="poller:gmail-inbox",
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        cid_tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "hi"})
        finally:
            reset_current_channel_id(cid_tok)
            reset_current_turn(turn_tok)
        assert "send_message rejected" in out
        assert "not a deliverable channel" in out
        assert bridge.send_calls == []  # nothing was sent

    @pytest.mark.asyncio
    async def test_non_interactive_explicit_channel_works(self) -> None:
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="chan-1")
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-poller",
            session_id="s-poller",
            trigger="poller",
            channel_id="poller:gmail-inbox",
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "chan-1"})
        finally:
            reset_current_turn(turn_tok)
        assert "send_message ok" in out
        assert registry.find_calls == ["chan-1"]
        assert bridge.send_calls == [{"cid": "chan-1", "text": "hi"}]

    @pytest.mark.asyncio
    async def test_interactive_no_channel_is_rejected(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-user-message",
            session_id="s-user-message",
            trigger="user_message",
            channel_id="web-1",
            interactivity=TurnInteractivity.INTERACTIVE,
        )
        cid_tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "hi"})
        finally:
            reset_current_channel_id(cid_tok)
            reset_current_turn(turn_tok)
        assert "send_message rejected" in out
        assert "not a deliverable channel" in out
        assert bridge.send_calls == []

    @pytest.mark.asyncio
    async def test_directives_only_send_skips_targetless_react(self) -> None:
        """A send_message whose text is only an <actions> react (empty clean
        text → nothing sent → no message id) must SKIP the react rather than
        call bridge.react(cid, None, emoji) (chainlink #394)."""
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        out = await send_message.ainvoke({
            "text": '<actions><react emoji="thumbsup" /></actions>',
            "channel_id": "chan-1",
        })
        # No text was sent and the targetless react was skipped (not None).
        assert bridge.send_calls == []
        assert bridge.react_calls == []
        assert "send_message ok" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel_id",
        ["no_reply", "no-reply", "noreply", "none", "  No-RePlY  ", "NONE"],
    )
    async def test_non_interactive_no_reply_sentinel_is_rejected_logged_and_skips_lookup(
        self, tmp_path, channel_id,
    ) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id=channel_id.strip())
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-poller",
            session_id="s-poller",
            trigger="poller",
            channel_id="poller:gmail-inbox",
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({
                "text": "hi",
                "channel_id": channel_id,
            })
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn_tok)
        assert "send_message rejected" in out
        assert "Declining to reply means NOT calling send_message" in out
        assert "operator alert channel" in out
        assert repr(channel_id) in out
        assert registry.find_calls == []
        assert bridge.send_calls == []

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == channel_id
        assert event["reason"] == "non_interactive_no_reply_channel"

    @pytest.mark.asyncio
    async def test_non_interactive_no_reply_sentinel_uses_active_turn_fallback_when_contextvar_lost(
        self, tmp_path,
    ) -> None:
        from mimir import _context
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="No-RePlY")
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-poller",
            session_id="s-poller",
            trigger="poller",
            channel_id="poller:gmail-inbox",
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        int_tok = set_current_turn_interactive(True)
        lost_tok = _context._current_turn.set(None)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "No-RePlY"})
        finally:
            _context._current_turn.reset(lost_tok)
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn_tok)

        assert "send_message rejected" in out
        assert "Declining to reply means NOT calling send_message" in out
        assert registry.find_calls == []
        assert bridge.send_calls == []

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == "No-RePlY"
        assert event["reason"] == "non_interactive_no_reply_channel"

    @pytest.mark.asyncio
    async def test_non_interactive_no_reply_sentinel_fails_closed_with_multiple_active_turns_when_contextvar_lost(
        self, tmp_path,
    ) -> None:
        from mimir import _context
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="No-RePlY")
        set_channel_registry(registry)
        _ctx1, turn1_tok = self._turn_ctx(
            turn_id="t-poller-1",
            session_id="s-poller-1",
            trigger="poller",
            channel_id="poller:gmail-inbox",
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        _ctx2, turn2_tok = self._turn_ctx(
            turn_id="t-poller-2",
            session_id="s-poller-2",
            trigger="user_message",
            channel_id="discord-123",
            interactivity=TurnInteractivity.INTERACTIVE,
        )
        int_tok = set_current_turn_interactive(True)
        lost_tok = _context._current_turn.set(None)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "No-RePlY"})
        finally:
            _context._current_turn.reset(lost_tok)
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn2_tok)
            reset_current_turn(turn1_tok)

        assert "send_message rejected" in out
        assert "Declining to reply means NOT calling send_message" in out
        assert registry.find_calls == []
        assert bridge.send_calls == []

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == "No-RePlY"
        assert event["reason"] == "non_interactive_no_reply_channel"

    @pytest.mark.asyncio
    async def test_no_reply_sentinel_does_not_trust_interactive_trigger_when_interactivity_is_unset(
        self, tmp_path,
    ) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="No-RePlY")
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-user-message",
            session_id="s-user-message",
            trigger="user_message",
            channel_id="web-1",
            interactivity=None,
        )
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "No-RePlY"})
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn_tok)

        assert "send_message rejected" in out
        assert "Declining to reply means NOT calling send_message" in out
        assert registry.find_calls == []
        assert bridge.send_calls == []

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == "No-RePlY"
        assert event["reason"] == "non_interactive_no_reply_channel"

    @pytest.mark.asyncio
    async def test_no_reply_sentinel_fails_closed_when_no_turn_is_resolvable(
        self, tmp_path,
    ) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="No-RePlY")
        set_channel_registry(registry)
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({
                "type": "tool_call",
                "id": "call-1",
                "name": "send_message",
                "args": {
                    "text": "hi",
                    "channel_id": "No-RePlY",
                    "interactivity": "interactive",
                },
            })
        finally:
            reset_current_turn_interactive(int_tok)

        assert out.status == "error"
        assert "send_message rejected" in out.content
        assert "Declining to reply means NOT calling send_message" in out.content
        assert registry.find_calls == []
        assert bridge.send_calls == []

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == "No-RePlY"
        assert event["reason"] == "non_interactive_no_reply_channel"

    @pytest.mark.asyncio
    async def test_http_event_ingress_no_reply_sentinel_rejects_even_when_interactive_true(
        self, tmp_path,
    ) -> None:
        from mimir.event_logger import init_logger
        from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_VALUE

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="No-RePlY")
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-http-event",
            session_id="s-http-event",
            trigger="user_message",
            channel_id="web-1",
            event_ingress=HTTP_EVENT_INGRESS_EXTRA_VALUE,
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "No-RePlY"})
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn_tok)

        assert "send_message rejected" in out
        assert "Declining to reply means NOT calling send_message" in out
        assert registry.find_calls == []
        assert bridge.send_calls == []

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [event] = [e for e in events if e["type"] == "send_message_blocked"]
        assert event["tool"] == "send_message"
        assert event["channel_id"] == "No-RePlY"
        assert event["reason"] == "non_interactive_no_reply_channel"

    @pytest.mark.asyncio
    async def test_interactive_no_reply_sentinel_uses_existing_lookup_path(self) -> None:
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="chan-1")
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-user-message",
            session_id="s-user-message",
            trigger="user_message",
            channel_id="web-1",
            interactivity=TurnInteractivity.INTERACTIVE,
        )
        int_tok = set_current_turn_interactive(False)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "No-Reply"})
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn_tok)

        assert out == "send_message failed: no bridge for channel 'No-Reply'"
        assert "Declining to reply means NOT calling send_message" not in out
        assert registry.find_calls == ["No-Reply"]
        assert bridge.send_calls == []

    @pytest.mark.asyncio
    async def test_tool_args_and_legacy_interactive_contextvar_cannot_override_non_interactive_turn(
        self,
    ) -> None:
        bridge = _StubBridge()
        registry = _StubRegistry(bridge, channel_id="chan-1")
        set_channel_registry(registry)
        _ctx, turn_tok = self._turn_ctx(
            turn_id="t-poller",
            session_id="s-poller",
            trigger="poller",
            channel_id="poller:gmail-inbox",
            interactivity=TurnInteractivity.NON_INTERACTIVE,
        )
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({
                "type": "tool_call",
                "id": "call-1",
                "name": "send_message",
                "args": {
                    "text": "hi",
                    "channel_id": "No-RePlY",
                    "interactivity": "interactive",
                    "trigger": "user_message",
                },
            })
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_turn(turn_tok)

        assert out.status == "error"
        assert "send_message rejected" in out.content
        assert "Declining to reply means NOT calling send_message" in out.content
        assert bridge.send_calls == []
        assert registry.find_calls == []


class TestSendMessageSkiplistGuard:
    def _turn_ctx(self, trigger: str):
        from mimir._context import reset_current_turn, set_current_turn
        from mimir.models import TurnContext

        ctx = TurnContext(
            turn_id=f"t-{trigger}",
            session_id="s1",
            trigger=trigger,
            channel_id="chan-1",
            started_at=0.0,
            agent_id="test",
        )
        tok = set_current_turn(ctx)
        return ctx, tok, reset_current_turn

    @pytest.mark.asyncio
    async def test_poller_skiplist_narration_is_rejected_and_logged(
        self, tmp_path,
    ) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="operator"))
        _ctx, tok, reset = self._turn_ctx("poller")
        try:
            out = await send_message.ainvoke({
                "text": "jira weekly update is automated. skip bucket. end silently.",
                "channel_id": "operator",
            })
        finally:
            reset(tok)

        assert "send_message rejected" in out
        assert "end the turn with no message" in out
        assert bridge.send_calls == []
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [ev] = [e for e in events if e["type"] == "send_message_blocked_skiplist"]
        assert ev["channel_id"] == "operator"
        assert ev["trigger"] == "poller"
        assert ev["matched_phrase"] == "end silently"

    @pytest.mark.asyncio
    async def test_scheduled_tick_skiplist_narration_is_rejected(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="operator"))
        _ctx, tok, reset = self._turn_ctx("scheduled_tick")
        try:
            out = await send_message.ainvoke({
                "text": "Batch complete.",
                "channel_id": "operator",
            })
        finally:
            reset(tok)

        assert "send_message rejected" in out
        assert bridge.send_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("trigger", "message", "matched_phrase"),
        [
            (
                "poller",
                '[skip] Nextdoor digest: "Unfortunately, there is no news about Oliver." - automated neighborhood email, not actionable.',
                "[skip]",
            ),
            (
                "scheduled_tick",
                "[ SKIPPED ] DLCC state-Democrats activation email from staff@dlcc.org - mass political fundraising outreach, not actionable, not in notify list. Filtered.",
                "[skipped]",
            ),
            (
                "poller",
                "   [ SkIp ] long autonomous narration that exceeds the short-message word gate and should still be blocked",
                "[skip]",
            ),
        ],
    )
    async def test_autonomous_leading_skip_marker_is_rejected_and_logged(
        self, tmp_path, trigger: str, message: str, matched_phrase: str,
    ) -> None:
        from mimir.event_logger import init_logger

        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="operator"))
        _ctx, tok, reset = self._turn_ctx(trigger)
        try:
            out = await send_message.ainvoke({
                "text": message,
                "channel_id": "operator",
            })
        finally:
            reset(tok)

        assert "send_message rejected" in out
        assert "end the turn with no message" in out
        assert bridge.send_calls == []
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [ev] = [e for e in events if e["type"] == "send_message_blocked_skiplist"]
        assert ev["channel_id"] == "operator"
        assert ev["trigger"] == trigger
        assert ev["matched_phrase"] == matched_phrase

    @pytest.mark.asyncio
    async def test_poller_escalation_containing_stop_phrase_sends(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="operator"))
        _ctx, tok, reset = self._turn_ctx("poller")
        try:
            out = await send_message.ainvoke({
                "text": "No action needed from you, but heads up: your TLS cert expires tomorrow.",
                "channel_id": "operator",
            })
        finally:
            reset(tok)

        assert "send_message ok" in out
        assert bridge.send_calls == [
            {
                "cid": "operator",
                "text": "No action needed from you, but heads up: your TLS cert expires tomorrow.",
            },
        ]

    @pytest.mark.asyncio
    async def test_interactive_turn_allows_skip_words(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        _ctx, tok, reset = self._turn_ctx("user_message")
        try:
            out = await send_message.ainvoke({
                "text": "[skip] You can skip that step if needed.",
                "channel_id": "chan-1",
            })
        finally:
            reset(tok)

        assert "send_message ok" in out
        assert bridge.send_calls == [
            {"cid": "chan-1", "text": "[skip] You can skip that step if needed."},
        ]

    @pytest.mark.asyncio
    async def test_poller_escalation_without_skiplist_phrase_sends(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="operator"))
        _ctx, tok, reset = self._turn_ctx("poller")
        try:
            out = await send_message.ainvoke({
                "text": "Vendor SkipperCo reported a production outage.",
                "channel_id": "operator",
            })
        finally:
            reset(tok)

        assert "send_message ok" in out
        assert bridge.send_calls == [
            {
                "cid": "operator",
                "text": "Vendor SkipperCo reported a production outage.",
            },
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            "Recommend we skip this release; CI is red on main.",
            "The filtered alert queue still has 2 P1s awaiting triage.",
            "Reminder: no operator alert is configured for the prod DB pager.",
            "The poller found 3 failed jobs; skip bucket cleanup can wait.",
            "FYI [skip] appears in the middle of this escalation.",
            "decisions_made=[skip digest item, notify operator about outage]",
        ],
    )
    async def test_poller_escalation_with_ambiguous_or_embedded_phrase_sends(
        self, message: str,
    ) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="operator"))
        _ctx, tok, reset = self._turn_ctx("poller")
        try:
            out = await send_message.ainvoke({
                "text": message,
                "channel_id": "operator",
            })
        finally:
            reset(tok)

        assert "send_message ok" in out
        assert bridge.send_calls == [{"cid": "operator", "text": message}]


# ────────────────────────────────────────────────────────────────────
# send_message delivery semantics (0.3.0)
# ────────────────────────────────────────────────────────────────────


class TestSendMessageDelivery:
    """send_message uses final=False (typing stays held to turn end), and a
    soft bridge failure (SendResult.sent=False) surfaces as a failure rather
    than silently looking delivered (auto-dispatch removed → sole reply path)."""

    @pytest.mark.asyncio
    async def test_send_uses_final_false_to_hold_typing(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        out = await send_message.ainvoke({"text": "hi", "channel_id": "chan-1"})
        assert "send_message ok" in out
        # final=False so the bridge does NOT cancel typing per-send; run_turn
        # releases it once at turn end (persists across multi-part replies).
        assert bridge.send_finals == [False]

    @pytest.mark.asyncio
    async def test_soft_failure_surfaces_and_is_not_recorded(self, tmp_path) -> None:
        from mimir.history import MessageBuffer, set_global_buffer

        bridge = _StubBridge()
        bridge.send_sent = False  # bridge reports a soft delivery failure
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        buf = MessageBuffer(history_path=tmp_path / "chat_history.jsonl")
        set_global_buffer(buf)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "chan-1"})
        finally:
            set_global_buffer(None)
        # The model is told it failed (not "ok"), so it can react/retry.
        assert "failed" in out and "not delivered" in out
        # The send WAS attempted...
        assert bridge.send_calls == [{"cid": "chan-1", "text": "hi"}]
        # ...but a soft failure is NOT recorded as a delivered assistant message.
        assert [m for m in buf._all if m.kind == "assistant_message"] == []


# ────────────────────────────────────────────────────────────────────
# directive-react delivery accounting (chainlink #408)
# ────────────────────────────────────────────────────────────────────


class TestDirectiveReactAccounting:
    """A <react> directive inside a send_message body is a real delivery:
    a confirmed directive react increments ctx.react_count (so the
    forgot-to-send guard doesn't false-flag an actions-only ack), and a
    declined/raising directive react emits send_message_directive_failed
    instead of vanishing into an except-pass."""

    def _turn_ctx(self):
        ctx = TurnContext(
            turn_id="t1", session_id="s1", trigger="user_message",
            channel_id="chan-1", started_at=0.0, agent_id="test",
            interactivity=TurnInteractivity.INTERACTIVE,
        )
        tok = set_current_turn(ctx)
        return ctx, tok, reset_current_turn

    @pytest.mark.asyncio
    async def test_actions_only_send_with_target_counts_as_reply(self) -> None:
        bridge = _StubBridge()
        bridge.react_returns = True
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        ctx, tok, reset = self._turn_ctx()
        try:
            out = await send_message.ainvoke({
                "text": '<actions><react emoji="👍" message="m-7" /></actions>',
                "channel_id": "chan-1",
            })
        finally:
            reset(tok)
        assert "send_message ok" in out
        assert bridge.react_calls == [
            {"cid": "chan-1", "message_id": "m-7", "emoji": "👍"},
        ]
        assert ctx.send_message_count == 0  # no text was sent
        assert ctx.react_count == 1         # the delivered react counts

    @pytest.mark.asyncio
    async def test_declined_directive_react_does_not_count_and_emits(
        self, tmp_path,
    ) -> None:
        from mimir.event_logger import init_logger
        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        bridge.react_returns = False  # bridge declined
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        ctx, tok, reset = self._turn_ctx()
        try:
            await send_message.ainvoke({
                "text": '<actions><react emoji="👍" message="m-7" /></actions>',
                "channel_id": "chan-1",
            })
        finally:
            reset(tok)
        assert ctx.react_count == 0
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [ev] = [e for e in events if e["type"] == "send_message_directive_failed"]
        assert ev["directive"] == "react"
        assert ev["error"] == "bridge declined"

    @pytest.mark.asyncio
    async def test_raising_directive_react_emits_and_send_still_ok(
        self, tmp_path,
    ) -> None:
        from mimir.event_logger import init_logger
        init_logger(tmp_path / "events.jsonl", session_id="test-session")
        bridge = _StubBridge()
        bridge.raise_on = "react"
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        ctx, tok, reset = self._turn_ctx()
        try:
            out = await send_message.ainvoke({
                "text": 'hi there <splitter>\n<actions><react emoji="👍" /></actions>'.replace(" <splitter>", ""),
                "channel_id": "chan-1",
            })
        finally:
            reset(tok)
        # The text part delivered (counts), the react raised (doesn't).
        assert "send_message ok" in out
        assert ctx.send_message_count == 1
        assert ctx.react_count == 0
        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        [ev] = [e for e in events if e["type"] == "send_message_directive_failed"]
        assert "react boom" in ev["error"]
