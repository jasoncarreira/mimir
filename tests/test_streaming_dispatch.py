"""181-O regression: streaming auto-dispatcher state machine + bridge wiring.

Tests the pure state-machine logic (no async/IO) and the
``StreamingAutoDispatcher`` integration with a fake bridge.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mimir._streaming_dispatch import (
    EXPLICIT_SEND_TOOL,
    StreamingAutoDispatcher,
    StreamingState,
    _split_blocks,
    advance_state,
    intermediate_text_segments,
)


def _ai(text: str = "", tool_calls: list[dict] | None = None) -> AIMessage:
    """Build an AIMessage; tool_calls default to []."""
    return AIMessage(content=text, tool_calls=tool_calls or [])


# ─── _split_blocks ─────────────────────────────────────────────────


class TestSplitBlocks:
    def test_string_content(self) -> None:
        text, tcs = _split_blocks(_ai("hello"))
        assert text == "hello"
        assert tcs == []

    def test_list_of_text_blocks(self) -> None:
        msg = AIMessage(content=[
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ])
        text, tcs = _split_blocks(msg)
        assert text == "part1\npart2"

    def test_includes_native_tool_calls(self) -> None:
        msg = _ai("hi", tool_calls=[{"name": "foo", "args": {}, "id": "t1"}])
        text, tcs = _split_blocks(msg)
        assert text == "hi"
        assert len(tcs) == 1
        assert tcs[0]["name"] == "foo"

    def test_folds_internal_tool_calls_from_metadata(self) -> None:
        msg = AIMessage(content="x", tool_calls=[])
        msg.response_metadata = {
            "internal_tool_calls": [{"name": "bar", "args": {}, "id": "t2"}],
        }
        _text, tcs = _split_blocks(msg)
        assert any(tc["name"] == "bar" for tc in tcs)


# ─── advance_state — happy path ────────────────────────────────────


class TestAdvanceStateHappyPath:
    def test_text_only_in_pre_tool_accumulates(self) -> None:
        s = StreamingState()
        assert advance_state(s, _ai("plan part 1")) is None
        assert advance_state(s, _ai("plan part 2")) is None
        assert s.plan_text() == "plan part 1\nplan part 2"
        assert s.phase == "pre_tool"

    def test_first_tool_call_flushes_plan_and_transitions(self) -> None:
        s = StreamingState()
        advance_state(s, _ai("about to call a tool"))
        plan = advance_state(s, _ai(
            "calling now",
            tool_calls=[{"name": "memory_query", "args": {"query": "x"}, "id": "t1"}],
        ))
        # Plan flush returned (mixed-message text appended before tool_use).
        assert plan == "about to call a tool\ncalling now"
        assert s.phase == "post_tool"

    def test_post_tool_text_buffers_as_candidate_result(self) -> None:
        s = StreamingState(phase="post_tool")
        advance_state(s, _ai("the answer is 42"))
        assert s.result_text() == "the answer is 42"

    def test_intermediate_tool_demotes_candidate_to_suppressed(self) -> None:
        s = StreamingState(phase="post_tool")
        advance_state(s, _ai("interim thought"))
        # Another tool call — interim text was NOT result.
        advance_state(s, _ai(
            "calling another tool",
            tool_calls=[{"name": "file_search", "args": {}, "id": "t2"}],
        ))
        assert s.result_text() == ""
        suppressed = s.suppressed_text()
        assert "interim thought" in suppressed
        assert "calling another tool" in suppressed

    def test_final_text_after_last_tool_is_result(self) -> None:
        s = StreamingState(phase="pre_tool")
        # Plan → first tool → intermediate text → second tool → result
        advance_state(s, _ai("plan"))
        advance_state(s, _ai("", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}]))
        advance_state(s, _ai("interim"))
        advance_state(s, _ai("", tool_calls=[{"name": "file_search", "args": {}, "id": "t2"}]))
        advance_state(s, _ai("here is the final answer"))
        assert s.result_text() == "here is the final answer"
        assert "interim" in s.suppressed_text()


# ─── advance_state — disable paths ────────────────────────────────


class TestAdvanceStateDisable:
    def test_explicit_send_message_disables_streaming(self) -> None:
        s = StreamingState()
        advance_state(s, _ai("about to ship"))
        plan = advance_state(s, _ai(
            "",
            tool_calls=[{"name": EXPLICIT_SEND_TOOL, "args": {"text": "hi"}, "id": "t1"}],
        ))
        # No plan flush — explicit send wins.
        assert plan is None
        assert s.disabled_by_explicit_send is True
        # Plan + candidate buffers cleared so nothing leaks downstream.
        assert s.plan_text() == ""
        assert s.result_text() == ""

    def test_namespaced_send_message_also_disables_streaming(self) -> None:
        """Regression for the 50-q bluesky bench finding: under the
        ``claude-code:*`` provider, langchain-claude-code's MCP bridge
        renames our @tool to ``mcp__langchain-tools__send_message``.
        Pre-fix the bare-string match missed it, streaming dispatch
        stayed enabled, plan flushed mid-turn for every probe
        (double-send risk). Now match either form."""
        for ns_name in (
            "mcp__langchain-tools__send_message",
            "mcp__mimir__send_message",
            "some_future_wrapper__send_message",
        ):
            s = StreamingState()
            advance_state(s, _ai("planning"))
            plan = advance_state(s, _ai(
                "",
                tool_calls=[{"name": ns_name, "args": {"text": "hi"}, "id": "t1"}],
            ))
            assert plan is None, f"plan flushed despite namespaced send: {ns_name}"
            assert s.disabled_by_explicit_send is True, (
                f"streaming should disable on {ns_name}"
            )

    def test_unrelated_namespaced_tool_does_not_disable(self) -> None:
        """Sanity check on the suffix-match: only tool names ending in
        ``__send_message`` (or the bare name) trigger the disable.
        ``mcp__langchain-tools__memory_query`` doesn't."""
        s = StreamingState()
        advance_state(s, _ai("planning"))
        plan = advance_state(s, _ai(
            "calling tool",
            tool_calls=[{"name": "mcp__langchain-tools__memory_query",
                         "args": {}, "id": "t1"}],
        ))
        # This is a regular tool call → plan flushes, streaming stays on.
        assert plan == "planning\ncalling tool"
        assert s.disabled_by_explicit_send is False

    def test_disabled_state_returns_none_for_subsequent_messages(self) -> None:
        s = StreamingState(disabled_by_explicit_send=True)
        assert advance_state(s, _ai("more text")) is None
        assert advance_state(s, _ai("", tool_calls=[{"name": "x", "args": {}, "id": "y"}])) is None

    def test_non_aimessage_returns_none(self) -> None:
        s = StreamingState()
        assert advance_state(s, HumanMessage(content="user")) is None
        assert advance_state(s, ToolMessage(content="ok", tool_call_id="t1")) is None

    def test_disabled_state_returns_none(self) -> None:
        s = StreamingState(enabled=False)
        assert advance_state(s, _ai("would have been plan")) is None
        assert s.plan_text() == ""


