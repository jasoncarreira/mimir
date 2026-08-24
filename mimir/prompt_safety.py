"""Shared sanitization for untrusted values in framework-authored prompt text."""

from __future__ import annotations

import re


FIELD_MAX_LEN = 240
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_prompt_field(value: object, max_len: int = FIELD_MAX_LEN) -> str:
    """Collapse whitespace, strip C0/DEL controls, and cap field length."""
    sanitized = " ".join(str(value).split())
    sanitized = CONTROL_CHARS_RE.sub("", sanitized)
    if len(sanitized) > max_len:
        sanitized = sanitized[: max_len - 1] + "…"
    return sanitized
