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
    # Positive — every glyph in ``_POSITIVE_GLYPHS`` should have at least
    # one alias here so a Slack-delivered reaction (which arrives as the
    # alias name, not the glyph) classifies correctly. Multiple aliases
    # for the same glyph are fine — Slack accepts both ``+1`` and
    # ``thumbsup`` for 👍, both ``no_entry`` and ``no_entry_sign`` for 🚫.
    "thumbsup": "👍",
    "+1": "👍",
    "heart": "❤️",
    "sparkling_heart": "💖",
    "heartpulse": "💗",
    "heartbeat": "💓",
    "two_hearts": "💕",
    "white_check_mark": "✅",
    "tada": "🎉",
    "rocket": "🚀",
    "100": "💯",
    "fire": "🔥",
    "star": "⭐",
    "star2": "🌟",
    "clap": "👏",
    "raised_hands": "🙌",
    "smile": "😄",
    "blush": "😊",
    "heart_eyes": "😍",
    "partying_face": "🥳",
    "sparkles": "✨",
    "muscle": "💪",
    "ok_hand": "👌",
    "ok": "🆗",
    "dart": "🎯",
    # Negative.
    "thumbsdown": "👎",
    "-1": "👎",
    "x": "❌",
    "no_entry": "🚫",
    "no_entry_sign": "🚫",
    "warning": "⚠️",
    "angry": "😠",
    "rage": "😡",  # per Slack's emoji conventions :rage: is 😡, not 😠
    "face_with_symbols_on_mouth": "🤬",
    "broken_heart": "💔",
    "confused": "😕",
    "disappointed": "😞",
    "worried": "😟",
    "no_good": "🙅",
    "confounded": "😖",
    "octagonal_sign": "🛑",
    "rotating_light": "🚨",
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
