"""Streaming auto-dispatcher (chainlink #5).

State-machine + integration tests. The dispatcher's job:

- Observe AssistantMessage as they stream from the SDK.
- On the first tool_use, flush accumulated "plan" text via the
  bridge with ``final=False`` (typing stays held).
- After that, suppress any text-only AssistantMessages between
  tool_uses (those become "reasoning" entries in turns.jsonl, not
  user-visible chunks).
- Expose ``result_text()`` for the caller's end-of-turn flush.
- Disable when the agent invokes ``mcp__mimir__send_message``
  explicitly (operator chose canonical-delivery).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from mimir._streaming_dispatch import (
    EXPLICIT_SEND_TOOL,
    StreamingAutoDispatcher,
    StreamingState,
    advance_state,
    intermediate_text_segments,
)
from mimir.bridges.base import Bridge, SendResult
from mimir.turn_logger import extract_turn_events


def _assistant(*blocks, parent_tool_use_id: str | None = None):
    return AssistantMessage(
        content=list(blocks),
        model="claude-opus-4-7",
        parent_tool_use_id=parent_tool_use_id,
    )


# --------------------------------------------------------------------- #
# Recording bridge — observes send() calls and the `final` kwarg
# --------------------------------------------------------------------- #


@dataclass
class _Sent:
    channel_id: str
    text: str
    final: bool
    attachments: list[Path] | None


class RecordingBridge(Bridge):
    prefixes = ("test-",)
    name = "test-recording"

    def __init__(self) -> None:
        self.sends: list[_Sent] = []
        self._counter = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
        *,
        final: bool = True,
    ) -> SendResult:
        self.sends.append(
            _Sent(
                channel_id=channel_id,
                text=text,
                final=final,
                attachments=list(attachment_paths) if attachment_paths else None,
            )
        )
        self._counter += 1
        return SendResult(
            sent=True, message_id=f"msg-{self._counter}", chunks=1,
        )

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        return True


# --------------------------------------------------------------------- #
# Pure state-machine tests (advance_state)
# --------------------------------------------------------------------- #


def test_pretool_text_accumulates_into_plan_buffer():
    state = StreamingState()
    plan = advance_state(state, _assistant(TextBlock(text="thinking 1")))
    assert plan is None  # no tool_use yet → no flush
    advance_state(state, _assistant(TextBlock(text="thinking 2")))
    assert state.plan_buffer == ["thinking 1", "thinking 2"]
    assert state.streamed_plan is False


def test_first_tool_use_flushes_plan_and_transitions_phase():
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="**Plan:** I'll do X")))
    plan = advance_state(
        state,
        _assistant(ToolUseBlock(id="t1", name="Read", input={})),
    )
    assert plan == "**Plan:** I'll do X"
    assert state.streamed_plan is True
    assert state.phase == "post_tool"


def test_mixed_message_with_first_tool_use_flushes_combined_plan():
    """Text + tool_use in the same AssistantMessage on the FIRST tool
    boundary: text reads as part of the plan and flushes alongside
    the boundary."""
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="setup")))
    plan = advance_state(
        state,
        _assistant(
            TextBlock(text="here goes"),
            ToolUseBlock(id="t1", name="Read", input={}),
        ),
    )
    assert plan == "setup\nhere goes"
    assert state.phase == "post_tool"


def test_post_tool_text_only_buffers_as_candidate_result():
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="plan")))
    advance_state(state, _assistant(ToolUseBlock(id="t1", name="Read", input={})))
    advance_state(state, _assistant(TextBlock(text="result text")))
    assert state.candidate_result_buffer == ["result text"]
    assert state.suppressed_intermediate == []
    assert state.result_text() == "result text"


def test_intermediate_text_between_tool_uses_is_suppressed():
    """Text-only AssistantMessage that arrives between two tool_uses
    should end up in suppressed_intermediate, not in the result."""
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="plan")))
    advance_state(state, _assistant(ToolUseBlock(id="t1", name="Read", input={})))
    advance_state(state, _assistant(TextBlock(text="ok now I'll")))  # intermediate
    advance_state(state, _assistant(ToolUseBlock(id="t2", name="Edit", input={})))
    advance_state(state, _assistant(TextBlock(text="final result")))

    assert state.candidate_result_buffer == ["final result"]
    assert state.suppressed_intermediate == ["ok now I'll"]
    assert state.result_text() == "final result"
    assert "ok now I'll" in state.suppressed_text()


def test_mixed_message_in_post_tool_routes_text_to_suppressed():
    """text + tool_use in post_tool: the text precedes the tool_use
    within the AssistantMessage (SDK ordering), so it's intermediate
    by definition — suppressed, not candidate."""
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="plan")))
    advance_state(state, _assistant(ToolUseBlock(id="t1", name="Read", input={})))
    advance_state(
        state,
        _assistant(
            TextBlock(text="now editing"),
            ToolUseBlock(id="t2", name="Edit", input={}),
        ),
    )
    advance_state(state, _assistant(TextBlock(text="done")))

    assert state.candidate_result_buffer == ["done"]
    assert "now editing" in state.suppressed_intermediate


def test_explicit_send_message_disables_streaming():
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="plan")))
    plan = advance_state(
        state,
        _assistant(
            ToolUseBlock(id="t1", name=EXPLICIT_SEND_TOOL, input={"text": "hi"}),
        ),
    )
    assert plan is None
    assert state.disabled_by_explicit_send is True
    # And the buffers are dropped — we won't redeliver anything via
    # streaming when the operator chose explicit delivery.
    assert state.plan_buffer == []
    assert state.candidate_result_buffer == []


def test_disabled_state_no_ops_subsequent_observations():
    state = StreamingState()
    state.disabled_by_explicit_send = True
    plan = advance_state(state, _assistant(TextBlock(text="ignored")))
    assert plan is None
    assert state.plan_buffer == []


def test_disabled_eligibility_no_ops():
    state = StreamingState(enabled=False)
    plan = advance_state(state, _assistant(TextBlock(text="ignored")))
    assert plan is None


def test_zero_tool_use_turn_never_flushes_plan():
    state = StreamingState()
    advance_state(state, _assistant(TextBlock(text="just a quick reply")))
    assert state.streamed_plan is False
    assert state.plan_buffer == ["just a quick reply"]
    assert state.candidate_result_buffer == []
    assert state.result_text() == ""


def test_subagent_internal_messages_skipped():
    """parent_tool_use_id != None → don't drive chunking. Subagent
    text doesn't reach the user-visible channel."""
    state = StreamingState()
    plan = advance_state(
        state,
        _assistant(
            TextBlock(text="subagent thinking"),
            ToolUseBlock(id="sub-1", name="Read", input={}),
            parent_tool_use_id="parent-1",
        ),
    )
    assert plan is None
    assert state.plan_buffer == []
    assert state.phase == "pre_tool"


