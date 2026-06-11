"""Smoke tests for the deepagents-backed Agent.

These tests stub out ``_build_agent_if_needed`` so no real model or
deepagents graph is constructed. We're verifying the
``run_turn`` orchestration:

  - SAGA pre-message query → memory_block injected into prompt
  - agent.ainvoke called once
  - extract_turn_events / derive_result_fields populate TurnRecord
  - saga.feedback fires post-message with the right atom IDs
  - TurnLogger sees one record
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mimir import mid_turn_injection as _mti
from mimir.agent import Agent
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent
from mimir.turn_logger import TurnLogger


def _make_config(home: Path) -> Config:
    """Build a real Config rooted at ``home``. The per-turn prompt
    assembly (CR#10 + 181-H) reads many Config fields (feedback
    window, usage block toggles, recent-activity limits) so the
    earlier ``_StubConfig`` with only ``home`` no longer suffices.
    """
    import os
    os.environ["MIMIR_HOME"] = str(home)
    return Config.from_env()


class _FakeAgent:
    """Replaces the deepagents CompiledStateGraph. Returns a canned
    message list shaped like ChatClaudeCode's output."""

    def __init__(self, response_messages: list[Any]) -> None:
        self._response_messages = response_messages
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], *, config: dict[str, Any]):
        self.invocations.append({"state": state, "config": config})
        # Echo the input + append response messages (mirrors LangGraph state).
        return {"messages": list(state.get("messages") or []) + self._response_messages}

    async def astream(self, state: dict[str, Any], *, config: dict[str, Any], stream_mode: str = "values"):
        """Yield one cumulative-state chunk (matches stream_mode='values'
        semantics). Real LangGraph emits one chunk per node; for tests
        a single final yield is sufficient — the turn loop derives
        events/output from the final cumulative message list."""
        self.invocations.append({"state": state, "config": config})
        yield {"messages": list(state.get("messages") or []) + self._response_messages}


class _BridgeStub:
    """Captures send / typing / cancel calls so tests can assert on the
    0.3.0 turn-end path: no auto-dispatch, typing held start→end."""

    name = "stub"
    prefixes = ("ch-",)

    def __init__(self) -> None:
        self.sends: list[tuple[str, str, bool]] = []
        self.cancels: list[str] = []
        self.typing_starts: list[str] = []

    async def send(self, channel_id: str, text: str, *,
                   final: bool = True, attachment_paths=None):
        self.sends.append((channel_id, text, final))
        class _R:
            sent = True
            error = None
        return _R()

    async def react(self, *a, **kw):
        return True

    async def cancel_typing(self, channel_id: str) -> None:
        self.cancels.append(channel_id)

    async def send_typing_indicator(self, channel_id: str) -> None:
        self.typing_starts.append(channel_id)

    async def fetch_history(self, *a, **kw):
        return []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


class _FakeSaga:
    """Tiny saga_client double — record query/feedback calls, return
    canned payloads."""

    def __init__(self, query_hits: list[dict[str, Any]] | None = None) -> None:
        self._hits = query_hits or []
        self.query_calls: list[dict[str, Any]] = []
        self.feedback_calls: list[dict[str, Any]] = []

    async def query(
        self, content: str, *, top_k: int = 12,
        session_id: str | None = None,
        context: list[dict[str, str]] | None = None,
        **_ignored: object,
    ):
        self.query_calls.append(
            {
                "content": content, "top_k": top_k,
                "session_id": session_id, "context": context,
            },
        )
        return {"atoms": self._hits, "triples": []}

    async def feedback(self, atom_ids, output, *, session_id=None, feedback="positive"):
        self.feedback_calls.append({
            "atom_ids": list(atom_ids), "output": output,
            "session_id": session_id, "feedback": feedback,
        })




class _BarrierSaga(_FakeSaga):
    """Saga double that pauses during pre-message query so tests can inject a
    follow-up while run_turn is still in setup (before the first model boundary)."""

    def __init__(self) -> None:
        super().__init__(query_hits=[])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def query(
        self, content: str, *, top_k: int = 12,
        session_id: str | None = None,
        context: list[dict[str, str]] | None = None,
        **_ignored: object,
    ):
        self.started.set()
        await self.release.wait()
        return await super().query(
            content, top_k=top_k, session_id=session_id, context=context,
        )


class _BoundaryFakeAgent(_FakeAgent):
    """Fake graph that simulates the first before_model boundary by draining
    whatever the injection registry has accumulated before returning."""

    async def astream(
        self,
        state: dict[str, Any],
        *,
        config: dict[str, Any],
        stream_mode: str = "values",
    ):
        _mti._drain(config["configurable"]["channel_id"])
        async for chunk in super().astream(
            state, config=config, stream_mode=stream_mode,
        ):
            yield chunk


class _FakeChannelSession:
    """Tiny ChannelSession stand-in. Real ChannelSession is a dataclass
    in session_manager.py; we only need the ``saga_session_id`` attribute
    for the agent to read."""

    def __init__(self, channel_id: str) -> None:
        self.saga_session_id = f"saga-{channel_id}-test-id"
        self.channel_id = channel_id
        self.turn_count = 0
        self.ended = False


