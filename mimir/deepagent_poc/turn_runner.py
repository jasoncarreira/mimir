"""End-to-end ``run_turn`` — the deepagents-path equivalent of
mimir's ``Agent._run_query_loop`` (mimir/agent.py).

Order of operations matches mimir's existing SDK path:

  1. Build context from prior channel messages (if any)
  2. PRE-MESSAGE hook: memory_client.query + format payload
  3. agent.ainvoke({"messages": [augmented_HumanMessage]})
  4. Extract events + output from result.messages
  5. POST-MESSAGE hook: feedback() against the union of
     (pre-message atom IDs + in-turn tool-result atom IDs)
  6. Write turn record to turn log
  7. Return TurnOutcome (telemetry envelope)

Open-strix's app.py is structurally identical (lines 1140-1250):
  pre_hook → ainvoke → extract → post_hook → log

This is the right migration shape: a single ``run_turn`` entry point
that wraps deepagents and returns a typed telemetry envelope. mimir's
existing dispatcher / channel layer calls this in place of the SDK.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mimir.memory.client import MemoryClient
from .post_message_hook import PostMessageResult, run_post_message
from .pre_message_hook import PreMessageResult, invoke_with_pre_message
from .turn_logger import (
    TurnLogger,
    TurnRecord,
    derive_result_fields,
    extract_turn_events,
    make_turn_id,
    truncate_input,
)


@dataclass
class TurnOutcome:
    """Returned to the caller (dispatcher / bench harness). Carries
    everything mimir's existing call sites consume."""
    turn_id: str
    output: str
    error: str | None
    hypothesis_for_bench: str  # alias for output, here for clarity
    pre_message: PreMessageResult | None
    post_message: PostMessageResult | None
    turn_record: TurnRecord
    duration_ms: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_turn(
    agent: Any,
    *,
    memory_client: MemoryClient,
    question: str,
    session_id: str,
    channel_id: str | None = None,
    saga_session_id: str | None = None,
    context_messages: list[dict[str, str]] | None = None,
    trigger: str = "user_message",
    top_k: int = 12,
    min_confidence_tier: str | None = None,
    reference_date: Any = None,
    feedback_signal: str = "positive",
    turn_logger: TurnLogger | None = None,
    config: dict[str, Any] | None = None,
) -> TurnOutcome:
    """One agent turn through the deepagents PoC pipeline.

    Mimics the SDK path's full lifecycle: pre-hook → invoke → post-hook
    → log. Returns a TurnOutcome with the agent's reply plus the
    typed telemetry envelopes the bench / ops dashboard / turn viewer
    consume.

    Failures during agent invocation are caught and surfaced through
    ``TurnOutcome.error``; the turn record is still written (matches
    mimir's fail-soft contract — observability shouldn't disappear
    when the agent crashes).
    """
    turn_id = make_turn_id()
    t_total_start = time.monotonic()

    # 1+2. PRE-MESSAGE hook + agent invoke (bundled by invoke_with_pre_message).
    error: str | None = None
    messages: list[Any] = []
    output = ""
    pre: PreMessageResult | None = None
    try:
        result, pre = await invoke_with_pre_message(
            agent,
            memory_client=memory_client,
            question=question,
            context_messages=context_messages,
            top_k=top_k,
            session_id=saga_session_id,
            min_confidence_tier=min_confidence_tier,
            reference_date=reference_date,
            config=config,
        )
        messages = result.get("messages", [])
        events, output = extract_turn_events(messages)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        events = []

    # 3. POST-MESSAGE credit pass.
    post: PostMessageResult | None = None
    if error is None and pre is not None:
        post = await run_post_message(
            memory_client=memory_client,
            response_text=output,
            pre_message_atom_ids=pre.saga_atom_ids,
            agent_messages=messages,
            session_id=saga_session_id,
            feedback=feedback_signal,
        )

    # 4. Build TurnRecord (mimir's existing schema).
    result_fields = derive_result_fields(messages)
    duration_ms = int((time.monotonic() - t_total_start) * 1000)
    record = TurnRecord(
        ts=_utc_now(),
        turn_id=turn_id,
        session_id=session_id,
        saga_session_id=saga_session_id,
        trigger=trigger,
        channel_id=channel_id,
        input=truncate_input(question),
        saga_atom_ids=(pre.saga_atom_ids if pre else []),
        events=events,
        output=output[:2048],
        duration_ms=duration_ms,
        error=error,
        **result_fields,
    )
    if turn_logger is not None:
        await turn_logger.write(record)

    return TurnOutcome(
        turn_id=turn_id,
        output=output,
        error=error,
        hypothesis_for_bench=output,
        pre_message=pre,
        post_message=post,
        turn_record=record,
        duration_ms=duration_ms,
    )