# ─── Observed-count bookkeeping ─────────────────────────────────


class TestObservedCount:
    def test_observed_count_increments_per_call(self) -> None:
        """Dedupe is the caller's responsibility — agent.py slices the
        cumulative messages list past ``observed_count`` so the same
        AIMessage isn't fed in twice across astream re-emissions."""
        s = StreamingState()
        advance_state(s, _ai("first"))
        advance_state(s, _ai("second"))
        advance_state(s, _ai("third"))
        assert s.observed_count == 3
        assert "first" in s.plan_text()
        assert "second" in s.plan_text()
        assert "third" in s.plan_text()


# ─── intermediate_text_segments ──────────────────────────────────


class TestIntermediateTextSegments:
    def test_no_tool_calls_returns_empty(self) -> None:
        msgs = [_ai("just text"), _ai("more text")]
        assert intermediate_text_segments(msgs) == []

    def test_single_tool_call_returns_empty(self) -> None:
        # First==last; no intermediate window.
        msgs = [
            _ai("plan"),
            _ai("calling", tool_calls=[{"name": "x", "args": {}, "id": "t1"}]),
            _ai("result"),
        ]
        assert intermediate_text_segments(msgs) == []

    def test_text_between_tool_calls_is_intermediate(self) -> None:
        msgs = [
            _ai("plan"),
            _ai("", tool_calls=[{"name": "x", "args": {}, "id": "t1"}]),
            _ai("interim text"),
            _ai("", tool_calls=[{"name": "y", "args": {}, "id": "t2"}]),
            _ai("final"),
        ]
        intermediate = intermediate_text_segments(msgs)
        assert "interim text" in intermediate