class _FakeSessionManager:
    """Captures the three SessionManager methods Agent.run_turn calls.

    - ``touch`` returns a stub ChannelSession (with a ``saga_session_id``).
    - ``increment_turn_count`` is a no-op (recorded for assertions).
    - ``end_now`` records the call (the focus of this PR) — does NOT
      actually fire an on_idle callback, since we're asserting the call
      itself was made by Agent, not that the downstream synthesis turn
      was enqueued (which is a SessionManager responsibility tested in
      test_session_manager.py).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _FakeChannelSession] = {}
        self.touch_calls: list[str] = []
        self.end_now_calls: list[str] = []
        self.increment_calls: list[str] = []

    async def touch(self, channel_id: str) -> _FakeChannelSession:
        self.touch_calls.append(channel_id)
        sess = self._sessions.get(channel_id)
        if sess is None or sess.ended:
            sess = _FakeChannelSession(channel_id)
            self._sessions[channel_id] = sess
        return sess

    def increment_turn_count(self, channel_id: str) -> None:
        self.increment_calls.append(channel_id)
        sess = self._sessions.get(channel_id)
        if sess and not sess.ended:
            sess.turn_count += 1

    async def end_now(self, channel_id: str) -> _FakeChannelSession | None:
        self.end_now_calls.append(channel_id)
        sess = self._sessions.pop(channel_id, None)
        if sess is None or sess.ended:
            return None
        sess.ended = True
        return sess


def _build_agent(tmp_path: Path, *,
                 fake_agent: _FakeAgent,
                 fake_saga: _FakeSaga | None = None,
                 session_manager=None) -> Agent:
    from mimir.event_logger import init_logger
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    init_logger(home / "logs" / "events.jsonl", session_id="test")
    cfg = _make_config(home)
    a = Agent(
        config=cfg,
        turn_logger=TurnLogger(home / "logs" / "turns.jsonl"),
        message_buffer=MessageBuffer(history_path=home / "messages.jsonl"),
        index_generator=IndexGenerator(home),
        saga_client=fake_saga,  # type: ignore[arg-type]
        session_manager=session_manager,  # type: ignore[arg-type]
    )
    # Skip the real deepagents.create_deep_agent — return our fake
    # whenever Agent goes to build/fetch the graph.
    a._agent = fake_agent  # type: ignore[attr-defined]
    return a


async def test_run_turn_writes_record_with_extracted_events(tmp_path: Path):
    fake_agent = _FakeAgent(response_messages=[
        AIMessage(
            content="Stored.",
            response_metadata={
                "internal_tool_calls": [
                    {"id": "toolu_1", "name": "memory_store",
                     "args": {"content": "color is blue"}}
                ],
                "tool_results": [
                    {"tool_use_id": "toolu_1", "name": "memory_store",
                     "result": {"stored": True, "atom_id": "f" * 16},
                     "is_error": False},
                ],
                "total_cost_usd": 0.001,
                "num_turns": 2,
            },
        ),
    ])
    fake_saga = _FakeSaga(query_hits=[
        {"atom_id": "a" * 16, "content": "prior memory", "stream": "semantic"},
    ])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)

    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-1",
        content="store my favorite color",
    )
    record = await agent.run_turn(event)

    # SAGA was queried with the user content
    assert len(fake_saga.query_calls) == 1
    assert fake_saga.query_calls[0]["content"] == "store my favorite color"

    # The pre-message memory block landed in the prompt to the agent
    invocation = fake_agent.invocations[0]
    prompt_msg = invocation["state"]["messages"][0]
    assert isinstance(prompt_msg, HumanMessage)
    assert "Possibly relevant memories" in prompt_msg.content

    # Events were extracted from response_metadata
    event_types = [e["type"] for e in record.events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    tc = next(e for e in record.events if e["type"] == "tool_call")
    assert tc["name"] == "memory_store"

    # Result fields surfaced cost + num_turns
    assert record.total_cost_usd == pytest.approx(0.001)
    assert record.num_turns == 2
    assert record.error is None

    # The TurnLogger appended one record
    turns = (tmp_path / "home" / "logs" / "turns.jsonl").read_text().splitlines()
    assert len(turns) == 1


class _FoldingFakeAgent(_FakeAgent):
    """Fake graph that simulates the MidTurnInjectionMiddleware folding a
    mid-turn user message mid-stream: during ``astream`` it injects + drains
    the registry for the turn's channel (exactly what ``before_model`` does on
    a real boundary), so ``run_turn``'s finally sees a folded record to thread
    into ``TurnRecord.injected_inputs`` (chainlink #376 PR 3/4).

    Note: this drives the registry directly, NOT the dispatcher's enqueue path,
    so the inject-time chat-history append (PR 4) does not fire here — that's
    covered in test_dispatcher. This test pins the turn-record threading."""

    def __init__(self, response_messages: list[Any], *, folded: list[AgentEvent]) -> None:
        super().__init__(response_messages)
        self._folded = folded

    async def astream(self, state: dict[str, Any], *, config: dict[str, Any], stream_mode: str = "values"):
        ch = config["configurable"]["channel_id"]
        for ev in self._folded:
            assert _mti.inject_message(ch, ev) == "injected"
            _mti._drain(ch)            # the boundary fold — records to `folded`
        async for chunk in super().astream(state, config=config, stream_mode=stream_mode):
            yield chunk


async def test_run_turn_records_folded_mid_turn_inputs(tmp_path: Path):
    """A message folded into the turn lands in TurnRecord.injected_inputs as
    {t_ms, text} (rendered as the model saw it, with a start-relative offset)
    and round-trips through turns.jsonl — while ``input`` stays the original
    prompt (PR 3/4 durable visibility readers a/b)."""
    _mti._REGISTRY.clear()
    folded = [AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="also check the staging logs",
        author="discord-7", author_display="alice",
    )]
    fake_agent = _FoldingFakeAgent([AIMessage(content="done")], folded=folded)
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="deploy please")
    record = await agent.run_turn(event)

    # (a) the folded input is on the record as {t_ms, text}, rendered with header.
    assert len(record.injected_inputs) == 1
    entry = record.injected_inputs[0]
    assert "also check the staging logs" in entry["text"]
    assert "mid-turn message from alice" in entry["text"]
    assert isinstance(entry["t_ms"], (int, float)) and entry["t_ms"] >= 0
    # original prompt unchanged — this is an ADDITIONAL input, not a rewrite.
    assert "deploy please" in record.input

    # (b) round-trips through turns.jsonl.
    import json
    turns = (tmp_path / "home" / "logs" / "turns.jsonl").read_text().splitlines()
    row = json.loads(turns[-1])
    assert row["injected_inputs"] == record.injected_inputs


async def test_run_turn_no_injected_inputs_when_nothing_folded(tmp_path: Path):
    """The common case: no mid-turn message → injected_inputs is empty and the
    buffer holds only the original inbound."""
    _mti._REGISTRY.clear()
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)
    record = await agent.run_turn(
        AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    )
    assert record.injected_inputs == []
    assert agent._buffer.channel_count("ch-1") == 1


async def test_append_inbound_to_buffer_is_idempotent(tmp_path: Path):
    """chainlink #376 PR 4: a message recorded at inject time must not be
    double-recorded if it later re-routes as its own turn (leftover) and the
    normal inbound path appends it again. The ``_buffer_recorded`` flag guards it."""
    agent = _build_agent(
        tmp_path, fake_agent=_FakeAgent(response_messages=[AIMessage(content="ok")]),
        fake_saga=None,
    )
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hello")
    await agent.on_message_injected(event)   # inject-time record
    assert agent._buffer.channel_count("ch-1") == 1
    await agent._append_inbound_to_buffer(event)  # leftover re-route → no-op
    assert agent._buffer.channel_count("ch-1") == 1


class _RequeueCaptureDispatcher:
    """Minimal dispatcher double: records requeue_front calls, no startup drain."""

    def __init__(self) -> None:
        self.requeued: list[AgentEvent] = []

    def drain_startup_user_messages(self, channel_id):  # noqa: ANN001
        return []

    def requeue_front(self, events):  # noqa: ANN001
        self.requeued.extend(events)
        return len(events)


async def test_run_turn_defers_folded_message(tmp_path: Path):
    """chainlink #384: a folded message the agent defers is (a) marked
    deferred=true in this turn's injected_inputs, and (b) re-enqueued as its own
    fresh turn (requeue_front) carrying force_new_turn + deferred_from_turn_id +
    deferred_reason, with the original content/author preserved."""
    _mti._REGISTRY.clear()
    folded = AgentEvent(
        trigger="user_message", channel_id="ch-1", content="unrelated new ask",
        author="discord-9", author_display="bob", source_id="m-999",
    )

    class _DeferringFakeAgent(_FakeAgent):
        async def astream(self, state, *, config, stream_mode="values"):
            ch = config["configurable"]["channel_id"]
            assert _mti.inject_message(ch, folded) == "injected"
            _mti._drain(ch)                                   # fold it
            assert _mti.defer_message(ch, "m-999", "topic switch") == "deferred"
            async for chunk in super().astream(state, config=config, stream_mode=stream_mode):
                yield chunk

    agent = _build_agent(
        tmp_path, fake_agent=_DeferringFakeAgent([AIMessage(content="on the first task")]),
        fake_saga=None,
    )
    cap = _RequeueCaptureDispatcher()
    agent._dispatcher = cap  # type: ignore[assignment]

    record = await agent.run_turn(
        AgentEvent(trigger="user_message", channel_id="ch-1", content="first task")
    )

    # (a) originating turn's injected_inputs entry marked deferred.
    assert len(record.injected_inputs) == 1
    entry = record.injected_inputs[0]
    assert entry.get("deferred") is True
    assert "unrelated new ask" in entry["text"]

    # (b) re-enqueued as a force_new_turn own-turn, traceable, content preserved.
    assert len(cap.requeued) == 1
    dev = cap.requeued[0]
    assert dev.source_id == "m-999"
    assert dev.content == "unrelated new ask"
    assert dev.extra.get("force_new_turn") is True
    assert dev.extra.get("deferred_from_turn_id") == record.turn_id
    assert dev.extra.get("deferred_reason") == "topic switch"


async def test_run_turn_no_saga_skips_query_and_feedback(tmp_path: Path):
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    record = await agent.run_turn(event)
    assert record.output == "ok"
    # No SAGA → no memory block injected
    prompt = fake_agent.invocations[0]["state"]["messages"][0].content
    assert "Possibly relevant memories" not in prompt


async def test_run_turn_scheduled_tick_skips_saga_query(tmp_path: Path):
    """scheduled_tick turns must NOT call saga.query() — no meaningful user
    query to anchor retrieval, so it's wasteful and the atoms would be noise.
    Session summaries (get_last_sessions path) still fire normally."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="heartbeat done")])
    fake_saga = _FakeSaga(query_hits=[
        {"atom_id": "b" * 16, "content": "some memory", "stream": "semantic"},
    ])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="## Scheduled tick\nchannel: heartbeat",
    )
    record = await agent.run_turn(event)

    assert record.output == "heartbeat done"
    # saga.query() must NOT have been called
    assert fake_saga.query_calls == [], (
        "saga.query() was called for a scheduled_tick turn — "
        "this is wasteful; there's no meaningful user query to anchor retrieval"
    )
    # "Possibly relevant memories" block must be absent from the prompt
    prompt = fake_agent.invocations[0]["state"]["messages"][0].content
    assert "Possibly relevant memories" not in prompt


async def test_run_turn_poller_skips_saga_query(tmp_path: Path):
    """poller turns (github-activity, oauth-usage-poll, etc.) have no
    meaningful user-authored query anchor — saga.query() must be skipped
    even when a saga client is present."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="poll done")])
    fake_saga = _FakeSaga(query_hits=[
        {"atom_id": "c" * 16, "content": "some memory", "stream": "semantic"},
    ])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)

    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        content="github-activity poll body",
    )
    record = await agent.run_turn(event)

    assert record.output == "poll done"
    # saga.query() must NOT have been called
    assert fake_saga.query_calls == [], (
        "saga.query() was called for a poller turn — "
        "this is wasteful; there's no meaningful user query to anchor retrieval"
    )
    # "Possibly relevant memories" block must be absent from the prompt
    prompt = fake_agent.invocations[0]["state"]["messages"][0].content
    assert "Possibly relevant memories" not in prompt


# ─── IMMEDIATE_SESSION_END_TRIGGERS — end_now called immediately ────


async def test_run_turn_scheduled_tick_ends_session_immediately(tmp_path: Path):
    """After a ``scheduled_tick`` turn completes, the session manager's
    ``end_now`` must fire — synthesis turn enqueued immediately,
    bypassing the standard ``MIMIR_SAGA_SESSION_IDLE_MINUTES`` countdown.

    Cron-fired heartbeats don't anchor a conversation; the next cron tick
    creates its own session. Waiting 10 minutes to synthesize is just
    deferred work.
    """
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="heartbeat done")])
    fake_sessions = _FakeSessionManager()
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=fake_sessions,
    )

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="## Scheduled tick\nchannel: heartbeat",
    )
    await agent.run_turn(event)

    # touch fires at turn start, end_now fires after turn finished.
    assert fake_sessions.touch_calls == ["scheduler:heartbeat"]
    assert fake_sessions.end_now_calls == ["scheduler:heartbeat"], (
        "end_now must be called for scheduled_tick — without it the "
        "synthesis turn waits MIMIR_SAGA_SESSION_IDLE_MINUTES (default 10)"
    )


async def test_run_turn_poller_ends_session_immediately(tmp_path: Path):
    """Same as scheduled_tick: poller-fired turns trigger immediate
    session end. Pollers (github-activity, ntfy, oauth-usage) fire
    autonomously and don't sustain a conversation either."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="poller batch processed")])
    fake_sessions = _FakeSessionManager()
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=fake_sessions,
    )

    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        content="## github-activity\nNew comment on jasoncarreira/mimir#100: ...",
    )
    await agent.run_turn(event)

    assert fake_sessions.end_now_calls == ["poller:github-activity"]


