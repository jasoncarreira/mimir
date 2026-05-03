"""Shared emoji alias table + per-transport resolvers.

The agent emits emoji as either ``:alias:`` shortcodes or unicode glyphs
in ``<react>`` directives; each transport wants a different shape:

- Discord: unicode glyph or ``<:name:id>`` for custom server emoji.
- Slack:   ``name`` (without colons) — the API takes the alias directly.

The alias table is the source of truth for cross-transport aliases.
Custom-emoji forms (Discord ``<:foo:123>`` and Slack workspace-only
aliases) are passed through unchanged — the agent gets them from the
per-channel emoji vocabulary block in the turn prompt.

Lifted from lettabot (~/projects/letta/lettabot/src/channels/shared/emoji.ts);
Python port with the same alias set so cross-bot guidance is portable.
"""

from __future__ import annotations

import re

# Alias → unicode glyph. Keep small + obvious; the agent can also emit
# raw unicode directly and we pass it through.
EMOJI_ALIASES: dict[str, str] = {
    "eyes": "\U0001F440",
    "thumbsup": "\U0001F44D",
    "thumbs_up": "\U0001F44D",
    "+1": "\U0001F44D",
    "thumbsdown": "\U0001F44E",
    "-1": "\U0001F44E",
    "heart": "❤️",
    "fire": "\U0001F525",
    "smile": "\U0001F604",
    "laughing": "\U0001F606",
    "tada": "\U0001F389",
    "clap": "\U0001F44F",
    "ok_hand": "\U0001F44C",
    "white_check_mark": "✅",
    "x": "❌",
    "warning": "⚠️",
    "rocket": "\U0001F680",
    "sparkles": "✨",
    "thinking": "\U0001F914",
    "confused": "\U0001F615",
}


# Reverse map: unicode glyph → canonical alias (for Slack which wants names).
# Built from EMOJI_ALIASES, taking the first alias that maps to each glyph.
_UNICODE_TO_ALIAS: dict[str, str] = {}
for _name, _glyph in EMOJI_ALIASES.items():
    _UNICODE_TO_ALIAS.setdefault(_glyph, _name)


_ALIAS_RE = re.compile(r"^:([^:\s]+):$")
_DISCORD_CUSTOM_RE = re.compile(r"^<a?:[A-Za-z0-9_]+:\d+>$")


def resolve_for_discord(emoji: str) -> str:
    """Return the form discord-py wants for ``message.add_reaction``.

    Accepts ``:alias:``, bare ``alias``, raw unicode, or a Discord custom
    emoji literal (``<:name:id>`` / ``<a:name:id>``). Aliases resolve to
    their unicode glyph; everything else passes through (custom emoji
    literals, unknown unicode the alias table doesn't know about).
    """
    s = emoji.strip()
    if not s:
        return s
    if _DISCORD_CUSTOM_RE.match(s):
        return s
    m = _ALIAS_RE.match(s)
    if m and m.group(1) in EMOJI_ALIASES:
        return EMOJI_ALIASES[m.group(1)]
    if s in EMOJI_ALIASES:
        return EMOJI_ALIASES[s]
    return s


def resolve_for_slack(emoji: str) -> str | None:
    """Return the alias form Slack's reactions API wants — bare name, no
    colons. Returns None if we can't map the input to an alias the
    Slack workspace can resolve.

    Inputs accepted:
    - ``":alias:"`` → ``"alias"`` (passes through to Slack as-is)
    - ``"alias"`` if it's a known alias → ``"alias"``
    - unicode glyph that has a known alias → reverse-lookup
    - unicode glyph with no known alias → None (caller logs and skips)
    """
    s = emoji.strip()
    if not s:
        return None
    m = _ALIAS_RE.match(s)
    if m:
        return m.group(1)
    if s in EMOJI_ALIASES:
        return s
    return _UNICODE_TO_ALIAS.get(s)


__all__ = [
    "EMOJI_ALIASES",
    "resolve_for_discord",
    "resolve_for_slack",
]
