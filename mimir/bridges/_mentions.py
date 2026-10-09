"""Outbound text guards shared by the chat bridges and operator notices."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_SLACK_BROADCAST = re.compile(
    r"<!(here|channel|everyone|subteam\^[^<>|]+)(?:\|[^<>]*)?>",
    re.IGNORECASE,
)
_DISPLAY_SIGILS = frozenset("@<>[]()`*_~#")


def neutralize_slack_broadcasts(text: str) -> str:
    """Render Slack broadcast and user-group tokens as inert plain text."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        return "@subteam" if name.startswith("subteam^") else f"@{name}"

    # A token inside a label can expose an outer token after substitution.
    # Repeat until neither the inner nor the newly exposed outer token is live.
    while True:
        cleaned = _SLACK_BROADCAST.sub(replace, text)
        if cleaned == text:
            return cleaned
        text = cleaned


def neutralize_slack_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy Block Kit payloads, cleaning text objects at any nesting depth."""

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            result = {key: clean(item) for key, item in value.items()}
            if value.get("type") in ("mrkdwn", "plain_text") and isinstance(value.get("text"), str):
                result["text"] = neutralize_slack_broadcasts(value["text"])
            return result
        return value

    return clean(blocks)


def neutralize_display_name(name: str, max_chars: int = 64) -> str:
    """Remove mention/formatting sigils and controls from an untrusted name."""

    cleaned = "".join(
        char for char in name
        if char not in _DISPLAY_SIGILS and unicodedata.category(char) != "Cc"
    )
    return " ".join(cleaned.split())[:max_chars]