async def test_run_turn_user_message_does_not_end_session_immediately(tmp_path: Path):
    """``user_message`` triggers a real conversation. The session must
    stay alive so follow-up messages on the same channel keep adding to
    it — synthesis fires only after MIMIR_SAGA_SESSION_IDLE_MINUTES of
    silence. end_now must NOT be called."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="hi back")])
    fake_sessions = _FakeSessionManager()
    fake_saga = _FakeSaga()
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=fake_saga,
        session_manager=fake_sessions,
    )

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-123",
        content="hello",
    )
    await agent.run_turn(event)

    assert fake_sessions.touch_calls == ["discord-123"]
    assert fake_sessions.end_now_calls == [], (
        "end_now must NOT be called for user_message — the conversation "
        "is live; synthesis should wait for the idle timer"
    )


async def test_run_turn_saga_session_end_does_not_recurse(tmp_path: Path):
    """``saga_session_end`` IS the synthesis turn — ending the session
    that just produced it would loop. end_now must NOT fire here even
    though saga_session_end is in NON_USER_QUERY_TRIGGERS (because the
    NON_USER set is about saga.query skipping, not session lifecycle)."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="session summarized")])
    fake_sessions = _FakeSessionManager()
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=fake_sessions,
    )

    event = AgentEvent(
        trigger="saga_session_end",
        channel_id="scheduler:heartbeat",
        content="## Saga session end synthesis\n...",
    )
    await agent.run_turn(event)

    assert fake_sessions.end_now_calls == [], (
        "end_now must NOT be called for saga_session_end — that's the "
        "synthesis turn; ending its own session would recurse"
    )


async def test_run_turn_immediate_end_failure_does_not_crash_turn(tmp_path: Path):
    """If end_now raises (e.g. dispatcher queue full when enqueueing
    saga_session_end), the autonomous turn that just finished should
    still return cleanly — the error is logged + swallowed, the record
    is returned to the dispatcher."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])

    class _BrokenSessionManager(_FakeSessionManager):
        async def end_now(self, channel_id: str):
            self.end_now_calls.append(channel_id)
            raise RuntimeError("dispatcher queue full")

    fake_sessions = _BrokenSessionManager()
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=fake_sessions,
    )

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="tick",
    )
    # Must NOT raise.
    record = await agent.run_turn(event)
    assert record.output == "ok"
    assert fake_sessions.end_now_calls == ["scheduler:heartbeat"]


async def test_run_turn_typing_fires_at_start_and_cancels_at_end(
    tmp_path: Path,
):
    """0.3.0: on an interactive turn the typing indicator fires at turn
    START (so the user sees the message was received) and is released only
    at turn END — never auto-dispatching the model's text."""
    from mimir.channel_registry import ChannelRegistry

    fake_agent = _FakeAgent(response_messages=[
        AIMessage(content="here is my reply"),
    ])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]

    agent = _build_agent(tmp_path, fake_agent=fake_agent)
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    record = await agent.run_turn(event)

    # Typing fired at start, released at end.
    assert bridge.typing_starts == ["ch-1"]
    assert bridge.cancels == ["ch-1"]
    # No auto-dispatch: the turn loop never ships text via bridge.send.
    assert bridge.sends == []
    # The model's final text is captured as reasoning in the turn record.
    assert "here is my reply" in record.output
    assert record.error is None


async def test_run_turn_records_error_when_ainvoke_raises(tmp_path: Path):
    class _BoomAgent:
        async def ainvoke(self, *a, **kw):
            raise RuntimeError("upstream failure")
        async def astream(self, *a, **kw):
            raise RuntimeError("upstream failure")
            yield  # unreachable, makes this an async generator
    fake_saga = _FakeSaga()
    agent = _build_agent(
        tmp_path, fake_agent=_BoomAgent(), fake_saga=fake_saga,  # type: ignore[arg-type]
    )
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="x")
    record = await agent.run_turn(event)
    assert record.error and "upstream failure" in record.error
    assert record.events == []
    # feedback skipped on error
    assert fake_saga.feedback_calls == []


async def test_run_turn_emits_turn_failed_event_on_error(tmp_path: Path):
    """Any turn that fails must emit a ``turn_failed`` event so the
    failure is operator-visible (ops dashboard + events.jsonl), not just
    a ``log.exception`` line. Uses a poller-trigger to confirm it fires
    regardless of turn kind. Regression for the silently-dropped
    poller-review 503 (chainlink #299)."""
    import json

    class _BoomAgent:
        async def ainvoke(self, *a, **kw):
            raise RuntimeError("codex 503 boom")

        async def astream(self, *a, **kw):
            raise RuntimeError("codex 503 boom")
            yield  # unreachable — makes this an async generator

    agent = _build_agent(tmp_path, fake_agent=_BoomAgent())  # type: ignore[arg-type]
    event = AgentEvent(trigger="poller", channel_id="ch-1", content="review PR #511")
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    failed = [e for e in evs if e.get("type") == "turn_failed"]
    assert len(failed) == 1, (
        f"expected exactly one turn_failed event; "
        f"got types {[e.get('type') for e in evs]}"
    )
    ev = failed[0]
    assert ev.get("trigger") == "poller"  # fires for ANY turn kind
    assert ev.get("channel_id") == "ch-1"
    assert "codex 503 boom" in (ev.get("error") or "")


# ── turn-outcome item identity for poller recovery (chainlink #262) ──


async def test_turn_failed_carries_poller_item_identity(tmp_path: Path):
    """A failed poller turn stamps the originating item identity
    (source_id + poller_name + items) onto ``turn_failed`` so the
    framework can correlate the failure to the specific poller item(s)
    and re-emit them (chainlink #262). No ``turn_completed`` on failure."""
    import json

    class _BoomAgent:
        async def ainvoke(self, *a, **kw):
            raise RuntimeError("codex 503 boom")

        async def astream(self, *a, **kw):
            raise RuntimeError("codex 503 boom")
            yield  # unreachable — makes this an async generator

    agent = _build_agent(tmp_path, fake_agent=_BoomAgent())  # type: ignore[arg-type]
    items = [{"event_type": "pr_review_requested",
              "repo": "jasoncarreira/mimir", "number": 511}]
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        content="review PR #511",
        source_id="poller:github-activity:1700:batch:0",
        extra={"poller_name": "github-activity", "items": items},
    )
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    failed = [e for e in evs if e.get("type") == "turn_failed"]
    assert len(failed) == 1
    ev = failed[0]
    assert ev.get("source_id") == "poller:github-activity:1700:batch:0"
    assert ev.get("poller_name") == "github-activity"
    assert ev.get("items") == items
    # No success event on a failed turn.
    assert [e for e in evs if e.get("type") == "turn_completed"] == []


