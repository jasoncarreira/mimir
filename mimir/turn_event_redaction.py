"""Shared redaction helpers for live turn-event detail."""

from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    # Authorization: Bearer value and bare Bearer value shapes. Keep this before
    # the generic authorization key/value pattern so it consumes the token, not
    # just the literal "Bearer" prefix.
    re.compile(r"(?i)\b(authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]+"),
    # key=value, key: value, JSON, and Python dict-repr secret values.
    re.compile(
        r"(?i)(['\"]?[A-Za-z0-9_.:-]*(?:token|api[_-]?key|secret|password|authorization)['\"]?\s*[:=]\s*)"
        r"(?:['\"][^'\"]*['\"]|[^,\s}]+)"
    ),
    # Common provider / GitHub / AWS token prefixes.
    re.compile(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16})\b"),
    # Conservative high-entropy fallback for long unbroken credential-like blobs.
    re.compile(r"\b(?=[A-Za-z0-9_+/=-]{40,}\b)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9_+/=-]+\b"),
)
_PATH_PATTERN = re.compile(
    r"(?<![\w:])(?:/[^\s,'\"}]+|~/[^\s,'\"}]+|[A-Za-z]:\\[^\s,'\"}]+|"
    r"(?:attachments|scratch|state|memory|mimir|tests|frontend|docs|uploads|tmp|workspace)[/\\][^\s,'\"}]+)"
)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:token|api[_-]?key|secret|password|authorization)"
)


def scrub_detail(value: Any, *, limit: int = 320) -> str | None:
    text = _clean(value, limit=limit * 2)
    if not text:
        return None
    return _finalize_detail(scrub_text(text), limit=limit)


def scrub_turn_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy while preserving private IFC metadata in-process."""
    private = {
        key: event[key]
        for key in ("_ifc_labels", "_auth_context")
        if key in event
    }
    public = {key: value for key, value in event.items() if key not in private}
    cleaned = scrub_value(public)
    if not isinstance(cleaned, dict):
        return private
    cleaned.update(private)
    return cleaned


def scrub_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY_PATTERN.search(str(key)):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: scrub_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(item) for item in value)
    if isinstance(value, str):
        return scrub_text(value)
    return value


def scrub_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return _PATH_PATTERN.sub("[path]", redacted)


def _finalize_detail(text: str, *, limit: int) -> str | None:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _clean(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]