# ─── StreamingAutoDispatcher (bridge integration) ─────────────────


class _FakeSendResult:
    def __init__(self, sent: bool = True) -> None:
        self.sent = sent
        self.error: str | None = None


class _FakeBridge:
    name = "fake"

    def __init__(self) -> None:
        self.sends: list[tuple[str, str, bool]] = []
        self.next_result_sent = True

    async def send(
        self, channel_id: str, text: str, *, final: bool = True,
        attachment_paths=None,
    ):
        self.sends.append((channel_id, text, final))
        return _FakeSendResult(sent=self.next_result_sent)


@pytest.mark.asyncio
async def test_dispatcher_flushes_plan_on_first_tool_call() -> None:
    bridge = _FakeBridge()
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)
    await d.observe(_ai("here is the plan"))
    await d.observe(_ai(
        "", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}],
    ))
    # One send fired with final=False so the typing indicator stays held.
    assert len(bridge.sends) == 1
    assert bridge.sends[0] == ("ch-1", "here is the plan", False)
    assert d.streamed_plan is True


@pytest.mark.asyncio
async def test_dispatcher_does_not_flush_when_bridge_returns_unsent() -> None:
    """Bridge said sent=False — streamed_plan stays False so turns.jsonl
    won't claim text was suppressed when nothing reached the user."""
    bridge = _FakeBridge()
    bridge.next_result_sent = False
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)
    await d.observe(_ai("planning"))
    await d.observe(_ai(
        "", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}],
    ))
    assert d.streamed_plan is False


@pytest.mark.asyncio
async def test_dispatcher_eligibility_gates_bench_bridge() -> None:
    """BenchBridge skips streaming — no mid-turn user-visible UX
    to optimize for in the bench harness."""
    class _Bench(_FakeBridge):
        name = "bench"

    bridge = _Bench()
    d = StreamingAutoDispatcher(channel_id="bench-1", bridge=bridge)
    assert d.enabled is False
    await d.observe(_ai("plan"))
    await d.observe(_ai("", tool_calls=[{"name": "x", "args": {}, "id": "t1"}]))
    assert bridge.sends == []


@pytest.mark.asyncio
async def test_dispatcher_no_bridge_is_a_no_op() -> None:
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=None)
    assert d.enabled is False
    # Must not raise even though there's no bridge to send to.
    await d.observe(_ai("plan"))
    await d.observe(_ai("", tool_calls=[{"name": "x", "args": {}, "id": "t1"}]))


@pytest.mark.asyncio
async def test_dispatcher_explicit_send_message_disables_streaming() -> None:
    bridge = _FakeBridge()
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)
    await d.observe(_ai("plan"))
    await d.observe(_ai(
        "",
        tool_calls=[{"name": EXPLICIT_SEND_TOOL, "args": {"text": "ship"}, "id": "t1"}],
    ))
    # No mid-turn flush; the explicit send_message is the canonical
    # delivery and the run_turn end-of-turn path also skips
    # (caller checks disabled_by_explicit_send).
    assert bridge.sends == []
    assert d.disabled_by_explicit_send is True
    assert d.streamed_plan is False