async def test_turn_completed_emitted_for_successful_poller_turn(tmp_path: Path):
    """A successful poller turn emits ``turn_completed`` carrying the same
    item identity, so the framework can advance the per-poller watermark
    past the processed item(s) and not re-emit them (chainlink #262)."""
    import json

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="reviewed")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=_FakeSessionManager(),
    )
    items = [{"event_type": "pr_review_requested",
              "repo": "jasoncarreira/mimir", "number": 511}]
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        content="review PR #511",
        source_id="poller:github-activity:1700:batch:0",
        extra={"poller_name": "github-activity", "items": items},
    )
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    completed = [e for e in evs if e.get("type") == "turn_completed"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev.get("trigger") == "poller"
    assert ev.get("channel_id") == "poller:github-activity"
    assert ev.get("source_id") == "poller:github-activity:1700:batch:0"
    assert ev.get("poller_name") == "github-activity"
    assert ev.get("items") == items
    # Success ⇒ no failure event.
    assert [e for e in evs if e.get("type") == "turn_failed"] == []


async def test_turn_completed_not_emitted_for_non_poller_turn(tmp_path: Path):
    """``turn_completed`` is poller-gated — a successful ``user_message``
    turn must NOT emit it, so events.jsonl doesn't grow a success event
    per conversational turn (chainlink #262)."""
    import json

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="hi back")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
        session_manager=_FakeSessionManager(),
    )
    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-123",
        content="hello",
    )
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = (
        [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
        if events_log.exists() else []
    )
    assert [e for e in evs if e.get("type") == "turn_completed"] == []


# ── chat-history buffer append (regression for PR #181 drop) ────────


async def test_run_turn_appends_inbound_to_message_buffer(tmp_path: Path):
    """user_message event must land in the chat-history buffer
    BEFORE prompt assembly so ``assemble_recent_activity`` sees the
    inbound on this very turn. Regression for PR #181's deepagents
    migration which silently dropped the SDK-era inline append calls
    (last chat_history.jsonl write was 2026-05-17T21:48; mimirbot
    operated for ~19h with frozen Recent activity)."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    fake_saga = _FakeSaga()
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=fake_saga,
    )
    event = AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hello there", author="jason", author_display="Jason",
        source="discord",
    )
    record = await agent.run_turn(event)
    assert record.error is None

    # Both the inbound (user_message) and the outbound (assistant_message
    # via the fallback bridge.send) should be in the buffer. This
    # build_agent has no _channels, so only the inbound lands.
    msgs = list(agent._buffer._all)
    assert any(m.content == "hello there" and m.kind == "user_message"
               for m in msgs), (
        f"inbound user_message missing from buffer; got: "
        f"{[(m.kind, m.content[:30]) for m in msgs]}"
    )


async def test_run_turn_interactive_no_send_message_emits_no_reply_signal(
    tmp_path: Path,
):
    """0.3.0: an interactive turn that produced final text but never called
    send_message ships NOTHING (auto-dispatch is gone) and emits a negative
    ``interactive_turn_no_send_message`` feedback signal. The text is kept
    as reasoning in the turn record, not as a sent assistant_message."""
    import json
    from mimir.channel_registry import ChannelRegistry

    fake_agent = _FakeAgent(response_messages=[
        AIMessage(content="I worked out the answer but never sent it"),
    ])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga())
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hi", author="jason", source="discord",
    )
    record = await agent.run_turn(event)
    assert record.error is None
    # No auto-dispatch: nothing shipped, nothing buffered as a sent message.
    assert bridge.sends == []
    assert [m for m in agent._buffer._all if m.kind == "assistant_message"] == []
    # The unsent text is captured as reasoning in the turn record.
    assert "never sent it" in record.output

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    no_reply = [e for e in evs if e.get("type") == "interactive_turn_no_send_message"]
    assert len(no_reply) == 1, (
        f"expected the no-reply signal; got types {[e.get('type') for e in evs]}"
    )
    assert no_reply[0].get("channel_id") == "ch-1"
    assert no_reply[0].get("output_chars", 0) > 0


async def test_run_turn_successful_send_suppresses_no_reply_signal(
    tmp_path: Path,
):
    """When the turn actually DELIVERED a reply (send_message_count > 0), the
    forgot-to-send signal must NOT fire even with final text. The guard keys
    off confirmed delivery, NOT the mere presence of a send_message tool call
    (which can be refused / soft-fail and deliver nothing)."""
    import json
    from mimir.channel_registry import ChannelRegistry

    class _DeliveringAgent(_FakeAgent):
        """Simulate a confirmed send by bumping send_message_count on the
        active turn context — exactly what the real tool does after the
        bridge reports SendResult.sent=True."""
        async def astream(self, state, *, config, stream_mode="values"):
            from mimir._context import get_current_turn
            _ctx = get_current_turn()
            if _ctx is not None:
                _ctx.send_message_count += 1
                # chainlink #423: the guard is channel-scoped — the real
                # tool records the delivery channel alongside the count.
                _ctx.delivered_channel_ids.add("ch-1")
            async for chunk in super().astream(
                state, config=config, stream_mode=stream_mode,
            ):
                yield chunk

    fake_agent = _DeliveringAgent(response_messages=[AIMessage(content="done")])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga())
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    assert [e for e in evs if e.get("type") == "interactive_turn_no_send_message"] == []


async def test_run_turn_react_only_suppresses_no_reply_signal(tmp_path: Path):
    """0.3.2: a react-only reply (the react tool, no send_message) is a valid
    interactive response (an acknowledgment), so the forgot-to-send guard must
    NOT fire — keying off ctx.react_count, not just send_message_count."""
    import json
    from mimir.channel_registry import ChannelRegistry

    class _ReactingAgent(_FakeAgent):
        """Simulate a confirmed react by bumping react_count on the active turn
        context — exactly what the real react tool does on a successful react."""
        async def astream(self, state, *, config, stream_mode="values"):
            from mimir._context import get_current_turn
            _ctx = get_current_turn()
            if _ctx is not None:
                _ctx.react_count += 1
                _ctx.delivered_channel_ids.add("ch-1")
            async for chunk in super().astream(
                state, config=config, stream_mode=stream_mode,
            ):
                yield chunk

    fake_agent = _ReactingAgent(response_messages=[AIMessage(content="absorbed it 👍")])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga())
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    assert [e for e in evs if e.get("type") == "interactive_turn_no_send_message"] == []


async def test_run_turn_non_interactive_no_signal_no_typing(tmp_path: Path):
    """A non-interactive turn (scheduled_tick) on a bridge channel produces
    no typing indicator and no forgot-to-send signal — trigger gating applies
    even when the channel has a bridge."""
    import json
    from mimir.channel_registry import ChannelRegistry

    fake_agent = _FakeAgent(response_messages=[
        AIMessage(content="heartbeat ran; nothing to surface"),
    ])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga())
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(trigger="scheduled_tick", channel_id="ch-1", content="tick")
    await agent.run_turn(event)

    assert bridge.typing_starts == []  # no typing on a non-interactive turn
    assert bridge.sends == []
    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    assert [e for e in evs if e.get("type") == "interactive_turn_no_send_message"] == []


async def test_inbound_buffer_append_skips_internal_wake_triggers(tmp_path: Path):
    """``saga_session_end`` + ``shell_job_complete`` are internal wakes
    with no conversational content the agent would want in Recent
    activity. Pre-#181 explicitly skipped both in ``_record_inbound``;
    keep parity."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )

    for trig in ("saga_session_end", "shell_job_complete"):
        await agent._append_inbound_to_buffer(AgentEvent(
            trigger=trig, channel_id="ch-1", content="ignore me",
        ))
    assert agent._buffer.total_count() == 0


async def test_inbound_buffer_append_logs_scheduled_tick_as_system_note(
    tmp_path: Path,
):
    """Pre-#181 logged ``scheduled_tick`` as kind=system_note so the
    agent saw "I was woken at 10:00 with prompt X" in its next
    Recent activity. Allow-list scoping (an earlier mistake in this
    PR) silently dropped these; explicit regression guard."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )
    await agent._append_inbound_to_buffer(AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="Heartbeat — check for new commitments due.",
    ))
    msgs = list(agent._buffer._all)
    assert len(msgs) == 1
    assert msgs[0].kind == "system_note"
    assert "Heartbeat" in msgs[0].content


async def test_inbound_buffer_append_falls_back_to_author_for_display(
    tmp_path: Path,
):
    """Pre-#181: ``author_display=event.author_display or event.author``
    — display falls back to the platform-prefixed author key when the
    bridge didn't resolve a friendlier name. Locks in that parity."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )
    await agent._append_inbound_to_buffer(AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hi", author="discord-99",
        author_display=None,  # bridge didn't supply one
    ))
    msgs = list(agent._buffer._all)
    assert len(msgs) == 1
    assert msgs[0].author == "discord-99"
    assert msgs[0].author_display == "discord-99"


async def test_send_message_tool_appends_outbound_via_global_buffer(tmp_path: Path):
    """The ``send_message`` tool reads the buffer from
    ``mimir.history.get_global_buffer`` (set by ``server.py`` at
    startup) and appends every successful send. Regression guard
    for the tool-path side of the PR #181 regression."""
    from mimir.history import MessageBuffer, set_global_buffer
    from mimir.tools import registry as tools_reg

    class _CapBridge:
        name = "fake"
        async def send(self, channel_id, text, attachment_paths=None, *, final=True):
            class _R:
                sent = True
                message_id = "msg-tool-1"
            return _R()
        async def react(self, *a, **kw):
            return True

    class _Channels:
        def __init__(self, bridge):
            self._bridge = bridge
        def find(self, channel_id):
            return self._bridge

    buf = MessageBuffer(history_path=tmp_path / "chat_history.jsonl")
    set_global_buffer(buf)
    tools_reg.set_channel_registry(_Channels(_CapBridge()))
    tools_reg.set_current_channel_id("ch-tool")
    try:
        send_message = tools_reg.send_message
        # Call the @tool wrapper's underlying coro directly.
        result = await send_message.ainvoke({"text": "outbound from tool"})
        assert "send_message ok" in result
        # Buffer got the append.
        msgs = list(buf._all)
        assert len(msgs) == 1
        assert msgs[0].kind == "assistant_message"
        assert msgs[0].content == "outbound from tool"
        assert msgs[0].channel_id == "ch-tool"
        assert msgs[0].msg_id == "msg-tool-1"
    finally:
        set_global_buffer(None)  # type: ignore[arg-type]
        tools_reg.set_channel_registry(None)
        tools_reg.set_current_channel_id(None)


# ── agent_id plumbing (Shape B multi-agent observability) ──────────


async def test_run_turn_threads_agent_id_into_turn_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Config.agent_id (MIMIR_AGENT_ID) must land on TurnRecord so
    a cross-process operator running two agents on the same host
    can filter merged turns.jsonl output by agent. Locks in the
    Shape B observability invariant."""
    monkeypatch.setenv("MIMIR_AGENT_ID", "muninnbot")
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="hello")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )
    event = AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hi", author="jason",
    )
    record = await agent.run_turn(event)
    assert record.error is None
    assert record.agent_id == "muninnbot", (
        f"expected agent_id='muninnbot' on TurnRecord; got {record.agent_id!r}"
    )


async def test_run_turn_default_agent_id_is_mimir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When MIMIR_AGENT_ID is unset, the default is ``"mimir"`` —
    every record gets the tag, single-agent deployments stay
    unaffected. Pre-existing turns.jsonl readers will see the new
    field and tolerate it."""
    monkeypatch.delenv("MIMIR_AGENT_ID", raising=False)
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )
    record = await agent.run_turn(AgentEvent(
        trigger="user_message", channel_id="ch-1", content="x",
    ))
    assert record.agent_id == "mimir"


