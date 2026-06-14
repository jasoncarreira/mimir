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
from typing import Any, Optional

import pytest

from mimir.commitments.models import CommitmentStatus
from mimir.scheduler import SchedulerJob
from mimir.tools.registry import (
    _STATE,
    _channel_from_config_or_state,
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

    def find(self, channel_id: str):
        if channel_id == self._channel_id:
            return self._bridge
        return None


class _StubScheduler:
    """Minimal scheduler stub for add_schedule / remove_schedule / reload_pollers."""

    def __init__(self) -> None:
        self.add_calls: list[dict] = []
        self.remove_calls: list[str] = []
        self.reload_count: int = 0
        self.raise_on: str | None = None
        self._removed: bool = True  # default: job found and removed

    async def add_job(self, job: SchedulerJob) -> SchedulerJob:
        if self.raise_on == "add":
            raise RuntimeError("add boom")
        self.add_calls.append(
            {
                "name": job.name,
                "cron": job.cron,
                "prompt": job.prompt,
                "channel_id": job.channel_id,
            }
        )
        return job

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

    async def complete(self, commitment_id: str, *, message_id=None):
        if self.raise_on == "complete":
            raise RuntimeError("complete boom")
        self.complete_calls.append({"id": commitment_id, "message_id": message_id})
        return "done"

    async def snooze(self, commitment_id: str, *, until_unix: float, reason=None):
        if self.raise_on == "snooze":
            raise RuntimeError("snooze boom")
        self.snooze_calls.append({"id": commitment_id, "until_unix": until_unix})
        return "snoozed"

    async def dismiss(self, commitment_id: str, *, reason=None):
        if self.raise_on == "dismiss":
            raise RuntimeError("dismiss boom")
        self.dismiss_calls.append({"id": commitment_id, "reason": reason})
        return "dismissed"

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
        out = await send_message.ainvoke({"text": "hello"})
        assert "no channel registry configured" in out

    @pytest.mark.asyncio
    async def test_empty_text_returns_error(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": ""})
            assert "text is required" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_whitespace_text_returns_error(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "   "})
            assert "text is required" in out
        finally:
            reset_current_channel_id(tok)

    @pytest.mark.asyncio
    async def test_no_channel_id_returns_error(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge))
        # no contextvar set, no explicit channel_id
        out = await send_message.ainvoke({"text": "hello"})
        assert "no channel_id" in out

    @pytest.mark.asyncio
    async def test_no_bridge_returns_error(self) -> None:
        # registry finds no bridge for this channel
        set_channel_registry(_StubRegistry(bridge=None, channel_id="chan-1"))
        tok = set_current_channel_id("chan-1")
        try:
            out = await send_message.ainvoke({"text": "hello"})
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
            out = await send_message.ainvoke({"text": "hello"})
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
            out = await send_message.ainvoke({"text": "Hello world"})
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
        assert "result=done" in out


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
# send_message interactivity guard (0.3.0)
# ────────────────────────────────────────────────────────────────────


class TestSendMessageInteractivityGuard:
    """0.3.0: a channel-less send_message defaults to the turn's channel only
    on interactive turns; on non-interactive turns it errors and requires an
    explicit channel_id. Explicit channel always works."""

    @pytest.mark.asyncio
    async def test_non_interactive_no_channel_errors(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        cid_tok = set_current_channel_id("chan-1")
        int_tok = set_current_turn_interactive(False)
        try:
            out = await send_message.ainvoke({"text": "hi"})
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_channel_id(cid_tok)
        assert "non-interactive" in out
        assert bridge.send_calls == []  # nothing was sent

    @pytest.mark.asyncio
    async def test_non_interactive_explicit_channel_works(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        int_tok = set_current_turn_interactive(False)
        try:
            out = await send_message.ainvoke({"text": "hi", "channel_id": "chan-1"})
        finally:
            reset_current_turn_interactive(int_tok)
        assert "send_message ok" in out
        assert bridge.send_calls == [{"cid": "chan-1", "text": "hi"}]

    @pytest.mark.asyncio
    async def test_interactive_no_channel_defaults_and_sends(self) -> None:
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        cid_tok = set_current_channel_id("chan-1")
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({"text": "hi"})
        finally:
            reset_current_turn_interactive(int_tok)
            reset_current_channel_id(cid_tok)
        assert "send_message ok" in out
        assert bridge.send_calls == [{"cid": "chan-1", "text": "hi"}]

    @pytest.mark.asyncio
    async def test_directives_only_send_skips_targetless_react(self) -> None:
        """A send_message whose text is only an <actions> react (empty clean
        text → nothing sent → no message id) must SKIP the react rather than
        call bridge.react(cid, None, emoji) (chainlink #394)."""
        bridge = _StubBridge()
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({
                "text": '<actions><react emoji="thumbsup" /></actions>',
                "channel_id": "chan-1",
            })
        finally:
            reset_current_turn_interactive(int_tok)
        # No text was sent and the targetless react was skipped (not None).
        assert bridge.send_calls == []
        assert bridge.react_calls == []
        assert "send_message ok" in out


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
        from mimir.models import TurnContext
        from mimir._context import set_current_turn, reset_current_turn
        ctx = TurnContext(
            turn_id="t1", session_id="s1", trigger="user_message",
            channel_id="chan-1", started_at=0.0, agent_id="test",
        )
        tok = set_current_turn(ctx)
        return ctx, tok, reset_current_turn

    @pytest.mark.asyncio
    async def test_actions_only_send_with_target_counts_as_reply(self) -> None:
        bridge = _StubBridge()
        bridge.react_returns = True
        set_channel_registry(_StubRegistry(bridge, channel_id="chan-1"))
        ctx, tok, reset = self._turn_ctx()
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({
                "text": '<actions><react emoji="👍" message="m-7" /></actions>',
                "channel_id": "chan-1",
            })
        finally:
            reset_current_turn_interactive(int_tok)
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
        int_tok = set_current_turn_interactive(True)
        try:
            await send_message.ainvoke({
                "text": '<actions><react emoji="👍" message="m-7" /></actions>',
                "channel_id": "chan-1",
            })
        finally:
            reset_current_turn_interactive(int_tok)
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
        int_tok = set_current_turn_interactive(True)
        try:
            out = await send_message.ainvoke({
                "text": 'hi there <splitter>\n<actions><react emoji="👍" /></actions>'.replace(" <splitter>", ""),
                "channel_id": "chan-1",
            })
        finally:
            reset_current_turn_interactive(int_tok)
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