# --------------------------------------------------------------------- #
# Dispatcher integration tests (StreamingAutoDispatcher + bridge)
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatcher_flushes_plan_with_final_false():
    bridge = RecordingBridge()
    disp = StreamingAutoDispatcher(
        channel_id="test-ch", bridge=bridge, eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="**Plan:** do X")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    await disp.observe(_assistant(TextBlock(text="**Result:** done")))

    assert len(bridge.sends) == 1
    assert bridge.sends[0].text == "**Plan:** do X"
    assert bridge.sends[0].final is False
    assert disp.streamed_plan is True
    assert disp.result_text() == "**Result:** done"


@pytest.mark.asyncio
async def test_dispatcher_ineligible_never_sends():
    """Heartbeat / scheduled ticks → eligible=False → bridge.send is
    never called, even if text + tool_use arrive."""
    bridge = RecordingBridge()
    disp = StreamingAutoDispatcher(
        channel_id="test-ch", bridge=bridge, eligible=False,
    )
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    assert bridge.sends == []
    assert disp.streamed_plan is False


@pytest.mark.asyncio
async def test_dispatcher_bench_bridge_disables_streaming():
    """Bench harness reads SDK final text directly — never stream."""

    class _BenchLike(RecordingBridge):
        name = "bench"

    bridge = _BenchLike()
    disp = StreamingAutoDispatcher(
        channel_id="bench-x", bridge=bridge, eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    assert bridge.sends == []
    assert disp.enabled is False


@pytest.mark.asyncio
async def test_dispatcher_callbacks_fire_on_plan_flush():
    bridge = RecordingBridge()
    seen: list[tuple[str, Any]] = []

    async def on_plan(text, result, directives) -> None:
        seen.append(("ok", (text, result.message_id, len(directives))))

    async def on_fail(text: str, error: str) -> None:
        seen.append(("fail", (text, error)))

    disp = StreamingAutoDispatcher(
        channel_id="test-ch",
        bridge=bridge,
        on_plan_dispatched=on_plan,
        on_plan_failed=on_fail,
        eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))

    assert len(seen) == 1
    kind, (text, msg_id, n_directives) = seen[0]
    assert kind == "ok"
    assert text == "plan"
    assert msg_id == "msg-1"
    assert n_directives == 0