async def test_event_logger_stamps_agent_id_on_records(tmp_path: Path):
    """The EventLogger stamps every record with the agent_id passed
    at init time. ``None`` (the pre-existing shape) omits the key
    so downstream readers don't see a spurious agent_id=None on
    legacy operator runs."""
    from mimir.event_logger import EventLogger
    import json as _json

    path = tmp_path / "events.jsonl"

    # With agent_id set: key appears in every record.
    logger_with = EventLogger(
        path, session_id="sess-x", agent_id="muninnbot",
    )
    await logger_with.log("test_event", foo="bar")
    line = path.read_text().splitlines()[0]
    rec = _json.loads(line)
    assert rec["agent_id"] == "muninnbot"
    assert rec["session_id"] == "sess-x"
    assert rec["type"] == "test_event"
    assert rec["foo"] == "bar"

    # Without agent_id: key absent (not present-but-null).
    path2 = tmp_path / "events_no_agent.jsonl"
    logger_no = EventLogger(path2, session_id="sess-y")
    await logger_no.log("test_event")
    rec2 = _json.loads(path2.read_text().splitlines()[0])
    assert "agent_id" not in rec2, (
        "agent_id should be absent (not None) when not set, "
        "to keep the legacy record shape exactly"
    )


# ─── Algedonic pipeline gaps (algedonic-gaps-5 PR) ──────────────────


async def test_run_turn_does_not_auto_feedback(tmp_path: Path):
    """Operator decision 2026-05-29: the per-turn auto-credit pass is gone.

    run_turn must NOT call ``saga.feedback()`` just because a turn cited
    atoms and didn't fail, and must NOT emit ``saga_feedback_sent`` — that
    blanket positive-on-success boost inflated every retrieved atom
    regardless of use. Activation now rises only from the retrieval access
    event recall logs + DELIBERATE agent-curated feedback (the
    ``saga_feedback`` / ``saga_mark_contributions`` tools carry the
    ``saga_feedback_sent`` emit now)."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="done")])
    fake_saga = _FakeSaga(query_hits=[
        {"atom_id": "a" * 16, "content": "prior memory", "stream": "semantic"},
    ])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hello")
    await agent.run_turn(event)

    # No automatic contribution-credit call, even though an atom was cited
    # and the turn succeeded.
    assert fake_saga.feedback_calls == [], (
        "run_turn must not auto-call saga.feedback (per-turn auto-credit removed)"
    )
    import json as _json
    events_path = tmp_path / "home" / "logs" / "events.jsonl"
    event_types = [_json.loads(l)["type"] for l in events_path.read_text().splitlines() if l]
    assert "saga_feedback_sent" not in event_types, (
        "run_turn must not emit saga_feedback_sent — now carried by the "
        "agent-curated feedback tools"
    )


async def test_run_turn_emits_tool_call_denied_per_denial(tmp_path: Path):
    """Gap 3 fix: each entry drained from ``backend.drain_denials()`` must
    produce a ``tool_call_denied`` event in events.jsonl so write-guard
    denials surface in the algedonic block."""
    import json as _json

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)

    # Inject a fake backend with two pre-populated denials.
    class _FakeBackend:
        def drain_denials(self):
            return [
                {"op": "write", "file_path": "/etc/passwd", "writable_dirs": []},
                {"op": "edit", "file_path": "/proc/sys/x", "writable_dirs": []},
            ]

    agent._backend = _FakeBackend()  # type: ignore[attr-defined]

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="do bad thing")
    await agent.run_turn(event)

    events_path = tmp_path / "home" / "logs" / "events.jsonl"
    denied = [
        _json.loads(l) for l in events_path.read_text().splitlines() if l
        if _json.loads(l)["type"] == "tool_call_denied"
    ]
    assert len(denied) == 2, (
        "one tool_call_denied event expected per denial; got: "
        + str([e.get("file_path") for e in denied])
    )
    ops = {e.get("op") for e in denied}
    assert ops == {"write", "edit"}


async def test_run_turn_emits_synthesis_skipped_boundary_when_not_called(tmp_path: Path):
    """Gap 2 fix: on a ``saga_session_end`` (synthesis) turn, if
    ``saga_end_session`` was never called, a
    ``saga_synthesis_skipped_boundary`` event must land in events.jsonl
    so the next turn's algedonic block flags the missed step 3."""
    import json as _json

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="synthesis done")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)

    # Synthesis trigger; ctx.saga_end_session_called stays False (no tool called).
    event = AgentEvent(
        trigger="saga_session_end", channel_id="ch-1",
        content="## Session synthesis\n",
    )
    await agent.run_turn(event)

    events_path = tmp_path / "home" / "logs" / "events.jsonl"
    event_types = [_json.loads(l)["type"] for l in events_path.read_text().splitlines() if l]
    assert "saga_synthesis_skipped_boundary" in event_types, (
        "saga_synthesis_skipped_boundary must fire when the synthesis turn "
        "completes without calling saga_end_session"
    )


async def test_run_turn_no_synthesis_skipped_when_session_called(tmp_path: Path):
    """Complementary guard: if ``ctx.saga_end_session_called`` is True
    (the tool DID fire during the synthesis turn), the
    ``saga_synthesis_skipped_boundary`` event must NOT appear."""
    import json as _json

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="synthesis done")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)

    # Simulate the agent calling saga_end_session by patching the ctx after
    # it's created. We intercept the SessionManager.touch call to flip the flag.
    original_touch = agent._sessions.touch if agent._sessions else None

    # Patch run_turn to flip ctx.saga_end_session_called inside the turn.
    # Easiest: override the ctx creation path by patching TurnContext.
    from mimir.models import TurnContext
    original_init = TurnContext.__init__

    def _patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # Immediately mark it as called — simulates the tool firing.
        self.saga_end_session_called = True

    import unittest.mock as mock
    with mock.patch.object(TurnContext, "__init__", _patched_init):
        event = AgentEvent(
            trigger="saga_session_end", channel_id="ch-1",
            content="## Session synthesis\n",
        )
        await agent.run_turn(event)

    events_path = tmp_path / "home" / "logs" / "events.jsonl"
    event_types = [_json.loads(l)["type"] for l in events_path.read_text().splitlines() if l]
    assert "saga_synthesis_skipped_boundary" not in event_types, (
        "saga_synthesis_skipped_boundary must NOT fire when saga_end_session was called"
    )


# ── _rewrite_context_from_buffer ──────────────────────────────────────


async def _seed_buffer(
    buf: MessageBuffer, channel: str, items: list[tuple[str, str, str]],
) -> None:
    """Append ``(kind, author, content)`` triples to the buffer."""
    from mimir.history import Message
    from datetime import datetime, timezone
    for i, (kind, author, content) in enumerate(items):
        msg = Message(
            ts=datetime(2026, 5, 20, 12, 0, i, tzinfo=timezone.utc).isoformat(),
            msg_id=f"m-{i}",
            channel_id=channel,
            author=author,
            author_display=author,
            kind=kind,
            content=content,
            thread_id=None,
            source=channel,
        )
        await buf.append(msg)


@pytest.mark.asyncio
async def test_rewrite_context_from_buffer_maps_kind_to_role(tmp_path: Path):
    """Normal user/assistant messages become {role, content} dicts in
    arrival order. Pin the shape SagaStore.contextual_rewrite expects.
    """
    from mimir.agent import _rewrite_context_from_buffer

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl")
    await _seed_buffer(buf, "ch-1", [
        ("user_message",      "alice", "I just bought new Sony headphones"),
        ("assistant_message", "muninn", "Nice — the WH-1000XM6?"),
        ("user_message",      "alice", "yes, please save that"),
    ])
    ctx = _rewrite_context_from_buffer(buf, "ch-1")
    assert ctx == [
        {"role": "user",      "content": "I just bought new Sony headphones"},
        {"role": "assistant", "content": "Nice — the WH-1000XM6?"},
        {"role": "user",      "content": "yes, please save that"},
    ]


@pytest.mark.asyncio
async def test_rewrite_context_from_buffer_drops_system_notes(tmp_path: Path):
    """``system_note`` is algedonic-signal scaffolding, not a reference
    antecedent — must not poison the rewrite context."""
    from mimir.agent import _rewrite_context_from_buffer

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl")
    await _seed_buffer(buf, "ch-1", [
        ("system_note",       "system", "saga.feedback_sent (atom_count=3)"),
        ("user_message",      "alice", "tell me more about Italy"),
        ("system_note",       "system", "cost_rate_advisory"),
        ("assistant_message", "muninn", "Italy is varied — North vs South..."),
    ])
    ctx = _rewrite_context_from_buffer(buf, "ch-1")
    assert ctx == [
        {"role": "user",      "content": "tell me more about Italy"},
        {"role": "assistant", "content": "Italy is varied — North vs South..."},
    ]


