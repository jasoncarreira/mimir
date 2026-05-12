"""``mimir_get_turn`` MCP tool (SYNTHESIS_AND_BUDGET_FIXES.md change 1).

Lets the synthesis turn fetch a single turn's full content from
``turns.jsonl`` on demand, instead of having every turn's full record
JSON-dumped into the synthesis prompt up front. The synthesis template
now embeds metadata-only summaries; if memory capture wants to look
closer at a particular turn (its tool sequence, reasoning blocks, full
output), it calls this tool with the turn_id from the summary line.

Returns ``output`` and ``events`` only — drops ``input`` deliberately
(re-embedding the rendered prompt that fed an earlier turn is exactly
the cubic blowup we're avoiding) and trims internal bookkeeping fields
(``usage``, ``permission_denials``, ``saga_session_id``, …) the agent
doesn't need for memory capture.

Path-confined to the configured ``turns_log`` — the tool can't fetch
arbitrary files. Unknown turn_ids return a graceful is_error block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._tool_helpers import _content_block, _need, _safe


def build_turn_tools(turns_log: Path) -> list[SdkMcpTool]:
    @tool(
        "get_turn",
        "Fetch a single turn's output and events from turns.jsonl. "
        "Used by the saga_session_end synthesis turn to look at a "
        "specific turn's full content (the synthesis prompt only "
        "embeds metadata-only summaries up front to keep cost down). "
        "Returns {turn_id, trigger, output, events}. The original "
        "rendered prompt (input field) is intentionally not returned — "
        "re-embedding it would re-replay the prior session's history.",
        {"turn_id": str},
    )
    @_safe("get_turn", param_names=["turn_id"])
    async def get_turn(args: dict[str, Any]) -> dict[str, Any]:
        turn_id = _need(args, "turn_id")
        if not turns_log.is_file():
            return _content_block(
                f"get_turn failed: turns log not found at {turns_log}",
                is_error=True,
            )
        try:
            with turns_log.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("turn_id") != turn_id:
                        continue
                    payload = {
                        "turn_id": rec.get("turn_id"),
                        "trigger": rec.get("trigger"),
                        "output": rec.get("output", ""),
                        "events": rec.get("events", []),
                    }
                    return _content_block(
                        json.dumps(payload, ensure_ascii=False, default=str, indent=2)
                    )
        except OSError as exc:
            return _content_block(
                f"get_turn failed: could not read turns log ({exc})",
                is_error=True,
            )
        return _content_block(
            f"get_turn: no turn found with turn_id={turn_id!r}",
            is_error=True,
        )

    return [get_turn]


def turn_tool_names() -> list[str]:
    return ["mcp__mimir__get_turn"]
