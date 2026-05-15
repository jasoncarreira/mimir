"""Streaming auto-dispatcher — post-cutover stub.

Pre-cutover (chainlink #5) this consumed the SDK's StreamEvent
yields to deliver intermediate text to the channel as it arrived.
LangGraph has its own streaming surface (``agent.astream(...)``)
which the deepagent-backed Agent doesn't yet use — turns currently
complete-then-deliver via the final text message.

Phase D: re-add streaming using deepagents' astream so chunks land
on the channel as the model emits them.
"""
from __future__ import annotations

from typing import Any


class StreamingAutoDispatcher:
    """No-op streaming dispatcher post-cutover."""
    def __init__(self, *args, **kwargs):
        self._enabled = False

    def observe(self, *args, **kwargs) -> None:
        return None

    async def flush(self) -> None:
        return None
