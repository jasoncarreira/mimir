"""Per-channel queue of completed-subagent notifications (SPEC §4.4).

When the parent fires ``Agent("climber", ..., background=True)`` the SDK
returns immediately. The parent's message stream then receives:
- ``TaskStartedMessage``
- ``TaskProgressMessage`` (periodic)
- ``TaskNotificationMessage`` (on completion, with output_file + summary)

Inside the same turn we can't act on the notification — by then the model
has already produced its assistant message. Instead we:
1. Drain Task* events at end-of-turn into ``SubagentInbox.push(channel_id, ...)``.
2. At the next turn for that channel, ``drain(channel_id)`` returns any
   pending notifications. The agent injects them into the turn prompt as
   ``## Subagent updates`` so the model can react.

This is the spec's "subagent_inbox queue" pattern (§4.4 #2). Verified
empirically: SDK ``_internal/message_parser.py:191-203`` parses
``task_notification`` and yields it on the parent's stream — no polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

MAX_SUMMARY_BYTES = 2 * 1024


@dataclass
class SubagentResult:
    task_id: str
    status: str                     # "completed" | "failed" | "stopped"
    summary: str
    output_file: str | None         # SDK-managed path with the full final result
    description: str | None = None  # task description (from TaskStartedMessage)
    usage: dict[str, Any] | None = None
    received_ts: str | None = None  # ISO timestamp


@dataclass
class SubagentInbox:
    """One per process. Per-channel deque of pending notifications."""

    by_channel: dict[str, list[SubagentResult]] = field(default_factory=dict)
    _lock: asyncio.Lock | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def push(self, channel_id: str, result: SubagentResult) -> None:
        async with self._ensure_lock():
            self.by_channel.setdefault(channel_id, []).append(result)

    async def drain(self, channel_id: str) -> list[SubagentResult]:
        """Return all pending notifications for a channel, clearing the bucket."""
        async with self._ensure_lock():
            out = self.by_channel.pop(channel_id, [])
        return out

    def peek(self, channel_id: str) -> list[SubagentResult]:
        """Non-destructive view (used by tests + the viewer in later phases)."""
        return list(self.by_channel.get(channel_id, []))


def render_subagent_updates(results: Iterable[SubagentResult]) -> str:
    """Format pending notifications as a turn-prompt section."""
    lines: list[str] = []
    for r in results:
        head = f"- [{r.status}] task_id={r.task_id}"
        if r.description:
            head += f" — {r.description}"
        lines.append(head)
        if r.summary:
            summary = r.summary
            if len(summary) > MAX_SUMMARY_BYTES:
                summary = summary[:MAX_SUMMARY_BYTES] + "…[truncated]"
            lines.append(f"  summary: {summary}")
        if r.output_file:
            lines.append(f"  output_file: {r.output_file}")
    return "\n".join(lines)


def read_output_file(output_file: str | None, max_bytes: int = 32 * 1024) -> str | None:
    """Convenience for the agent — read a subagent's output_file (if any).
    Capped at ``max_bytes`` to keep the prompt manageable."""
    if not output_file:
        return None
    p = Path(output_file)
    if not p.is_file():
        return None
    try:
        body = p.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("output_file read failed for %s: %s", output_file, exc)
        return None
    if len(body) > max_bytes:
        return body[:max_bytes] + "\n…[truncated]"
    return body
