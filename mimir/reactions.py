"""Inbound reaction → algedonic-signal classification.

Bridges (Discord, Slack, web_chat) call ``classify_reaction(emoji)`` on
each inbound reaction to map the emoji glyph (or Slack alias name) to a
polarity tag — ``"positive"``, ``"negative"``, or ``"neutral"``. The
classifier returns the polarity plus a normalized canonical name used
in event payloads so different bridges' representations of the same
reaction don't fragment the algedonic stream.

Polarity is conservative on both ends: only emojis whose meaning is
unambiguous in product/feedback context get tagged. Everything else
falls through to ``"neutral"`` — surfaced informationally but not
counted as pleasure/pain. Operators can extend the maps in
``saga.toml`` (future) or via env override.

Time-gating happens in ``feedback.py`` (default 24h window). This
module just resolves the emoji.
"""

from __future__ import annotations

from typing import Literal

Polarity = Literal["positive", "negative", "neutral"]


# Slack alias name → unicode glyph. Slack delivers reactions as alias
# names (``thumbsup``, ``heart``, ``x``); other bridges use the unicode
# glyph directly. Normalize both to a canonical form so the algedonic
# stream is bridge-agnostic.
_SLACK_ALIAS_TO_GLYPH = {
    "thumbsup": "👍",
    "+1": "👍",
    "heart": "❤️",
    "white_check_mark": "✅",
    "tada": "🎉",
    "rocket": "🚀",
    "100": "💯",
    "fire": "🔥",
    "star": "⭐",
    "thumbsdown": "👎",
    "-1": "👎",
    "x": "❌",
    "no_entry": "🚫",
    "warning": "⚠️",
    "rage": "😠",
    "broken_heart": "💔",
    "confused": "😕",
}


_POSITIVE_GLYPHS = frozenset({
    "👍", "❤️", "💖", "💗", "💓", "💕", "✅", "🎉", "🚀", "💯",
    "⭐", "🌟", "👏", "🙌", "🔥", "😄", "😊", "😍", "🥳", "✨",
    "💪", "👌", "🆗", "🎯",
})


_NEGATIVE_GLYPHS = frozenset({
    "👎", "❌", "🚫", "⚠️", "😠", "😡", "🤬", "💔", "😕", "😞",
    "😟", "🙅", "😖", "🛑", "🚨",
})


def normalize_emoji(raw: str) -> str:
    """Convert a Slack alias name (``thumbsup``) or a unicode glyph
    (``👍``) into the canonical glyph form. Unknown aliases pass through
    unchanged so we don't lose data."""
    if not raw:
        return ""
    s = raw.strip()
    # Slack aliases sometimes arrive wrapped in colons (`:thumbsup:`).
    s = s.strip(":")
    # Skin-tone modifiers (``thumbsup::skin-tone-3``) — drop the suffix
    # for classification purposes; the base emoji carries the polarity.
    if "::" in s:
        s = s.split("::", 1)[0]
    return _SLACK_ALIAS_TO_GLYPH.get(s, s)


def classify_reaction(emoji: str) -> Polarity:
    """Map an emoji to a polarity. Conservative — only widely-recognized
    feedback glyphs land in positive/negative; everything else is
    neutral.

    >>> classify_reaction("👍")
    'positive'
    >>> classify_reaction("thumbsup")
    'positive'
    >>> classify_reaction("👎")
    'negative'
    >>> classify_reaction("🍕")
    'neutral'
    """
    glyph = normalize_emoji(emoji)
    if glyph in _POSITIVE_GLYPHS:
        return "positive"
    if glyph in _NEGATIVE_GLYPHS:
        return "negative"
    return "neutral"
