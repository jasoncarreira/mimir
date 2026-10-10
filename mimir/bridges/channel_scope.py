"""Pure, platform-independent inbound channel admission policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ChannelScope:
    allowed: frozenset[str] | None = None
    ignored: frozenset[str] = field(default_factory=frozenset)
    require_mention: bool = False
    free_response: frozenset[str] = field(default_factory=frozenset)
    allow_bots: Literal["none", "mentions", "all"] = "none"
    allowed_bot_ids: frozenset[str] = field(default_factory=frozenset)


def admit(
    scope: ChannelScope, *, channel_id: str, parent_channel_id: str | None,
    is_dm: bool, mentioned: bool, author_is_bot: bool, author_id: str | None,
) -> tuple[bool, str]:
    """Decide admission before intake; a thread matches either its own or parent id."""
    ids = {channel_id, parent_channel_id}
    if not is_dm:
        if ids & scope.ignored:
            return False, "ignored_channel"
        if scope.allowed is not None and not ids & scope.allowed:
            return False, "channel_not_allowed"
    if author_is_bot:
        if author_id in scope.allowed_bot_ids:
            return True, "admitted"
        if scope.allow_bots == "none" or (scope.allow_bots == "mentions" and not mentioned):
            return False, "bot_author"
    if not is_dm and scope.require_mention and not ids & scope.free_response and not mentioned:
        return False, "mention_required"
    return True, "admitted"
