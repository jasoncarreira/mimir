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

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

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
        a single final yield is sufficient — the streaming state machine
        in mimir._streaming_dispatch handles single-chunk inputs cleanly."""
        self.invocations.append({"state": state, "config": config})
        yield {"messages": list(state.get("messages") or []) + self._response_messages}


class _BridgeStub:
    """Captures send + cancel_typing calls so tests can assert on
    the end-of-turn dispatch path's three branches."""

    name = "stub"
    prefixes = ("ch-",)

    def __init__(self) -> None:
        self.sends: list[tuple[str, str, bool]] = []
        self.cancels: list[str] = []

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
        pass

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

    async def query(self, content: str, *, top_k: int = 12, session_id: str | None = None):
        self.query_calls.append(
            {"content": content, "top_k": top_k, "session_id": session_id},
        )
        return {"atoms": self._hits, "triples": []}

    async def feedback(self, atom_ids, output, *, session_id=None, feedback="positive"):
        self.feedback_calls.append({
            "atom_ids": list(atom_ids), "output": output,
            "session_id": session_id, "feedback": feedback,
        })


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


async def test_run_turn_cancels_typing_when_plan_streamed_but_result_empty(
    tmp_path: Path,
):
    """Edge case from review: if streamed_plan=True but result_text()
    is empty (model called tools then said nothing), the typing
    indicator is held by ``final=False`` and would dangle until the
    bridge's auto-expire kicks in. The end-of-turn path must call
    ``bridge.cancel_typing`` to release it explicitly.
    """
    from mimir.channel_registry import ChannelRegistry

    # AIMessage sequence: plan text → tool_call → nothing (empty AIMessage)
    # → ensures streaming flushes a plan but result_text() is empty.
    fake_agent = _FakeAgent(response_messages=[
        AIMessage(
            content="planning...",
            tool_calls=[
                {"name": "memory_query", "args": {"query": "x"}, "id": "t1"},
            ],
        ),
        AIMessage(content=""),  # no final result text
    ])
    bridge = _BridgeStub()
    registry = ChannelRegistry()
    registry.register(bridge)  # type: ignore[arg-type]

    agent = _build_agent(tmp_path, fake_agent=fake_agent)
    agent._channels = registry  # type: ignore[attr-defined]

    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    record = await agent.run_turn(event)

    # The plan was streamed mid-turn (final=False) but no result text
    # followed — so cancel_typing fires to release the indicator.
    plan_sends = [s for s in bridge.sends if s[2] is False]
    final_sends = [s for s in bridge.sends if s[2] is True]
    assert len(plan_sends) == 1
    assert "planning" in plan_sends[0][1]
    # No end-of-turn send (no result text to ship).
    assert final_sends == []
    # But typing indicator was released.
    assert bridge.cancels == ["ch-1"]
    # Turn record still wrote cleanly.
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


async def test_run_turn_appends_outbound_via_fallback_bridge_send(tmp_path: Path):
    """When the end-of-turn fallback ``bridge.send`` fires, the sent
    text must be appended to the chat-history buffer as an
    ``assistant_message`` so the agent's next turn sees its own reply
    in Recent activity. Mirrors what the SDK-era post-hook did before
    PR #181."""

    class _CapturingBridge:
        name = "fake"
        def __init__(self) -> None:
            self.sends: list[tuple[str, str, bool]] = []

        async def send_typing_indicator(self, *a, **kw):
            return None

        async def cancel_typing(self, *a, **kw):
            return None

        async def send(self, channel_id, text, attachment_paths=None, *, final=True):
            self.sends.append((channel_id, text, final))
            class _R:
                sent = True
                message_id = "msg-out-42"
            return _R()

    class _Channels:
        def __init__(self, bridge):
            self._bridge = bridge

        def find(self, channel_id):
            return self._bridge

    bridge = _CapturingBridge()
    fake_agent = _FakeAgent(response_messages=[
        AIMessage(content="here is my reply"),
    ])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )
    agent._channels = _Channels(bridge)  # type: ignore[attr-defined]

    event = AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hi", author="jason", source="discord",
    )
    record = await agent.run_turn(event)
    assert record.error is None
    # Bridge actually sent the reply (via the agent fallback path,
    # since this fake agent doesn't trigger streaming).
    assert ("ch-1", "here is my reply", True) in bridge.sends

    # The outbound must be in the buffer.
    msgs = list(agent._buffer._all)
    outbound = [m for m in msgs if m.kind == "assistant_message"]
    assert len(outbound) == 1, (
        f"expected 1 assistant_message; got "
        f"{[(m.kind, m.content[:30]) for m in msgs]}"
    )
    assert outbound[0].content == "here is my reply"
    assert outbound[0].channel_id == "ch-1"
    assert outbound[0].msg_id == "msg-out-42"


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


async def test_outbound_buffer_append_runs_even_when_bridge_send_fails(
    tmp_path: Path,
):
    """Pre-#181's ``_auto_dispatch_or_record``: "Always writes to
    chat_history regardless of dispatch outcome — so Recent activity
    reflects what the agent said even when delivery failed (the
    agent self-corrects when it sees a stale conversation that
    doesn't match what it thought it sent)."

    Verify the append fires even when ``bridge.send`` raises.
    """

    class _ExplodingBridge:
        name = "fake"
        async def send_typing_indicator(self, *a, **kw):
            return None
        async def cancel_typing(self, *a, **kw):
            return None
        async def send(self, *a, **kw):
            raise RuntimeError("network down")

    class _Channels:
        def __init__(self, bridge):
            self._bridge = bridge
        def find(self, channel_id):
            return self._bridge

    bridge = _ExplodingBridge()
    fake_agent = _FakeAgent(response_messages=[
        AIMessage(content="reply that won't reach the user"),
    ])
    agent = _build_agent(
        tmp_path, fake_agent=fake_agent, fake_saga=_FakeSaga(),
    )
    agent._channels = _Channels(bridge)  # type: ignore[attr-defined]

    event = AgentEvent(
        trigger="user_message", channel_id="ch-1",
        content="hi", author="jason", source="discord",
    )
    record = await agent.run_turn(event)
    # run_turn doesn't fail just because bridge.send did
    assert record.error is None
    # ...and the outbound IS in the buffer for the agent to reconcile.
    msgs = list(agent._buffer._all)
    outbound = [m for m in msgs if m.kind == "assistant_message"]
    assert len(outbound) == 1
    assert outbound[0].content == "reply that won't reach the user"
    # msg_id is None when send failed (no SendResult to read from)
    assert outbound[0].msg_id is None


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