@pytest.mark.asyncio
async def test_rewrite_context_from_buffer_all_system_notes_returns_none(tmp_path: Path):
    """When the whole window is system_note, return ``None`` so
    SagaStore.query short-circuits the rewrite LLM call.
    """
    from mimir.agent import _rewrite_context_from_buffer

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl")
    await _seed_buffer(buf, "ch-1", [
        ("system_note", "system", "saga.feedback_sent"),
        ("system_note", "system", "rate_limit_warning"),
        ("system_note", "system", "heartbeat_health_degraded"),
    ])
    ctx = _rewrite_context_from_buffer(buf, "ch-1")
    assert ctx is None


@pytest.mark.asyncio
async def test_rewrite_context_from_buffer_empty_buffer_returns_none(tmp_path: Path):
    """Empty channel → ``None`` (not an empty list). SagaStore.query
    treats ``None`` and ``[]`` differently if it ever does positional
    truthiness checks; pin the None contract explicitly.
    """
    from mimir.agent import _rewrite_context_from_buffer

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl")
    ctx = _rewrite_context_from_buffer(buf, "ch-1")
    assert ctx is None


@pytest.mark.asyncio
async def test_rewrite_context_from_buffer_drops_empty_content(tmp_path: Path):
    """Messages whose ``content`` is empty/whitespace are dropped — they
    can't anchor a reference. Mixed empty + populated must yield only
    the populated ones (NOT empty-string slots)."""
    from mimir.agent import _rewrite_context_from_buffer

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl")
    await _seed_buffer(buf, "ch-1", [
        ("user_message",      "alice", ""),
        ("user_message",      "alice", "   "),
        ("user_message",      "alice", "tell me about Italy"),
        ("assistant_message", "muninn", ""),
        ("assistant_message", "muninn", "Italy spans many regions."),
    ])
    ctx = _rewrite_context_from_buffer(buf, "ch-1")
    assert ctx == [
        {"role": "user",      "content": "tell me about Italy"},
        {"role": "assistant", "content": "Italy spans many regions."},
    ]
    # Nothing in the result has empty content.
    assert all(c["content"].strip() for c in ctx)


@pytest.mark.asyncio
async def test_rewrite_context_from_buffer_respects_window(tmp_path: Path):
    """The 10-msg window is what ``_REWRITE_CONTEXT_MESSAGES`` caps at —
    older entries don't leak in. Seed 15, expect the last 10 (in
    arrival order)."""
    from mimir.agent import _rewrite_context_from_buffer, _REWRITE_CONTEXT_MESSAGES

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl")
    items = [
        ("user_message", "alice", f"msg-{i}") for i in range(15)
    ]
    await _seed_buffer(buf, "ch-1", items)
    ctx = _rewrite_context_from_buffer(buf, "ch-1")
    assert ctx is not None
    # _REWRITE_CONTEXT_MESSAGES is 10 — only the last 10 survive.
    assert len(ctx) == _REWRITE_CONTEXT_MESSAGES == 10
    # And they are the most-recent 10, in chronological order.
    assert [c["content"] for c in ctx] == [f"msg-{i}" for i in range(5, 15)]


# ── _turn_matched_expected_tool_call ─────────────────────────────────


def test_turn_matched_expected_tool_call_bash_substring():
    """Bash tool with declared substring in its command satisfies."""
    from mimir.agent import _turn_matched_expected_tool_call

    markers = {
        "bash_substrings": ["gh pr review"],
        "tool_names": [],
        "signal_on_missing": "x",
    }
    events = [
        {"type": "tool_call", "name": "Bash",
         "args": {"command": "gh pr review 123 --approve --body 'lgtm'"}},
    ]
    assert _turn_matched_expected_tool_call(events, markers) is True


def test_turn_matched_expected_tool_call_mcp_tool_name():
    """A tool_call whose name is in the markers' tool_names satisfies
    even without a Bash substring match — covers the MCP path."""
    from mimir.agent import _turn_matched_expected_tool_call

    markers = {
        "bash_substrings": ["gh pr review"],
        "tool_names": ["pull_request_review_write"],
        "signal_on_missing": "x",
    }
    events = [
        {"type": "tool_call", "name": "pull_request_review_write", "args": {}},
    ]
    assert _turn_matched_expected_tool_call(events, markers) is True


def test_turn_matched_expected_tool_call_non_matching_bash_does_not_match():
    """``gh pr view`` shouldn't satisfy a marker that wants
    ``gh pr review``. Pin the substring discrimination."""
    from mimir.agent import _turn_matched_expected_tool_call

    markers = {
        "bash_substrings": ["gh pr review"],
        "tool_names": [],
        "signal_on_missing": "x",
    }
    events = [
        {"type": "tool_call", "name": "Bash",
         "args": {"command": "gh pr view 123"}},
    ]
    assert _turn_matched_expected_tool_call(events, markers) is False


def test_turn_matched_expected_tool_call_empty_or_invalid_markers():
    """Empty / None / non-dict markers → False (no expectation set,
    nothing to match against)."""
    from mimir.agent import _turn_matched_expected_tool_call

    events = [
        {"type": "tool_call", "name": "Bash",
         "args": {"command": "gh pr review 123"}},
    ]
    assert _turn_matched_expected_tool_call(events, {}) is False
    assert _turn_matched_expected_tool_call(events, None) is False
    assert _turn_matched_expected_tool_call(events, "not a dict") is False
    # Markers with both lists empty also → False.
    empty_markers = {"tool_names": [], "bash_substrings": []}
    assert _turn_matched_expected_tool_call(events, empty_markers) is False


def test_turn_matched_expected_tool_call_ignores_non_tool_call_events():
    """Reasoning events, tool_result events, etc. should NOT contribute
    to the match — only tool_call type."""
    from mimir.agent import _turn_matched_expected_tool_call

    markers = {
        "bash_substrings": ["gh pr review"],
        "tool_names": [],
        "signal_on_missing": "x",
    }
    events = [
        # A reasoning event mentioning the string shouldn't trigger.
        {"type": "reasoning",
         "content": "I should run gh pr review 123 next"},
        # A tool_result echoing the command shouldn't trigger either.
        {"type": "tool_result", "name": "Bash",
         "result": "gh pr review succeeded"},
    ]
    assert _turn_matched_expected_tool_call(events, markers) is False


def test_turn_matched_expected_tool_call_discriminates_review_from_review_comment():
    """``gh pr review-comment`` is a distinct GitHub-CLI subcommand for
    standalone review comments — it is NOT a review submission. The
    github-poller's marker uses ``"gh pr review "`` (trailing space)
    to discriminate from ``gh pr review-comment``. Pin the contract:
    a marker with the trailing-space substring must NOT match a Bash
    call to ``gh pr review-comment``. Mimir PR #236 review nit.
    """
    from mimir.agent import _turn_matched_expected_tool_call

    markers = {
        "bash_substrings": ["gh pr review "],
        "tool_names": [],
        "signal_on_missing": "x",
    }
    # NOT a submission — standalone comment subcommand.
    review_comment_events = [
        {"type": "tool_call", "name": "Bash",
         "args": {"command": "gh pr review-comment --body 'a thought'"}},
    ]
    assert _turn_matched_expected_tool_call(
        review_comment_events, markers,
    ) is False
    # IS a submission — real review with --approve.
    review_submit_events = [
        {"type": "tool_call", "name": "Bash",
         "args": {"command": "gh pr review 123 --approve --body 'lgtm'"}},
    ]
    assert _turn_matched_expected_tool_call(
        review_submit_events, markers,
    ) is True


# ── missed-submission detector: shell_exec + batched markers (#299 f/u) ──


def test_count_expected_tool_calls_matches_shell_exec_and_counts():
    """The shell tool is ``shell_exec`` (deepagents), not ``Bash`` — a
    ``gh pr review`` via shell_exec must count. Pre-fix only ``Bash``
    matched, so deepagents submissions were invisible and the
    missed-submission check false-fired (chainlink #299 follow-up).
    Also counts multiple submissions + MCP tool names."""
    from mimir.agent import _count_expected_tool_calls

    markers = {
        "bash_substrings": ["gh pr review "],
        "tool_names": ["pull_request_review_write"],
        "signal_on_missing": "poller_review_missed_submission",
    }
    events = [
        {"type": "tool_call", "name": "shell_exec",
         "args": {"command": "gh pr review 1 --approve"}},
        {"type": "tool_call", "name": "Bash",
         "args": {"command": "gh pr review 2 --approve"}},  # legacy runtime
        {"type": "tool_call", "name": "pull_request_review_write", "args": {}},
    ]
    assert _count_expected_tool_calls(events, markers) == 3
    # ``gh pr view`` (not review) doesn't count.
    assert _count_expected_tool_calls(
        [{"type": "tool_call", "name": "shell_exec",
          "args": {"command": "gh pr view 1"}}], markers,
    ) == 0


def test_expected_submission_markers_finds_top_level_and_items():
    """Markers are collected from the top level AND from per-item
    ``extra["items"]`` (poller batch shape). The per-item lookup is the
    core fix — poller events are always batch-wrapped, so a top-level-only
    read found nothing and the check never ran (chainlink #299 f/u)."""
    from mimir.agent import _expected_submission_markers

    m = {"bash_substrings": ["gh pr review "], "tool_names": [],
         "signal_on_missing": "poller_review_missed_submission"}
    assert _expected_submission_markers({"expected_tool_call": m}) == [m]
    batch = {"items": [
        {"event_type": "pr_opened", "expected_tool_call": m},
        {"event_type": "pr_review_requested", "expected_tool_call": m},
        {"event_type": "issue_comment"},  # non-review item — no marker
    ]}
    assert _expected_submission_markers(batch) == [m, m]
    assert _expected_submission_markers({}) == []
    assert _expected_submission_markers({"items": []}) == []
    assert _expected_submission_markers(None) == []  # type: ignore[arg-type]


