"""turns.jsonl writer + Claude Agent SDK message → events extractor (SPEC §10).

Schema is open-strix's ``TurnRecord`` plus two mimir additions: ``saga_session_id``
and ``saga_atom_ids`` (SPEC §10.2). The ``events`` list shape — reasoning /
tool_call / tool_result entries — is identical so existing tooling
(benchmark/scripts/collate_turns.py, benchmark/overview_turns.py) keeps working.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .models import TurnRecord

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_BYTES = 4 * 1024
MAX_INPUT_BYTES = 2 * 1024
DEFAULT_MAX_TURNS = 1000


def truncate_input(prompt: str) -> str:
    if len(prompt) <= MAX_INPUT_BYTES:
        return prompt
    return prompt[:MAX_INPUT_BYTES] + "…[truncated]"


def _coerce_tool_result_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # MCP can return [{"type": "text", "text": "..."}, ...]
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def extract_turn_events(messages: list[Message]) -> tuple[list[dict[str, Any]], str]:
    """Walk a Claude Agent SDK message stream and produce ``(events, output)``.

    Mapping (SPEC §10.3):
      AssistantMessage with TextBlock + ToolUseBlocks → reasoning + tool_call
      AssistantMessage with only TextBlock(s)         → appended to output
      AssistantMessage with ThinkingBlock             → reasoning event
      UserMessage with ToolResultBlock(s)             → tool_result events

    Subagent messages (``parent_tool_use_id`` is set) are skipped — only the
    parent's ``Agent`` tool_call and its tool_result land in the parent log.
    The subagent's internal tool calls are visible to the SDK but not
    flattened here. (Per-subagent log files are a Phase 5 stretch.)

    Tool-result ``name`` is filled by correlating ``tool_use_id`` against the
    preceding ``tool_call`` events; the SDK's ``ToolResultBlock`` carries only
    the id.
    """
    events: list[dict[str, Any]] = []
    output_parts: list[str] = []
    tool_name_by_id: dict[str, str] = {}

    for msg in messages:
        # Skip subagent-internal turns. Top-level messages have parent_tool_use_id=None.
        if getattr(msg, "parent_tool_use_id", None) is not None:
            continue
        if isinstance(msg, AssistantMessage):
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_uses: list[ToolUseBlock] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    thinking_parts.append(block.thinking)
                elif isinstance(block, ToolUseBlock):
                    tool_uses.append(block)

            if thinking_parts:
                events.append({"type": "reasoning", "content": "\n".join(thinking_parts)})

            if text_parts and tool_uses:
                # Text alongside tool calls reads as reasoning that precedes the call.
                events.append({"type": "reasoning", "content": "\n".join(text_parts)})
            elif text_parts:
                output_parts.extend(text_parts)

            for tu in tool_uses:
                tool_name_by_id[tu.id] = tu.name
                events.append({
                    "type": "tool_call",
                    "id": tu.id,
                    "name": tu.name,
                    "args": tu.input,
                })

        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        body = _coerce_tool_result_content(block.content)
                        if len(body) > MAX_TOOL_RESULT_BYTES:
                            body = body[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
                        events.append({
                            "type": "tool_result",
                            "id": block.tool_use_id,
                            "name": tool_name_by_id.get(block.tool_use_id, ""),
                            "content": body,
                            "is_error": bool(block.is_error),
                        })

    return events, "\n".join(output_parts)


class TurnLogger:
    """Append-only JSONL with bounded retention. Lock-serialized writes."""

    def __init__(self, path: Path, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self._path = path
        self._max_turns = max(1, max_turns)
        self._line_count = 0
        self._lock: asyncio.Lock | None = None

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                self._line_count = sum(
                    1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                )
            except OSError:
                self._line_count = 0

    async def write(self, record: TurnRecord) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._write(record)

    async def _write(self, record: TurnRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=True, default=str)
        try:
            # Recreate the parent dir if it was removed out-of-band
            # (e.g. a benchmark cleanup script wiped logs/ while we ran).
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._line_count += 1
            # Hysteresis: trim only when over cap by ≥10%. Same rationale
            # as event_logger — avoids O(file) rewrite on every write past
            # the cap.
            if self._line_count > self._max_turns + max(self._max_turns // 10, 1):
                await self._trim()
        except OSError as exc:
            log.warning("turns.jsonl write failed: %s", exc)

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
            log.warning("turns.jsonl trim failed: %s", exc)