@pytest.mark.asyncio
async def test_dispatcher_strips_actions_from_plan_flush_text():
    """Plan with inline ``<actions>``: the bridge send carries only
    the cleaned remainder (no raw markup), and the parsed directives
    flow through to the dispatched callback so the caller can fire
    them mid-turn alongside the plan."""
    bridge = RecordingBridge()
    seen: list[tuple[str, str | None, tuple]] = []

    async def on_plan(text, result, directives) -> None:
        msg_id = result.message_id if result is not None else None
        seen.append((text, msg_id, directives))

    disp = StreamingAutoDispatcher(
        channel_id="test-ch",
        bridge=bridge,
        on_plan_dispatched=on_plan,
        eligible=True,
    )
    plan_with_actions = (
        "**Plan:** I'll do X then Y\n\n"
        "<actions>\n"
        '  <react emoji="👍" />\n'
        "</actions>"
    )
    await disp.observe(_assistant(TextBlock(text=plan_with_actions)))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))

    assert len(bridge.sends) == 1
    sent_text = bridge.sends[0].text
    assert "<actions>" not in sent_text
    assert "I'll do X then Y" in sent_text
    # Callback got the parsed directive — caller dispatches mid-turn.
    assert len(seen) == 1
    text_seen, msg_id, directives = seen[0]
    assert msg_id == "msg-1"
    assert len(directives) == 1


@pytest.mark.asyncio
async def test_dispatcher_directives_only_plan_dispatches_without_send():
    """A plan that's *only* an actions block (e.g. an inline ack-react
    on the inbound user message): no bridge send (nothing user-visible
    to deliver), but the callback still fires so the directive gets
    dispatched. Previously these directives were silently dropped."""
    bridge = RecordingBridge()
    seen: list[tuple[str, object, tuple]] = []

    async def on_plan(text, result, directives) -> None:
        seen.append((text, result, directives))

    disp = StreamingAutoDispatcher(
        channel_id="test-ch",
        bridge=bridge,
        on_plan_dispatched=on_plan,
        eligible=True,
    )
    actions_only = (
        '<actions>\n'
        '  <react emoji="👍" message="inbound-123" />\n'
        '</actions>'
    )
    await disp.observe(_assistant(TextBlock(text=actions_only)))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    # No bridge send — the cleaned text was empty.
    assert bridge.sends == []
    # But the callback fired with result=None and the directive.
    assert len(seen) == 1
    text_seen, result, directives = seen[0]
    assert text_seen == ""
    assert result is None
    assert len(directives) == 1


@pytest.mark.asyncio
async def test_dispatcher_callback_exception_does_not_break_loop():
    """The dispatched callback can raise (e.g. the directive dispatch
    itself errors); the dispatcher must swallow it so the main message
    loop keeps running."""
    bridge = RecordingBridge()

    async def boom(text, result, directives) -> None:
        raise RuntimeError("dispatch exploded")

    disp = StreamingAutoDispatcher(
        channel_id="test-ch",
        bridge=bridge,
        on_plan_dispatched=boom,
        eligible=True,
    )
    # Should not raise.
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    # Bridge send still happened.
    assert len(bridge.sends) == 1


@pytest.mark.asyncio
async def test_dispatcher_explicit_send_no_plan_flush():
    """When the agent invokes mcp__mimir__send_message explicitly,
    the streaming dispatcher must not also send a plan flush — the
    explicit call IS the canonical delivery."""
    bridge = RecordingBridge()
    disp = StreamingAutoDispatcher(
        channel_id="test-ch", bridge=bridge, eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(
        _assistant(
            ToolUseBlock(id="t1", name=EXPLICIT_SEND_TOOL, input={"text": "hi"}),
        )
    )
    assert bridge.sends == []
    assert disp.disabled_by_explicit_send is True
    assert disp.streamed_plan is False


@pytest.mark.asyncio
async def test_dispatcher_zero_tool_use_no_send():
    """Single-paragraph reply with no tool calls: streaming never
    fires, caller falls through to the existing single-flush path."""
    bridge = RecordingBridge()
    disp = StreamingAutoDispatcher(
        channel_id="test-ch", bridge=bridge, eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="just answering")))
    assert bridge.sends == []
    assert disp.streamed_plan is False