async def test_run_turn_emits_missed_submission_for_unsubmitted_poller_review(
    tmp_path: Path,
):
    """End-to-end regression for the PR #522 false-approval: a poller
    review turn whose marker is in ``extra["items"]`` and that submits NO
    review must emit ``poller_review_missed_submission``. Pre-fix the
    top-level-only marker read skipped the check entirely, so a turn could
    claim "Approved on GitHub" without ever calling ``gh pr review`` and
    nothing flagged it (chainlink #299 follow-up)."""
    import json

    fake_agent = _FakeAgent(response_messages=[
        AIMessage(content="Approved PR #522 (but never ran gh pr review)"),
    ])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=_FakeSessionManager(),
    )
    marker = {
        "tool_names": ["pull_request_review_write"],
        "bash_substrings": ["gh pr review "],
        "signal_on_missing": "poller_review_missed_submission",
    }
    event = AgentEvent(
        trigger="poller",
        channel_id="poller:github-activity",
        content="Review PR #522 ...",
        source_id="poller:github-activity:1:batch:0",
        extra={
            "poller_name": "github-activity",
            "items": [
                {"event_type": "pr_opened", "repo": "o/r", "number": 522,
                 "expected_tool_call": marker},
            ],
        },
    )
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    missed = [e for e in evs if e.get("type") == "poller_review_missed_submission"]
    assert len(missed) == 1, (
        f"expected the missed-submission signal; got {[e.get('type') for e in evs]}"
    )
    assert missed[0]["expected"] == 1
    assert missed[0]["submitted"] == 0


async def test_early_phase_poller_crash_still_emits_turn_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """chainlink #306: a crash in the EARLY phase of a poller turn (prompt
    build / agent construction — before the model-loop try/except that emits
    turn_failed) must still emit turn_failed, so poller-recovery doesn't leak
    the in-flight item forever. The model-loop path is covered by
    test_run_turn_emits_turn_failed_event_on_error; this covers the
    pre-model-loop path, where the exception propagates out of
    _run_turn_body."""
    import json

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="x")])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=None,
        session_manager=_FakeSessionManager(),
    )

    async def _boom(*a, **k):
        raise RuntimeError("prompt build exploded")

    monkeypatch.setattr(agent, "_build_turn_prompt", _boom)
    event = AgentEvent(
        trigger="poller", channel_id="poller:gmail", content="x",
        source_id="poller:gmail:1:batch:0",
        extra={"poller_name": "gmail", "items": [{"event_type": "x", "number": 1}]},
    )
    with pytest.raises(RuntimeError, match="prompt build exploded"):
        await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    failed = [e for e in evs if e.get("type") == "turn_failed"]
    assert len(failed) == 1, (
        f"expected a turn_failed from the early-phase crash; "
        f"got {[e.get('type') for e in evs]}"
    )
    assert failed[0]["trigger"] == "poller"
    assert failed[0].get("phase") == "pre_model_loop"
    assert failed[0].get("source_id") == "poller:gmail:1:batch:0"


def test_expected_submission_markers_dedupes_top_level_when_items_present():
    """chainlink #308 (finding #38): a top-level marker on a BATCH event is
    the batch's shared declaration, NOT an extra Nth item — per-item markers
    are authoritative, so ``expected`` isn't double-counted."""
    from mimir.agent import _expected_submission_markers

    m_top = {"bash_substrings": ["gh pr review "], "signal_on_missing": "x"}
    m_item = {"bash_substrings": ["gh pr review 1"], "signal_on_missing": "x"}
    batch = {"expected_tool_call": m_top, "items": [
        {"number": 1, "expected_tool_call": m_item},
        {"number": 2, "expected_tool_call": m_item},
    ]}
    # Per-item wins; the top-level marker is NOT added as a 3rd entry.
    assert _expected_submission_markers(batch) == [m_item, m_item]


async def test_no_missed_submission_signal_on_failed_poller_turn(tmp_path: Path):
    """chainlink #308 (finding #22): a FAILED poller review turn already emits
    turn_failed; the missed-submission check must NOT also fire (the review
    obviously wasn't submitted because the turn died — double-signal noise)."""
    import json

    class _BoomAgent:
        async def ainvoke(self, *a, **kw):
            raise RuntimeError("codex 503 boom")

        async def astream(self, *a, **kw):
            raise RuntimeError("codex 503 boom")
            yield  # unreachable — makes this an async generator

    agent = _build_agent(tmp_path, fake_agent=_BoomAgent())  # type: ignore[arg-type]
    marker = {
        "tool_names": [], "bash_substrings": ["gh pr review "],
        "signal_on_missing": "poller_review_missed_submission",
    }
    event = AgentEvent(
        trigger="poller", channel_id="poller:gh", content="review",
        source_id="sid-1",
        extra={"poller_name": "gh",
               "items": [{"number": 5, "expected_tool_call": marker}]},
    )
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    assert [e for e in evs if e.get("type") == "turn_failed"], "failure should be emitted"
    assert [e for e in evs if e.get("type") == "poller_review_missed_submission"] == []


async def test_run_turn_folds_injected_skill_atoms_into_record_no_autofeedback(
    tmp_path: Path, monkeypatch
):
    """slice 6 integration: skill-learning atom IDs recorded onto the turn
    ctx during prompt build (poller auto_skill_block / non-poller middleware)
    must land in the TurnRecord's saga_atom_ids — so the session-boundary
    synthesis turn can vote them — WITHOUT triggering the (removed) per-turn
    auto-feedback."""
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="done")])
    fake_saga = _FakeSaga(query_hits=[
        {"atom_id": "a" * 16, "content": "prior memory", "stream": "semantic"},
    ])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)

    skill_atom = "s" * 16
    orig = agent._build_turn_prompt

    async def _patched(ctx, event, **kw):
        # Stand in for the injection sites populating the turn ctx.
        ctx.injected_skill_atom_ids.append(skill_atom)
        return await orig(ctx, event, **kw)

    monkeypatch.setattr(agent, "_build_turn_prompt", _patched)

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hello")
    record = await agent.run_turn(event)

    # Skill atom folded into the record for synthesis voting...
    assert skill_atom in record.saga_atom_ids
    # ...alongside the pre-injected retrieval atom.
    assert "a" * 16 in record.saga_atom_ids
    # ...but NOT auto-credited (per-turn feedback removed).
    assert fake_saga.feedback_calls == []


async def test_run_turn_arms_injection_before_saga_setup(tmp_path: Path):
    """chainlink #383 facet 2: a follow-up arriving during slow setup (SAGA
    query/prompt assembly), after dispatcher marks the channel in-flight but
    before the first model boundary, is accepted into the active turn instead
    of falling back to a queued next turn."""
    _mti._REGISTRY.clear()
    fake_saga = _BarrierSaga()
    fake_agent = _BoundaryFakeAgent(response_messages=[AIMessage(content="done")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="first")
    task = asyncio.create_task(agent.run_turn(event))
    await asyncio.wait_for(fake_saga.started.wait(), timeout=1.0)

    assert _mti.inject_message(
        "ch-1",
        AgentEvent(trigger="user_message", channel_id="ch-1", content="during setup"),
    ) == "injected"

    fake_saga.release.set()
    record = await asyncio.wait_for(task, timeout=2.0)

    assert record.injected_inputs
    assert "during setup" in record.injected_inputs[0]["text"]


async def test_run_turn_drains_startup_queued_followups(tmp_path: Path):
    """chainlink #383 facet 1: user messages already queued behind the current
    event at turn start are folded into the starting turn and do not remain in
    the dispatcher queue as separate follow-up turns."""
    from mimir.dispatcher import Dispatcher

    _mti._REGISTRY.clear()
    fake_agent = _BoundaryFakeAgent(response_messages=[AIMessage(content="done")])

    disp = Dispatcher(
        replace(_make_config(tmp_path / "home"), midturn_injection_channels=("ch-",)),
        None,
    )
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)
    agent._dispatcher = disp

    q = disp._queues["ch-1"] = asyncio.Queue()
    await q.put(
        AgentEvent(
            trigger="user_message",
            channel_id="ch-1",
            content="queued followup",
        ),
    )

    record = await agent.run_turn(
        AgentEvent(trigger="user_message", channel_id="ch-1", content="first"),
    )

    assert "queued followup" in record.injected_inputs[0]["text"]
    assert q.qsize() == 0
    await asyncio.wait_for(q.join(), timeout=1.0)


async def test_run_turn_does_not_drain_startup_followups_for_non_user_turn(
    tmp_path: Path,
):
    """Startup-queued user messages must not be folded into non-user turns.

    A saga_session_end/react/shell-job turn may be silent or non-conversational;
    absorbing a queued user message there would acknowledge it nowhere.
    """
    from mimir.dispatcher import Dispatcher

    _mti._REGISTRY.clear()
    fake_agent = _BoundaryFakeAgent(response_messages=[AIMessage(content="done")])

    disp = Dispatcher(
        replace(_make_config(tmp_path / "home"), midturn_injection_channels=("ch-",)),
        None,
    )
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)
    agent._dispatcher = disp

    q = disp._queues["ch-1"] = asyncio.Queue()
    await q.put(
        AgentEvent(
            trigger="user_message",
            channel_id="ch-1",
            content="queued followup",
        ),
    )

    record = await agent.run_turn(
        AgentEvent(
            trigger="saga_session_end",
            channel_id="ch-1",
            content="summarize session",
            extra={"saga_session_id": "saga-test"},
        ),
    )

    assert record.injected_inputs == []
    assert q.qsize() == 1
    assert q.get_nowait().content == "queued followup"
    q.task_done()


