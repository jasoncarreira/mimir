"""Per-turn streaming auto-dispatcher (chainlink #5).

Pairs with the learned-behavior 'plan → work → result, no play-by-play':
the *content* shape (Jason 2026-05-04 21:54, refined 2026-05-05 17:43).

The default agent loop is all-or-nothing — every AssistantMessage
accumulates, then ``extract_turn_events`` and ``_auto_dispatch_or_record``
fire ONCE at end-of-turn. On multi-tool turns the user sees nothing
until the final wall lands.

This module observes ``AssistantMessage`` blocks as they stream in and
flushes on a single semantic boundary: **the first tool_use** ends the
"plan" phase. After that, any text accumulated before the next tool_use
is *intermediate* (suppressed from the user, captured as reasoning in
turns.jsonl). Text after the *last* tool_use becomes the "result" flush.

State machine
=============

phase ∈ {pre_tool, post_tool}, starts pre_tool

per AssistantMessage observed (top-level only; subagent-internal ones
have ``parent_tool_use_id != None`` and are skipped — they don't drive
user-visible chunking):

- text-only blocks:
  - pre_tool  → append text to ``plan_buffer``
  - post_tool → append text to ``candidate_result_buffer``
                (any prior candidate moves to ``suppressed_intermediate``;
                tool_use observations also drain candidate → suppressed)

- mixed (text + tool_use blocks):
  - text in pre_tool with first tool_use here → text goes into plan
    flush, tool_use ends pre_tool, plan flushes immediately
  - text in post_tool → text goes straight to ``suppressed_intermediate``
    (a tool_use will follow in the same message, so this text is
    by definition intermediate)
  - tool_use named ``mcp__mimir__send_message`` → operator chose
    explicit delivery; disable streaming (the existing send_message
    path is the canonical reply, no plan or result flush)
  - any other tool_use in post_tool → drain ``candidate_result_buffer``
    into ``suppressed_intermediate`` (those words turned out to be
    intermediate, not result)

end of turn
===========

- ``disabled`` (explicit send_message) or never reached post_tool
  (zero-tool turn): ``streamed_plan`` is False; the caller falls
  through to the single-flush path. Identical to current behavior.

- ``streamed_plan`` is True: caller takes ``result_text()`` and
  dispatches via the existing parse_directives + ``<actions>``
  pipeline as ``final=True``.

Boundary picked
===============

The boundary is **the SDK's tool_use block**, not a regex on
``**Plan:**`` / ``**Result:**`` text markers. The SDK already gives
us the semantic boundary for free; relying on text markers couples
content shape to delivery shape and breaks when the agent forgets
to use the markers. (Considered + rejected during design — see
chainlink #5 comment 17:59.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:  # pragma: no cover
    from .bridges._directives import Directive
    from .bridges.base import Bridge, SendResult

log = logging.getLogger(__name__)

# Tool name that disables streaming when invoked. The agent calling
# send_message explicitly is choosing the canonical-delivery path; the
# auto-dispatcher (streaming or otherwise) must not also try to send.
EXPLICIT_SEND_TOOL = "mcp__mimir__send_message"


@dataclass
class StreamingState:
    """Internal state of a per-turn streaming dispatcher.

    Exposed for testing — production code goes through
    ``StreamingAutoDispatcher`` and treats this dataclass as opaque.
    """

    enabled: bool = True
    phase: str = "pre_tool"
    plan_buffer: list[str] = field(default_factory=list)
    candidate_result_buffer: list[str] = field(default_factory=list)
    suppressed_intermediate: list[str] = field(default_factory=list)
    streamed_plan: bool = False
    disabled_by_explicit_send: bool = False

    def plan_text(self) -> str:
        return "\n".join(self.plan_buffer).strip()

    def result_text(self) -> str:
        return "\n".join(self.candidate_result_buffer).strip()

    def suppressed_text(self) -> str:
        return "\n\n".join(s.strip() for s in self.suppressed_intermediate if s.strip())


def _split_blocks(msg: AssistantMessage) -> tuple[list[str], list[ToolUseBlock]]:
    text_parts: list[str] = []
    tool_uses: list[ToolUseBlock] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            if block.text:
                text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_uses.append(block)
    return text_parts, tool_uses


def advance_state(state: StreamingState, msg: Message) -> str | None:
    """Pure state-machine step. Returns the plan text to flush, or None.

    Caller is responsible for actually invoking the bridge with the
    returned plan text. Splitting the side effect out of the state
    machine keeps it unit-testable without async/IO.
    """
    if not state.enabled or state.disabled_by_explicit_send:
        return None
    if not isinstance(msg, AssistantMessage):
        return None
    # Subagent-internal messages don't drive user-visible chunking.
    if getattr(msg, "parent_tool_use_id", None) is not None:
        return None

    text_parts, tool_uses = _split_blocks(msg)
    text = "\n".join(text_parts) if text_parts else ""

    # Explicit send_message → operator chose the canonical-delivery
    # path. Disable streaming entirely; the existing send_message_attempts
    # gate skips _auto_dispatch_or_record on the caller side.
    if any(tu.name == EXPLICIT_SEND_TOOL for tu in tool_uses):
        state.disabled_by_explicit_send = True
        # Anything we'd buffered up to here is moot — drop it.
        state.plan_buffer.clear()
        state.candidate_result_buffer.clear()
        return None

    plan_to_flush: str | None = None

    if state.phase == "pre_tool":
        if text:
            state.plan_buffer.append(text)
        if tool_uses:
            # First tool_use boundary — flush plan and transition.
            candidate_plan = "\n".join(state.plan_buffer).strip()
            if candidate_plan:
                plan_to_flush = candidate_plan
                state.streamed_plan = True
            state.phase = "post_tool"
    else:  # post_tool
        if tool_uses:
            # New tool_use → any text in this message precedes the
            # tool_use within the AssistantMessage (SDK ordering),
            # so it's by definition intermediate. Send it to
            # suppressed.
            if text:
                state.suppressed_intermediate.append(text)
            # And the prior candidate result was also intermediate.
            if state.candidate_result_buffer:
                state.suppressed_intermediate.extend(
                    state.candidate_result_buffer
                )
                state.candidate_result_buffer.clear()
        elif text:
            # Text-only AssistantMessage in post_tool. Could be the
            # final result, or could turn out to be intermediate if
            # another tool_use arrives. Buffer as candidate; the
            # next tool_use (if any) will demote it.
            state.candidate_result_buffer.append(text)

    return plan_to_flush


class StreamingAutoDispatcher:
    """Owns the plan flush during a streaming turn.

    Wired into ``Agent._run_turn`` between message arrival and
    end-of-turn. When eligible, observes each ``AssistantMessage`` as
    it streams in; on the first tool_use boundary, flushes the
    accumulated plan text via the bridge with ``final=False`` (so the
    Discord typing indicator stays held — the bot is still working).

    The result flush is the caller's job — ``Agent._run_turn`` calls
    ``_auto_dispatch_or_record`` with ``result_text()`` after the
    message loop ends, treating it like the existing single-flush
    path but with the plan already delivered.
    """

    def __init__(
        self,
        *,
        channel_id: str,
        bridge: "Bridge | None",
        on_plan_dispatched: (
            Callable[
                [str, "SendResult | None", "tuple[Directive, ...]"],
                Awaitable[None],
            ]
            | None
        ) = None,
        on_plan_failed: Callable[[str, str], Awaitable[None]] | None = None,
        eligible: bool = True,
    ) -> None:
        self._channel_id = channel_id
        self._bridge = bridge
        self._on_plan_dispatched = on_plan_dispatched
        self._on_plan_failed = on_plan_failed
        # Bench / no-bridge channels skip streaming. The eligibility
        # gate matches the existing _auto_dispatch_or_record gate so
        # every channel gets one consistent answer.
        enabled = (
            eligible
            and bridge is not None
            and getattr(bridge, "name", "") not in ("bench",)
        )
        self.state = StreamingState(enabled=enabled)

    @property
    def enabled(self) -> bool:
        return self.state.enabled

    @property
    def streamed_plan(self) -> bool:
        return self.state.streamed_plan

    @property
    def disabled_by_explicit_send(self) -> bool:
        return self.state.disabled_by_explicit_send

    def result_text(self) -> str:
        return self.state.result_text()

    def suppressed_text(self) -> str:
        return self.state.suppressed_text()

    async def observe(self, msg: Message) -> None:
        """Advance the state machine and (if needed) flush the plan.

        On a flush, ``<actions>`` directives present in the plan text
        are parsed out and forwarded to ``on_plan_dispatched`` so the
        caller can dispatch them mid-turn (against the just-sent plan
        message). The plan-flush bridge send carries only the cleaned
        text; raw ``<actions>`` markup never reaches the channel."""
        plan_text = advance_state(self.state, msg)
        if plan_text is None:
            return
        # Plan flush — final=False so the Discord typing indicator
        # stays held: the bot is in the middle of work, not done.
        if self._bridge is None:
            return

        # Parse directives out of the plan text. Directives are
        # dispatched alongside the plan flush (so an inline ack-react
        # in the plan actually lands on the user's message); the bridge
        # send carries only the cleaned remainder so raw markup never
        # reaches the channel.
        cleaned_plan, directives = _parse_plan_directives(plan_text)

        send_result: "SendResult | None" = None
        if cleaned_plan.strip():
            try:
                send_result = await self._bridge.send(
                    self._channel_id, cleaned_plan, final=False,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("streaming plan flush failed")
                if self._on_plan_failed is not None:
                    try:
                        await self._on_plan_failed(
                            cleaned_plan, f"{type(exc).__name__}: {exc}"
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("on_plan_failed callback raised")
                return
            if not send_result.sent:
                if self._on_plan_failed is not None:
                    try:
                        await self._on_plan_failed(
                            cleaned_plan,
                            send_result.error or "bridge returned sent=False",
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("on_plan_failed callback raised")
                return

        # Notify the dispatched callback when there's something for it
        # to act on: either a successful bridge send, or directives that
        # still need dispatching even though the cleaned plan text was
        # empty (e.g. an actions-only plan ack). When both are absent,
        # nothing to report.
        if send_result is None and not directives:
            return
        if self._on_plan_dispatched is not None:
            try:
                await self._on_plan_dispatched(
                    cleaned_plan, send_result, directives,
                )
            except Exception:  # noqa: BLE001
                log.exception("on_plan_dispatched callback raised")


def _parse_plan_directives(
    text: str,
) -> tuple[str, "tuple[Directive, ...]"]:
    """Return ``(cleaned_text, directives_tuple)``.

    Wraps the bridges' parse_directives. The plan flush sends the
    cleaned text only; the parsed directives are forwarded to the
    on_plan_dispatched callback so the caller can dispatch them
    against the just-sent plan flush. (Previously these were
    silently dropped — see chainlink #5 follow-up.)
    """
    try:
        from .bridges._directives import parse_directives

        parsed = parse_directives(text)
        return (parsed.clean_text or "", tuple(parsed.directives))
    except Exception:  # noqa: BLE001
        log.exception("parse_directives raised in plan-flush parser")
        return text, ()


def intermediate_text_segments(messages: list[Message]) -> list[str]:
    """Return the list of text segments that streaming would have
    suppressed as intermediate (between first and last tool_use).

    Used by ``extract_turn_events(streaming_active=True)`` to demote
    those segments from output → reasoning so turns.jsonl reflects
    what the user actually saw.

    Walks top-level AssistantMessages only (subagent-internal
    messages are filtered upstream).
    """
    # Find indices of the first and last AssistantMessages that
    # contain a tool_use. Text in messages strictly between those
    # indices is intermediate. Text in mixed messages with tool_use
    # at the same position is also intermediate when the position
    # is post-first-tool-use.
    tool_indices: list[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, AssistantMessage):
            continue
        if getattr(msg, "parent_tool_use_id", None) is not None:
            continue
        if any(isinstance(b, ToolUseBlock) for b in msg.content):
            tool_indices.append(i)

    if not tool_indices:
        return []

    first_tool = tool_indices[0]
    last_tool = tool_indices[-1]
    if first_tool == last_tool:
        return []

    suppressed: list[str] = []
    for i in range(first_tool + 1, last_tool + 1):
        msg = messages[i]
        if not isinstance(msg, AssistantMessage):
            continue
        if getattr(msg, "parent_tool_use_id", None) is not None:
            continue
        text_parts, tool_uses = _split_blocks(msg)
        if not text_parts:
            continue
        # Text in a mixed message at index i precedes the tool_use
        # within the message — intermediate either way.
        suppressed.append("\n".join(text_parts))
    return suppressed


__all__ = [
    "EXPLICIT_SEND_TOOL",
    "StreamingAutoDispatcher",
    "StreamingState",
    "advance_state",
    "intermediate_text_segments",
]
