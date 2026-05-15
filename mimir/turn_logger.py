"""turns.jsonl writer + LangChain message → events extractor.

Post-cutover (2026-05-14): replaces the claude_agent_sdk message
types with langchain_core.messages (AIMessage, ToolMessage,
HumanMessage). Schema is unchanged — the existing bench tooling /
benchmark/scripts/collate_turns.py / benchmark/overview_turns.py /
turn viewer all read this output without modification.

What's dropped from the SDK version (Phase D cleanup, can re-add):
  - streaming_active intermediate-narration demotion (chainlink #5)
  - message_t_ms per-event timestamping
  - parent_tool_use_id subagent-message skipping (deepagents
    ``task`` tool has its own subagent shape)

These are kept as stubs in the function signature so call sites
that pass them through don't break — they're just ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ._jsonl_tail import _tail_lines, count_lines_chunked
from .models import TurnRecord

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_BYTES = 4 * 1024
MAX_INPUT_BYTES = 2 * 1024
DEFAULT_MAX_TURNS = 1000


def truncate_input(prompt: str) -> str:
    if len(prompt) <= MAX_INPUT_BYTES:
        return prompt
    return prompt[:MAX_INPUT_BYTES] + "…[truncated]"


def _coerce_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # LangChain can return [{"type": "text", "text": "..."}, ...]
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def extract_turn_events(
    messages: list[Any],
    *,
    streaming_active: bool = False,    # kept for API compat; ignored
    message_t_ms: list[float] | None = None,  # kept for API compat; ignored
) -> tuple[list[dict[str, Any]], str]:
    """Walk a LangChain message list, return ``(events, output)``.

    Schema (matches the SDK version's output):
      AIMessage with content + tool_calls  → reasoning + tool_call events
      AIMessage with only content          → appended to output
      ToolMessage                          → tool_result event

    streaming_active + message_t_ms are accepted for back-compat
    with the SDK version's call sites; ignored in this build.
    """
    events: list[dict[str, Any]] = []
    output_parts: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            content_text = _coerce_content(msg.content)
            if content_text and msg.tool_calls:
                events.append({"type": "reasoning", "content": content_text})
            elif content_text:
                output_parts.append(content_text)
            for tc in (msg.tool_calls or []):
                events.append({
                    "type": "tool_call",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", "unknown"),
                    "args": tc.get("args"),
                })
        elif isinstance(msg, ToolMessage):
            body = _coerce_content(msg.content)
            if len(body) > MAX_TOOL_RESULT_BYTES:
                body = body[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
            events.append({
                "type": "tool_result",
                "id": getattr(msg, "tool_call_id", ""),
                "name": getattr(msg, "name", ""),
                "content": body,
                "is_error": getattr(msg, "status", None) == "error",
            })
    return events, "\n".join(output_parts)


def derive_result_fields(messages: list[Any]) -> dict[str, Any]:
    """Pull SDK-equivalent ResultMessage fields from LangChain messages.

    LangChain stores stop_reason in response_metadata, usage in
    usage_metadata. langchain-anthropic / langchain-openai populate
    these; some providers populate them inconsistently. Returns None
    for fields we can't resolve — matches mimir's existing nullable
    contract for these fields.
    """
    final_ai: AIMessage | None = None
    for msg in messages:
        if isinstance(msg, AIMessage):
            final_ai = msg

    if final_ai is None:
        return {
            "result_subtype": None,
            "result_is_error": None,
            "stop_reason": None,
            "num_turns": None,
            "total_cost_usd": None,
            "usage": None,
        }

    md = final_ai.response_metadata or {}
    stop_reason = md.get("stop_reason") or md.get("finish_reason")

    # Aggregate usage_metadata across all AI messages.
    agg_in = agg_out = agg_cache_read = agg_cache_create = 0
    has_usage = False
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.usage_metadata:
            has_usage = True
            u = msg.usage_metadata
            agg_in += u.get("input_tokens", 0)
            agg_out += u.get("output_tokens", 0)
            details = u.get("input_token_details", {}) or {}
            agg_cache_read += details.get("cache_read", 0)
            agg_cache_create += details.get("cache_creation", 0)
    usage = None
    if has_usage:
        usage = {
            "input_tokens": agg_in,
            "output_tokens": agg_out,
            "cache_read_input_tokens": agg_cache_read,
            "cache_creation_input_tokens": agg_cache_create,
        }

    num_turns = sum(1 for m in messages if isinstance(m, AIMessage)) or None
    result_subtype = "success"
    result_is_error = False
    if stop_reason in ("max_turns", "max_tokens"):
        result_subtype = "error_max_turns"
        result_is_error = True

    return {
        "result_subtype": result_subtype,
        "result_is_error": result_is_error,
        "stop_reason": stop_reason,
        "num_turns": num_turns,
        "total_cost_usd": None,  # not surfaced by langchain-anthropic/openai uniformly
        "usage": usage,
    }


class TurnLogger:
    """Append-only JSONL with bounded retention. Lock-serialized writes.

    Identical surface to the SDK-side TurnLogger — same TurnRecord
    schema, same _jsonl_tail helpers. The only change is the
    underlying event extractor lives in this module instead of
    consuming SDK message types.
    """

    def __init__(self, path: Path, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self._path = path
        self._max_turns = max(1, max_turns)
        self._line_count = 0
        self._lock: asyncio.Lock | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._line_count = count_lines_chunked(path)

    async def write(self, record: TurnRecord) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._write(record)

    async def _write(self, record: TurnRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=True, default=str)
        try:
            await asyncio.to_thread(self._append_line, line)
            self._line_count += 1
            # Hysteresis: trim only when over cap by ≥10%.
            if self._line_count > int(self._max_turns * 1.1):
                await self._trim()
        except OSError as exc:
            log.warning("Failed to write turn record: %s", exc)

    def _append_line(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def _trim(self) -> None:
        await asyncio.to_thread(self._trim_sync)

    def _trim_sync(self) -> None:
        try:
            keep = _tail_lines(self._path, self._max_turns)
            tmp = self._path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            tmp.rename(self._path)
            self._line_count = len(keep)
        except OSError as exc:
            log.warning("Failed to trim turn log: %s", exc)


def make_turn_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]