async def test_run_turn_non_user_turn_does_not_arm_mid_turn_injection(
    tmp_path: Path,
):
    """chainlink #385: non-user same-channel turns must not arm the registry.

    The dispatcher may mark a saga_session_end/react/shell-job turn in-flight,
    but enqueue-time folding still depends on run_turn registering an active
    mid-turn injection entry. For non-user turns, inject_message must see no
    active turn and fall back to normal queueing.
    """

    class _InjectionProbeAgent(_FakeAgent):
        def __init__(self) -> None:
            super().__init__([AIMessage(content="done")])
            self.inject_result: str | None = None

        async def astream(
            self,
            state: dict[str, Any],
            *,
            config: dict[str, Any],
            stream_mode: str = "values",
        ):
            self.inject_result = _mti.inject_message(
                config["configurable"]["channel_id"],
                AgentEvent(
                    trigger="user_message",
                    channel_id=config["configurable"]["channel_id"],
                    content="during synthesis",
                ),
            )
            async for chunk in super().astream(
                state, config=config, stream_mode=stream_mode,
            ):
                yield chunk

    _mti._REGISTRY.clear()
    fake_agent = _InjectionProbeAgent()
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)

    record = await agent.run_turn(
        AgentEvent(
            trigger="saga_session_end",
            channel_id="ch-1",
            content="summarize session",
            extra={"saga_session_id": "saga-test"},
        ),
    )

    assert fake_agent.inject_result == "no_active_turn"
    assert record.injected_inputs == []
    assert _mti._drain("ch-1") == []


async def test_run_turn_early_armed_injection_deactivates_on_setup_error(
    tmp_path: Path,
):
    """chainlink #383 watch item: arming the injection registry before setup
    must still clean up if setup fails before the model-loop finally runs."""
    _mti._REGISTRY.clear()
    agent = _build_agent(
        tmp_path,
        fake_agent=_FakeAgent(response_messages=[AIMessage(content="unused")]),
        fake_saga=None,
    )

    async def failing_body(*_args: object, **_kwargs: object):
        assert _mti.inject_message(
            "ch-1",
            AgentEvent(
                trigger="user_message",
                channel_id="ch-1",
                content="accepted before setup crash",
            ),
        ) == "injected"
        raise RuntimeError("setup exploded")

    agent._run_turn_body = failing_body  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="setup exploded"):
        await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="ch-1", content="first"),
        )

    assert _mti.inject_message(
        "ch-1",
        AgentEvent(trigger="user_message", channel_id="ch-1", content="later"),
    ) == "no_active_turn"


async def test_run_turn_bounds_hung_finalize_hook(tmp_path: Path, monkeypatch):
    """chainlink #389: a hung finalize hook is bounded by
    post_turn_timeout_seconds so it can't hold the dispatcher worker (and thus
    the channel) forever. run_turn still returns and the record is written —
    post-loop work runs OUTSIDE the model-loop timeout, so without the bound a
    hook hang would wedge the turn indefinitely."""
    import asyncio
    monkeypatch.setenv("MIMIR_POST_TURN_TIMEOUT_SECONDS", "2")
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)

    class _HangFinalizeHook:
        async def finalize(self, ctx, event, record):
            await asyncio.Event().wait()  # never returns

    agent._hooks.append(_HangFinalizeHook())
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")

    # Must return well under the guard despite the hung hook (would hang forever
    # pre-fix). The TurnRecord is still produced (written before finalize).
    record = await asyncio.wait_for(agent.run_turn(event), timeout=15.0)
    assert record is not None
    assert record.output == "ok"


async def test_run_turn_setup_phase_exception_releases_cleanup(tmp_path: Path):
    """chainlink #415: the setup phase (session touch, wiki snapshot,
    contextvar arming) runs INSIDE run_turn's cleanup try. A setup-phase
    exception must deactivate the mid-turn-injection registry entry
    (requeuing anything already injected) and leave no leaked turn
    context — previously the window between register_inflight and the
    model-loop try escaped the finally entirely."""
    from mimir import mid_turn_injection
    from mimir._context import get_current_turn
    from mimir.channel_registry import ChannelRegistry

    fake_agent = _FakeAgent(response_messages=[AIMessage(content="unreached")])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga())
    agent._channels = registry  # type: ignore[attr-defined]

    # Blow up mid-setup — after injection arming + typing start, before
    # the model loop (the previously-uncovered window).
    def _boom():
        raise RuntimeError("setup boom")
    agent._snapshot_wiki_mtimes = _boom  # type: ignore[assignment]

    event = AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hi", author="jason", source="discord",
    )
    with pytest.raises(RuntimeError, match="setup boom"):
        await agent.run_turn(event)

    # The injection-registry entry was deactivated (a fresh inject sees
    # no active turn rather than queueing into a stale entry).
    assert mid_turn_injection.inject_message("ch-1", event) == "no_active_turn"
    # No leaked turn context (token reset despite the mid-setup raise).
    assert get_current_turn() is None


async def test_run_turn_cross_channel_only_delivery_still_flags(tmp_path: Path):
    """chainlink #423: the forgot-to-send guard is channel-scoped. A turn
    whose only confirmed delivery went to a DIFFERENT channel (e.g. an
    ops-channel alert) left the asking user in silence — the signal fires,
    carrying delivered_elsewhere so feedback can distinguish
    answered-in-the-wrong-room from totally silent."""
    import json
    from mimir.channel_registry import ChannelRegistry

    class _CrossChannelAgent(_FakeAgent):
        async def astream(self, state, *, config, stream_mode="values"):
            from mimir._context import get_current_turn
            _ctx = get_current_turn()
            if _ctx is not None:
                _ctx.send_message_count += 1
                _ctx.delivered_channel_ids.add("ops-channel")  # not ch-1
            async for chunk in super().astream(
                state, config=config, stream_mode=stream_mode,
            ):
                yield chunk

    fake_agent = _CrossChannelAgent(response_messages=[AIMessage(content="alerted ops")])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga())
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    await agent.run_turn(event)

    events_log = tmp_path / "home" / "logs" / "events.jsonl"
    evs = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    [sig] = [e for e in evs if e.get("type") == "interactive_turn_no_send_message"]
    assert sig["channel_id"] == "ch-1"
    assert sig["delivered_elsewhere"] == ["ops-channel"]


def test_resolve_model_claude_code_deprecated_by_default(monkeypatch):
    """chainlink #426: the claude-code subprocess route bypasses the tool
    budget + prohibited-action gating, is unused by live deployments, and
    is deprecated — _resolve_model refuses it unless the operator opts in
    via MIMIR_ALLOW_CLAUDE_CODE."""
    from mimir.agent import _resolve_model

    monkeypatch.delenv("MIMIR_ALLOW_CLAUDE_CODE", raising=False)
    with pytest.raises(RuntimeError, match="deprecated"):
        _resolve_model("claude-code:claude-sonnet-4-6")


def test_resolve_model_claude_code_override_passes_gate(monkeypatch):
    """With the opt-in set, the gate steps aside — resolution proceeds to
    the normal import path (ImportError in envs without the fork, never
    the deprecation RuntimeError)."""
    from mimir.agent import _resolve_model

    monkeypatch.setenv("MIMIR_ALLOW_CLAUDE_CODE", "1")
    try:
        _resolve_model("claude-code:claude-sonnet-4-6")
    except RuntimeError as exc:
        pytest.fail(f"gate fired despite override: {exc}")
    except ImportError:
        pass  # fork not installed in this env — expected past the gate


def test_resolve_model_claude_code_scaffold_opt_in(tmp_path, monkeypatch):
    """chainlink #426 (review follow-up): Config.from_env reads os.environ
    only, so 'mimir setup --subscription' writing MIMIR_ALLOW_CLAUDE_CODE=1
    into <home>/.env never reaches a bare 'mimir run' through env. The gate
    consumes the scaffold line directly — setup-written operator intent for
    this home — so the supported Max quickstart works without a shell
    export. Env absent + no scaffold still refuses."""
    from mimir.agent import _resolve_model

    monkeypatch.delenv("MIMIR_ALLOW_CLAUDE_CODE", raising=False)

    # No scaffold → still refused.
    with pytest.raises(RuntimeError, match="deprecated"):
        _resolve_model("claude-code:claude-sonnet-4-6", home=tmp_path)

    # Scaffolded opt-in (what setup writes for claude-code routes) → gate
    # steps aside; resolution proceeds to the import path.
    (tmp_path / ".env").write_text(
        "MIMIR_MODEL_SPEC=claude-code:claude-sonnet-4-6\n"
        "MIMIR_ALLOW_CLAUDE_CODE=1\n",
        encoding="utf-8",
    )
    try:
        _resolve_model("claude-code:claude-sonnet-4-6", home=tmp_path)
    except RuntimeError as exc:
        pytest.fail(f"gate fired despite scaffolded opt-in: {exc}")
    except ImportError:
        pass  # fork not installed — expected past the gate
