"""Shared ``ChannelMessage`` type for bridge history fetches.

The agent calls ``fetch_channel_history`` to catch up on a channel
the bridge didn't see live (bot was offline, restart between message
and reply, late-joining a thread). The bridge converts platform-
specific message objects into a uniform ``ChannelMessage`` shape so
the agent's prompt format stays portable across Discord / Slack /
future transports.

Returned messages are oldest-first — agent reads them in
conversational order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChannelMessage:
    id: str
    ts: str  # ISO-8601 UTC
    author_id: str | None
    author_display: str | None
    is_bot: bool
    content: str
    attachment_urls: tuple[str, ...] = ()
    # Platform-specific extras (thread_ts, reply_count, edited flag, etc.).
    # Optional — the agent reads through the formatted text rendering, so
    # this is mostly for diagnostic logging and future fields we don't
    # want to bake into the type signature yet.
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["ChannelMessage"]
