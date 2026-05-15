"""Post-message hook: credit-pass after agent.ainvoke completes.

Mimir's existing _post_message_hook (mimir/agent.py:_post_message_hook)
does three things:

  1. Gather every atom_id surfaced this turn — both the pre-message
     hook's atoms AND atoms surfaced by in-turn ``saga_query`` tool
     calls. Triple-source atoms too (when the agent grounded its
     answer in a (s,p,o) fact, the source atom earned its keep).
  2. Call ``saga_client.feedback(atom_ids, response_text)`` —
     translates to ``MemoryClient.feedback`` in the cutover. This
     writes ``feedback_positive`` access_events that boost the cited
     atoms' activation scores for future recall.
  3. Capture timing metrics (post_message_ms) for the turn record.

This is again an external wrapper, not deepagents middleware. The
credit pass should fire ONCE after the agent's final reply — not
after every model call within a multi-step turn.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from mimir.memory.client import MemoryClient


# Match atom-id-shaped strings in tool result text. mimir's atom IDs
# are 16-char hex (sha256[:16]); saga's atom IDs are 16-char hex too.
# This regex is intentionally lax — false matches are harmless because
# feedback() rejects unknown IDs at the SQL layer.
_ATOM_ID_RE = re.compile(r"\b[0-9a-f]{16}\b")


@dataclass
class PostMessageResult:
    """Outcome of the credit pass."""
    atom_ids_credited: list[str]
    feedback_ok: bool
    feedback_error: str | None
    post_message_ms: int


def _extract_atom_ids_from_tool_results(messages: list[Any]) -> list[str]:
    """Walk ToolMessages in the agent's message list. For tool calls
    that returned a memory payload, pull every atom-id-shaped substring
    out of the tool result content.

    Why: mimir's pre-message hook captures atom IDs at the API
    boundary. But if the agent ALSO called ``memory_query`` as a
    follow-up mid-turn, those atom IDs need crediting too. The tool
    result is a string (formatted by ``_format_saga_payload``), so we
    pattern-match for the IDs.

    Cheap + good-enough: the formatted payload doesn't naturally
    contain other 16-hex strings, so false positives are rare. If
    they happen, ``MemoryClient.feedback`` silently drops unknown
    atom_ids at the SQL level.
    """
    from langchain_core.messages import ToolMessage
    found: set[str] = set()
    out: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            for match in _ATOM_ID_RE.findall(content):
                if match not in found:
                    found.add(match)
                    out.append(match)
    return out


async def run_post_message(
    *,
    memory_client: MemoryClient,
    response_text: str,
    pre_message_atom_ids: list[str],
    agent_messages: list[Any],
    session_id: str | None = None,
    feedback: str = "positive",
) -> PostMessageResult:
    """Credit atoms that contributed to the answer.

    Union: pre-message-hook atom IDs + in-turn memory_query tool result
    atom IDs. Empty atom IDs list → no feedback call (matches mimir's
    behavior of skipping the credit pass when there was nothing to
    credit).
    """
    t0 = time.monotonic()
    # Union pre-message + in-turn surfaced atom IDs, preserving order.
    seen: set[str] = set()
    all_ids: list[str] = []
    for aid in pre_message_atom_ids:
        if aid not in seen:
            seen.add(aid)
            all_ids.append(aid)
    for aid in _extract_atom_ids_from_tool_results(agent_messages):
        if aid not in seen:
            seen.add(aid)
            all_ids.append(aid)

    if not all_ids:
        return PostMessageResult(
            atom_ids_credited=[],
            feedback_ok=True,  # nothing to do = trivially ok
            feedback_error=None,
            post_message_ms=int((time.monotonic() - t0) * 1000),
        )

    try:
        await memory_client.feedback(
            all_ids,
            response_text,
            session_id=session_id,
            feedback=feedback,
        )
        return PostMessageResult(
            atom_ids_credited=all_ids,
            feedback_ok=True,
            feedback_error=None,
            post_message_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as exc:
        return PostMessageResult(
            atom_ids_credited=all_ids,
            feedback_ok=False,
            feedback_error=f"{type(exc).__name__}: {exc}",
            post_message_ms=int((time.monotonic() - t0) * 1000),
        )
