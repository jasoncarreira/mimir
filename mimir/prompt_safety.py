"""Shared sanitization for untrusted values in framework-authored prompt text."""

from __future__ import annotations

import re
import unicodedata


FIELD_MAX_LEN = 240
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_prompt_field(value: object, max_len: int = FIELD_MAX_LEN) -> str:
    """Collapse whitespace, strip controls/formatting, and escape header brackets.

    Only for metadata, never message bodies. Literal escape sequences cannot
    open or close the framework's square-bracket headers.
    """
    sanitized = " ".join(str(value).split())
    sanitized = "".join(
        char for char in sanitized if unicodedata.category(char) not in {"Cc", "Cf"}
    )
    sanitized = sanitized.replace("[", r"\u005b").replace("]", r"\u005d")
    if len(sanitized) > max_len:
        sanitized = sanitized[: max_len - 1] + "…"
    return sanitized


def prefix_prompt_body(body: str) -> str:
    """Mark every untrusted body line, preserving its contents and line endings.

    Unlike a closing fence, this prefix cannot be escaped by body text. Use
    splitlines rather than splitting on LF so CR and Unicode separators also
    cannot introduce an unmarked framework-looking line.
    """
    return "".join("| " + line for line in body.splitlines(keepends=True)) or "| "