@pytest.mark.asyncio
async def test_dispatcher_result_and_suppressed_after_full_turn() -> None:
    bridge = _FakeBridge()
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)
    # Plan → first tool → interim → second tool → result
    await d.observe(_ai("plan part"))
    await d.observe(_ai("", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}]))
    await d.observe(_ai("interim notes"))
    await d.observe(_ai("", tool_calls=[{"name": "file_search", "args": {}, "id": "t2"}]))
    await d.observe(_ai("final reply"))
    assert d.streamed_plan is True
    assert d.result_text() == "final reply"
    assert "interim notes" in d.suppressed_text()
    # One mid-turn send (the plan); the result is the caller's job
    # to flush at end of turn.
    assert len(bridge.sends) == 1


# ─── Action-directive stripping (pre-#181 parity) ─────────────────


class _ReactingBridge(_FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.reacts: list[tuple[str, str | None, str]] = []

    async def react(self, channel_id: str, message_id, emoji: str):
        self.reacts.append((channel_id, message_id, emoji))
        return True


@pytest.mark.asyncio
async def test_dispatcher_strips_actions_markup_before_bridge_send() -> None:
    """Pre-#181 dispatcher parsed ``<actions>`` directives out of
    plan_text before sending — bridge received cleaned text only.
    The deepagents migration kept the streaming machinery but dropped
    the cleaning step; this test pins the parity invariant so raw
    ``<actions>`` markup can never reach the user mid-stream again.
    """
    bridge = _ReactingBridge()
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)
    plan_with_directive = (
        "Got it, looking into this now.\n"
        '<actions><react emoji="👀" /></actions>'
    )
    await d.observe(_ai(plan_with_directive))
    await d.observe(_ai(
        "", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}],
    ))
    # Cleaned text reached the bridge — no <actions> markup.
    assert len(bridge.sends) == 1
    sent_text = bridge.sends[0][1]
    assert "<actions>" not in sent_text
    assert "<react" not in sent_text
    assert "Got it" in sent_text
    # The directive WAS dispatched against the just-sent plan flush.
    assert bridge.reacts == [("ch-1", None, "👀")]
    assert d.streamed_plan is True


@pytest.mark.asyncio
async def test_dispatcher_actions_only_plan_no_bridge_send_but_directives_fire() -> None:
    """Edge case: plan is directives-only. After stripping ``<actions>``
    there's nothing left to send to the bridge — but the directives
    must still dispatch (e.g. an ack-react with no accompanying text).
    Pre-#181 invariant: ``cleaned_plan.strip()`` empty → no send,
    but directives are still forwarded."""
    bridge = _ReactingBridge()
    d = StreamingAutoDispatcher(channel_id="ch-1", bridge=bridge)
    directives_only = '<actions><react emoji="✅" /></actions>'
    await d.observe(_ai(directives_only))
    await d.observe(_ai(
        "", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}],
    ))
    # No bridge send (nothing left after stripping).
    assert bridge.sends == []
    # But the react fired.
    assert bridge.reacts == [("ch-1", None, "✅")]
    # streamed_plan stays False — we never confirmed a send.
    assert d.streamed_plan is False


@pytest.mark.asyncio
async def test_dispatcher_appends_cleaned_text_to_buffer_not_raw() -> None:
    """The outbound_appender callback must receive the CLEANED text
    (no ``<actions>`` markup) so chat_history reflects what the user
    actually saw, not the raw model output. Pre-#181 invariant."""
    bridge = _FakeBridge()
    captured: list[tuple[str, str]] = []

    async def _appender(channel_id, content, *, msg_id, source):
        captured.append((channel_id, content))

    d = StreamingAutoDispatcher(
        channel_id="ch-1", bridge=bridge,
        outbound_appender=_appender,
    )
    await d.observe(_ai(
        "Reply.\n<actions><react emoji=\"👍\" /></actions>"
    ))
    await d.observe(_ai(
        "", tool_calls=[{"name": "memory_query", "args": {}, "id": "t1"}],
    ))
    assert len(captured) == 1
    appended_content = captured[0][1]
    assert appended_content == "Reply."
    assert "<actions>" not in appended_content
