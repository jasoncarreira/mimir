"""Turn logger for the deepagents PoC.

Adapter pattern proven by ``open-strix/open_strix/turn_logger.py``:
walk LangChain's `AIMessage` / `ToolMessage` list, emit mimir's event
schema (``{type: "reasoning"|"tool_call"|"tool_result", ...}``).

Mimir's current ``TurnRecord`` (mimir/models.py) has fields the SDK-
specific path populates from ``ResultMessage`` events. Here we
populate them from LangChain's message surfaces:

  Mimir TurnRecord field   ← LangChain source
  ────────────────────────────────────────────────────────────────────
  result_subtype           ← inferred from final AIMessage's stop reason
                             (success / error_max_turns / error)
  result_is_error          ← True if final message has an error
  stop_reason              ← AIMessage.response_metadata["stop_reason"]
                             or ["finish_reason"]
  num_turns                ← count of AI messages with tool_calls + 1
  total_cost_usd           ← AIMessage.usage_metadata (if cost available
                             from langchain-anthropic)
  usage                    ← aggregate usage_metadata across AI messages
  permission_denials       ← [] (deepagents permission middleware records
                             these separately; PoC doesn't surface them)

  saga_session_id, saga_atom_ids, saga_calls, kind: passed in by caller
  (these are mimir-specific, not derivable from messages alone).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_BYTES = 4 * 1024
MAX_INPUT_BYTES = 2 * 1024
DEFAULT_MAX_TURNS = 1000


@dataclass
class TurnRecord:
    """Mirror mimir's mimir/models.py:TurnRecord shape — the existing
    bench tooling / turn viewer / ops dashboard all read this schema."""
    ts: str
    turn_id: str
    session_id: str
    saga_session_id: str | None
    trigger: str
    channel_id: str | None
    input: str
    saga_atom_ids: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    duration_ms: int = 0
    error: str | None = None
    result_subtype: str | None = None
    result_is_error: bool | None = None
    stop_reason: str | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    permission_denials: list[Any] = field(default_factory=list)
    kind: str | None = None
    saga_calls: list[dict[str, Any]] = field(default_factory=list)


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
) -> tuple[list[dict[str, Any]], str]:
    """Walk a LangChain message list, return (events, output).

    Lifted from open-strix/open_strix/turn_logger.py:55 — proven pattern.
    Schema is identical to mimir's SDK-based extractor in mimir/turn_logger.py.
    """
    events: list[dict[str, Any]] = []
    output_parts: list[str] = []

    for msg in messages:
        if isinstance(msg, AIMessage):
            content_text = _coerce_content(msg.content)
            if content_text and msg.tool_calls:
                # Reasoning preceding tool calls — preserved in time order
                events.append({"type": "reasoning", "content": content_text})
            elif content_text:
                # Plain assistant reply (no tools) — accumulates as output
                output_parts.append(content_text)
            for tc in (msg.tool_calls or []):
                events.append({
                    "type": "tool_call",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", "unknown"),
                    "args": tc.get("args"),
                })
        elif isinstance(msg, ToolMessage):
            content = _coerce_content(msg.content)
            if len(content) > MAX_TOOL_RESULT_BYTES:
                content = content[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
            events.append({
                "type": "tool_result",
                "id": getattr(msg, "tool_call_id", ""),
                "name": getattr(msg, "name", ""),
                "content": content,
                "is_error": getattr(msg, "status", None) == "error",
            })

    return events, "\n".join(output_parts).strip()


def derive_result_fields(messages: list[Any]) -> dict[str, Any]:
    """Pull the SDK-equivalent result fields out of the final AIMessage.

    Best-effort — different model providers populate ``response_metadata``
    and ``usage_metadata`` inconsistently. Returns None for fields we
    can't resolve, matching mimir's existing nullable contract.
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

    # langchain-anthropic populates usage_metadata with input_tokens /
    # output_tokens / cache_read_input_tokens / cache_creation_input_tokens
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None

    # Aggregate across all AI messages (multi-step turns add up)
    agg_in = agg_out = agg_cache_read = agg_cache_create = 0
    has_usage = False
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.usage_metadata:
            has_usage = True
            u = msg.usage_metadata
            agg_in += u.get("input_tokens", 0)
            agg_out += u.get("output_tokens", 0)
            # cache fields live under input_token_details on some providers
            details = u.get("input_token_details", {}) or {}
            agg_cache_read += details.get("cache_read", 0)
            agg_cache_create += details.get("cache_creation", 0)
    if has_usage:
        usage = {
            "input_tokens": agg_in,
            "output_tokens": agg_out,
            "cache_read_input_tokens": agg_cache_read,
            "cache_creation_input_tokens": agg_cache_create,
        }

    # Count of AI messages = num_turns (rough mapping of the SDK's count)
    num_turns = sum(1 for m in messages if isinstance(m, AIMessage)) or None

    # Subtype: success unless we see an error message at the end
    result_subtype = "success"
    result_is_error = False
    if stop_reason == "max_turns" or stop_reason == "max_tokens":
        result_subtype = "error_max_turns"
        result_is_error = True

    return {
        "result_subtype": result_subtype,
        "result_is_error": result_is_error,
        "stop_reason": stop_reason,
        "num_turns": num_turns,
        "total_cost_usd": total_cost_usd,  # langchain-anthropic doesn't surface USD directly
        "usage": usage,
    }


class TurnLogger:
    """Append-only JSONL turn log. Identical surface to open-strix's
    TurnLogger; serializes writes via asyncio.Lock to prevent
    concurrent trim races.
    """

    def __init__(self, path: Path, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self._path = path
        self._max_turns = max(1, max_turns)
        self._line_count = 0
        self._write_lock: Any = None
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                self._line_count = sum(
                    1 for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            except OSError:
                self._line_count = 0

    async def write(self, record: TurnRecord) -> None:
        if self._write_lock is None:
            import asyncio
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            line = json.dumps(asdict(record), ensure_ascii=True, default=str)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._line_count += 1
                if self._line_count > self._max_turns:
                    await self._trim()
            except OSError as exc:
                log.warning("Failed to write turn record: %s", exc)

    async def _trim(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) <= self._max_turns:
                return
            kept = lines[-self._max_turns:]
            tmp = self._path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.rename(self._path)
            self._line_count = len(kept)
        except OSError as exc:
            log.warning("Failed to trim turn log: %s", exc)


def make_turn_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]