@pytest.mark.asyncio
async def test_dispatcher_send_failure_fires_failure_callback():
    class _FailingBridge(RecordingBridge):
        async def send(self, channel_id, text, attachment_paths=None, *, final=True):
            return SendResult(sent=False, error="bridge offline")

    bridge = _FailingBridge()
    fails: list[tuple[str, str]] = []

    async def on_fail(text: str, error: str) -> None:
        fails.append((text, error))

    disp = StreamingAutoDispatcher(
        channel_id="test-ch",
        bridge=bridge,
        on_plan_failed=on_fail,
        eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    assert fails == [("plan", "bridge offline")]


@pytest.mark.asyncio
async def test_dispatcher_send_raises_caught_and_logged():
    class _RaisingBridge(RecordingBridge):
        async def send(self, channel_id, text, attachment_paths=None, *, final=True):
            raise RuntimeError("network down")

    bridge = _RaisingBridge()
    fails: list[tuple[str, str]] = []

    async def on_fail(text: str, error: str) -> None:
        fails.append((text, error))

    disp = StreamingAutoDispatcher(
        channel_id="test-ch",
        bridge=bridge,
        on_plan_failed=on_fail,
        eligible=True,
    )
    await disp.observe(_assistant(TextBlock(text="plan")))
    await disp.observe(_assistant(ToolUseBlock(id="t1", name="Read", input={})))
    assert len(fails) == 1
    assert "network down" in fails[0][1]


# --------------------------------------------------------------------- #
# extract_turn_events with streaming_active=True — turns.jsonl mirrors
# what the user actually saw.
# --------------------------------------------------------------------- #


def test_extract_with_streaming_demotes_intermediate_to_reasoning():
    """Two tool_uses, with text between: the intermediate text-only
    AssistantMessage becomes a reasoning event, not part of output."""
    msgs = [
        _assistant(TextBlock(text="plan")),
        _assistant(ToolUseBlock(id="t1", name="Read", input={})),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="r1", is_error=False)],
        ),
        _assistant(TextBlock(text="intermediate narration")),
        _assistant(ToolUseBlock(id="t2", name="Edit", input={})),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t2", content="r2", is_error=False)],
        ),
        _assistant(TextBlock(text="result")),
    ]
    events, output = extract_turn_events(msgs, streaming_active=True)

    # Plan (pre-first-tool) and result (post-last-tool) join into output.
    assert "plan" in output
    assert "result" in output
    # The intermediate text appears as a reasoning event, not output.
    assert "intermediate narration" not in output
    reasoning_contents = [
        e["content"] for e in events if e["type"] == "reasoning"
    ]
    assert any("intermediate narration" in c for c in reasoning_contents)


def test_extract_without_streaming_preserves_old_behavior():
    """Default streaming_active=False → all text-only messages still
    collapse into output. Regression guard: existing callers don't
    see a behavior change."""
    msgs = [
        _assistant(TextBlock(text="plan")),
        _assistant(ToolUseBlock(id="t1", name="Read", input={})),
        _assistant(TextBlock(text="middle")),
        _assistant(ToolUseBlock(id="t2", name="Edit", input={})),
        _assistant(TextBlock(text="result")),
    ]
    events, output = extract_turn_events(msgs)
    # All three text-only messages contribute to output.
    assert "plan" in output and "middle" in output and "result" in output


def test_extract_with_streaming_no_change_for_zero_tool_turn():
    """Streaming flag set but turn had no tool_use: behavior identical."""
    msgs = [_assistant(TextBlock(text="hello"))]
    events, output = extract_turn_events(msgs, streaming_active=True)
    assert output == "hello"
    assert events == []


def test_extract_with_streaming_no_change_for_single_tool_turn():
    """One tool_use → no 'intermediate' range. Plan + result both
    survive into output exactly as without the flag."""
    msgs = [
        _assistant(TextBlock(text="plan")),
        _assistant(ToolUseBlock(id="t1", name="Read", input={})),
        _assistant(TextBlock(text="result")),
    ]
    events, output = extract_turn_events(msgs, streaming_active=True)
    assert "plan" in output
    assert "result" in output


def test_intermediate_text_segments_helper():
    """The helper used by streaming-active extract path."""
    msgs = [
        _assistant(TextBlock(text="plan")),
        _assistant(ToolUseBlock(id="t1", name="Read", input={})),
        _assistant(TextBlock(text="middle")),
        _assistant(ToolUseBlock(id="t2", name="Edit", input={})),
        _assistant(TextBlock(text="result")),
    ]
    segs = intermediate_text_segments(msgs)
    assert segs == ["middle"]


def test_intermediate_text_segments_empty_for_zero_or_one_tool_use():
    assert intermediate_text_segments([_assistant(TextBlock(text="hi"))]) == []
    assert (
        intermediate_text_segments(
            [
                _assistant(TextBlock(text="plan")),
                _assistant(ToolUseBlock(id="t1", name="Read", input={})),
                _assistant(TextBlock(text="result")),
            ]
        )
        == []
    )
