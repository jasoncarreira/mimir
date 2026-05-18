"""Per-turn streaming auto-dispatcher (chainlink #5, ported to LangChain).

Restores mimir's "plan / work / result" chunking under the deepagents
runtime. The default agent loop is all-or-nothing — ``agent.ainvoke``
accumulates every step's output and we ship one big bridge.send at
end of turn. On multi-tool turns the user sees nothing until the
final wall lands.

This module observes LangChain ``AIMessage`` chunks as they stream in
(via ``agent.astream(stream_mode="values")``) and flushes on a single
semantic boundary: **the first tool_call** ends the "plan" phase. Text
accumulated before that boundary is sent immediately with
``final=False`` (Discord typing indicator stays held — the bot is
still working). Text between tool calls becomes ``suppressed
intermediate`` (captured as reasoning in turns.jsonl, NOT shown to
the user). Text after the last tool call is the ``result`` flush.

Special case: if the agent invokes ``send_message`` explicitly as a
tool call, streaming dispatch self-disables — explicit send is the
canonical delivery and we don't want to double-ship.

Adaptation from main's SDK version:
  - ``AssistantMessage`` → LangChain ``AIMessage``
  - ``TextBlock`` / ``ToolUseBlock`` content blocks → ``.content`` (str)
    + ``.tool_calls`` (list[dict])
  - ``EXPLICIT_SEND_TOOL`` = ``"send_message"`` (native @tool name,
    not the SDK-era ``mcp__mimir__send_message`` namespaced form)
  - Subagent filter (``parent_tool_use_id``) dropped — LangGraph
    flattens sub-tool calls into the parent AIMessage; no nested
    "internal" AssistantMessages to skip.
  - ``<actions>`` directive parsing dropped — bridges-specific markup
    that lived in the SDK pipeline; deepagents tool calls don't emit
    that shape. Plan text ships verbatim.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage

if TYPE_CHECKING:  # pragma: no cover
    from .bridges.base import Bridge, SendResult

# Callback shape for chat-history outbound appends. The agent's
# ``Agent._append_outbound_to_buffer`` matches this signature; tests
# pass ``None`` to skip the append. ``msg_id`` is the bridge's
# delivered message id (None when the bridge failed); ``source`` is
# the inbound trigger's source (e.g. ``"discord"``) so outbound rows
# inherit the same provenance tag.
OutboundAppender = Callable[..., Awaitable[None]]

log = logging.getLogger(__name__)


# Native @tool name from mimir.tools.registry. Routed through the
# ``claude-code:*`` provider, langchain-claude-code's MCP bridge wraps
# every @tool as ``mcp__langchain-tools__<name>`` before handing it to
# the claude subprocess — so by the time tool_calls come back on an
# AIMessage they carry the namespaced form, not the bare name. We
# detect either suffix so streaming dispatch self-disables on explicit
# send regardless of which provider path the model took.
EXPLICIT_SEND_TOOL_NAMES = frozenset({
    "send_message",                         # native langchain @tool
    "mcp__langchain-tools__send_message",   # claude-code MCP bridge
    "mcp__mimir__send_message",             # legacy SDK MCP-server form
})


def _is_explicit_send(tool_call: Any) -> bool:
    """True iff this tool_call is the operator-chose-canonical-delivery
    send_message path. Matches across native / claude-code-bridged /
    legacy-MCP-bridged tool names."""
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    if not name:
        return False
    if name in EXPLICIT_SEND_TOOL_NAMES:
        return True
    # Tolerant suffix match — future bridge renames that keep the
    # trailing ``send_message`` token still self-disable.
    return name.endswith("__send_message") or name == "send_message"


# Kept for legacy callers / tests that compared against this constant.
EXPLICIT_SEND_TOOL = "send_message"


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
    # Count of AIMessages already passed to advance_state. The caller
    # (agent.py during astream) bumps a parallel counter and only
    # forwards messages past this index, since astream(stream_mode=
    # "values") re-emits the cumulative messages list every step.
    # We don't use ``id(msg)`` because Python recycles ids for
    # garbage-collected temporaries — caused a real test-flake.
    observed_count: int = 0

    def plan_text(self) -> str:
        return "\n".join(self.plan_buffer).strip()

    def result_text(self) -> str:
        return "\n".join(self.candidate_result_buffer).strip()

    def suppressed_text(self) -> str:
        return "\n\n".join(s.strip() for s in self.suppressed_intermediate if s.strip())


def _split_blocks(msg: AIMessage) -> tuple[str, list[dict[str, Any]]]:
    """Extract (text, tool_calls) from a LangChain AIMessage.

    ``msg.content`` may be a plain string OR a list of content-block
    dicts (provider-dependent). We coerce both to a single text string
    so the state machine can stay shape-agnostic. ``msg.tool_calls``
    is the canonical tool-call list; ``response_metadata`` may carry
    internal_tool_calls (from claude-code) which we also fold in.
    """
    content = msg.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
        text = "\n".join(parts)
    else:
        text = str(content) if content else ""

    tool_calls = list(msg.tool_calls or [])
    rmd = getattr(msg, "response_metadata", None) or {}
    for internal in rmd.get("internal_tool_calls") or []:
        if internal not in tool_calls:
            tool_calls.append(internal)
    return text.strip(), tool_calls


def advance_state(state: StreamingState, msg: BaseMessage) -> str | None:
    """Pure state-machine step. Returns the plan text to flush, or None.

    Caller invokes the bridge with the returned plan text. Splitting
    the side effect out of the state machine keeps it unit-testable
    without async/IO.
    """
    if not state.enabled or state.disabled_by_explicit_send:
        return None
    if not isinstance(msg, AIMessage):
        return None
    state.observed_count += 1

    text, tool_uses = _split_blocks(msg)

    # Explicit send_message → operator chose the canonical-delivery
    # path. Disable streaming entirely; everything we've buffered
    # so far is moot. ``_is_explicit_send`` matches across native,
    # claude-code-bridged, and legacy-MCP-bridged tool names.
    if any(_is_explicit_send(tc) for tc in tool_uses):
        state.disabled_by_explicit_send = True
        state.plan_buffer.clear()
        state.candidate_result_buffer.clear()
        return None

    plan_to_flush: str | None = None

    if state.phase == "pre_tool":
        if text:
            state.plan_buffer.append(text)
        if tool_uses:
            # First tool_use boundary — flush plan and transition.
            # Note: ``streamed_plan`` is NOT set here. The dispatcher
            # sets it AFTER the bridge confirms sent=True (otherwise
            # a directives-only plan or a bridge crash would mislead
            # downstream telemetry into claiming text was suppressed
            # when in fact nothing reached the user).
            candidate_plan = "\n".join(state.plan_buffer).strip()
            if candidate_plan:
                plan_to_flush = candidate_plan
            state.phase = "post_tool"
    else:  # post_tool
        if tool_uses:
            # New tool_use → any text in this message precedes the
            # tool_use within the AIMessage, so it's by definition
            # intermediate. Send it to suppressed.
            if text:
                state.suppressed_intermediate.append(text)
            # And the prior candidate result was also intermediate.
            if state.candidate_result_buffer:
                state.suppressed_intermediate.extend(
                    state.candidate_result_buffer
                )
                state.candidate_result_buffer.clear()
        elif text:
            # Text-only AIMessage in post_tool. Could be the final
            # result, or could turn out to be intermediate if another
            # tool_use arrives. Buffer as candidate; the next tool_use
            # (if any) will demote it.
            state.candidate_result_buffer.append(text)

    return plan_to_flush


class StreamingAutoDispatcher:
    """Owns the plan flush during a streaming turn.

    Wired into ``Agent._run_turn_body`` between message arrival and
    end-of-turn. When eligible, observes each ``AIMessage`` as it
    streams in; on the first tool_call boundary, flushes the
    accumulated plan text via the bridge with ``final=False`` (so
    the Discord typing indicator stays held — the bot is still
    working).

    The result flush is the caller's job — the run_turn end-of-turn
    bridge.send picks up ``result_text()`` when streaming was active,
    or falls through to the canonical output flush otherwise.
    """

    def __init__(
        self,
        *,
        channel_id: str,
        bridge: "Bridge | None",
        eligible: bool = True,
        outbound_appender: "OutboundAppender | None" = None,
        channel_source: str | None = None,
    ) -> None:
        self._channel_id = channel_id
        self._bridge = bridge
        # ``outbound_appender(channel_id, content, *, msg_id, source)``
        # — optional async callback to record the flushed text into
        # ``chat_history`` so the agent sees its own streamed reply
        # in the next turn's Recent activity. The agent passes
        # ``Agent._append_outbound_to_buffer``; tests / bench paths
        # leave it ``None`` and the dispatcher is a no-op on this axis.
        self._outbound_appender = outbound_appender
        self._channel_source = channel_source
        # Bench / no-bridge / non-user-facing channels skip streaming.
        # ``BenchBridge`` has name="bench"; tests / smoke runs that
        # invoke without a bridge also fall through cleanly here.
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

    async def observe(self, msg: BaseMessage) -> None:
        """Advance the state machine and (if needed) flush the plan.

        Parses ``<actions>`` directives out of the plan text before
        sending — the bridge receives only the cleaned remainder so
        raw markup never reaches the channel. Pre-#181's dispatcher
        did this same cleaning + directive dispatch; the deepagents
        migration kept the streaming machinery but dropped the
        cleaning step, so raw ``<actions>...</actions>`` blocks were
        being sent to users mid-stream. Restoring matches the
        agent-fallback ``bridge.send`` path (``agent.py``) which has
        always parsed directives.
        """
        plan_text = advance_state(self.state, msg)
        if plan_text is None:
            return
        if self._bridge is None:
            return

        # Strip directives. ``parse_directives`` failure is non-fatal —
        # if the bridges' parser raises, send the raw text rather than
        # blocking the flush; the agent fallback at end of turn has
        # the same fallback shape.
        try:
            from .bridges._directives import parse_directives, ReactDirective
            parsed = parse_directives(plan_text)
            clean_plan = parsed.clean_text or ""
            directives = list(parsed.directives)
        except Exception:  # noqa: BLE001
            log.exception("parse_directives raised in plan-flush parser")
            clean_plan = plan_text
            directives = []

        result = None
        if clean_plan.strip():
            try:
                result = await self._bridge.send(
                    self._channel_id, clean_plan, final=False,
                )
            except Exception:  # noqa: BLE001
                log.exception("streaming plan flush failed")
                # Continue to directive dispatch + buffer append below
                # — the cleaned plan still represents what the agent
                # intended to say, and inline directives (e.g. an
                # ack-react) may still be worth firing.
                result = None
            else:
                # Only flip ``streamed_plan`` after the bridge confirms
                # ``sent=True`` — otherwise turns.jsonl could claim text
                # was suppressed from the user when in fact the bridge
                # dropped it. Pre-#181 invariant.
                if getattr(result, "sent", False):
                    self.state.streamed_plan = True

        # Dispatch parsed directives. Currently only ReactDirective is
        # actionable in this path; the agent-fallback at end of turn
        # has the same partial coverage. SendFileDirective is not yet
        # implemented anywhere.
        for d in directives:
            if isinstance(d, ReactDirective):
                target = d.message_id or (
                    getattr(result, "message_id", None) if result else None
                )
                try:
                    await self._bridge.react(
                        self._channel_id, target, d.emoji,
                    )
                except Exception:  # noqa: BLE001
                    log.debug("streaming react directive failed", exc_info=True)

        # Record the CLEANED text in chat_history so the agent's next
        # turn sees its own streamed reply in Recent activity — and so
        # the raw ``<actions>`` markup the user never saw doesn't leak
        # into the conversation record either. Mirrors pre-#181's
        # ``_record_outbound(plan_text=cleaned_plan, …)`` invariant.
        if clean_plan and self._outbound_appender is not None:
            try:
                await self._outbound_appender(
                    self._channel_id,
                    clean_plan,
                    msg_id=getattr(result, "message_id", None) if result else None,
                    source=self._channel_source,
                )
            except Exception:  # noqa: BLE001
                log.exception("streaming outbound buffer append failed")


def intermediate_text_segments(messages: list[BaseMessage]) -> list[str]:
    """Return text segments that streaming would have suppressed as
    intermediate (between first and last tool_call).

    Used by ``extract_turn_events(streaming_active=True)`` (when wired)
    to demote those segments from ``output`` → ``reasoning`` in
    turns.jsonl so the log reflects what the user actually saw.
    """
    # Find indices of AIMessages with tool_calls.
    tool_indices: list[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        _text, tcs = _split_blocks(msg)
        if tcs:
            tool_indices.append(i)

    if not tool_indices or len(tool_indices) == 1:
        return []

    first_tool = tool_indices[0]
    last_tool = tool_indices[-1]
    suppressed: list[str] = []
    for i in range(first_tool + 1, last_tool + 1):
        msg = messages[i]
        if not isinstance(msg, AIMessage):
            continue
        text, _ = _split_blocks(msg)
        if text:
            suppressed.append(text)
    return suppressed


__all__ = [
    "EXPLICIT_SEND_TOOL",
    "StreamingAutoDispatcher",
    "StreamingState",
    "advance_state",
    "intermediate_text_segments",
]
