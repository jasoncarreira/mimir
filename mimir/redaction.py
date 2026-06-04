"""Shared best-effort redaction helpers for durable logs.

The patterns here intentionally cover broad token-shaped credentials before
strings land in durable state such as ``turns.jsonl`` or ``events.jsonl``.
False positives are acceptable: the redactor masks values, it never refuses to
log the surrounding context.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Token-shape redaction for subprocess / event output (pre-OSS hardening,
# review item #8, extended by chainlink #370). Anything a subprocess or event
# payload emits can land in durable JSONL logs, so broad masking is preferable
# to call-site-specific best effort.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:github_pat_|ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]+"),
    # Anthropic API keys. Prefix ``sk-ant-`` is stable across the API and
    # Claude Code provisioning paths. Allow the underscore / hyphen alphabet
    # observed in issued keys.
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    # Slack bot, user, app, refresh, and config tokens. Prefix is the
    # disambiguator; payload alphabet is base62-ish + dashes.
    re.compile(r"xox[bpasr]-[A-Za-z0-9-]+"),
    # OpenAI-style secret keys (``sk-…`` and ``sk-proj-…``). The ``sk-ant-``
    # case is already covered above; this matches the OpenAI shapes without
    # colliding.
    re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    # ``Authorization: Bearer <token>`` headers in dumped HTTP traces.
    # Case-insensitive; captures the value through whitespace / quote boundary.
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    # ``token=…``, ``api_key=…``, ``password=…`` value-style fields (URL query,
    # env var dumps, JSON pretty-prints with bareword keys). The value alphabet
    # stops at common delimiters so the regex doesn't eat the rest of the line.
    re.compile(r"(?i)(token=|api[_-]?key=|password=|passwd=|secret=)([^\s\"',&]+)"),
    # Discord bot tokens — JWT-shaped with ``MTk…`` / ``MzU…`` prefix for many
    # of them. Use the well-documented 24+.6+27 segment shape.
    re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
)


def redact_text(text: str) -> str:
    """Strip token-shaped secrets out of text before it lands in logs.

    Replacement is ``[REDACTED]`` so logs still indicate "something matched a
    token shape here" without exposing the value. For the ``bearer …`` and
    ``token=…`` patterns, the prefix is preserved so surrounding context stays
    readable.
    """
    if not text:
        return text
    out = text
    for pat in _TOKEN_PATTERNS:
        # Patterns with capture groups preserve the prefix; the others mask the
        # whole match. We detect by group count.
        if pat.groups == 2:
            out = pat.sub(r"\1[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


def redact_payload(value: Any) -> Any:
    """Recursively redact strings in a JSON-ish payload.

    The event sink accepts arbitrary payload values and serializes with
    ``json.dumps(..., default=str)``. Preserve container shape for normal JSON
    values while redacting token-shaped substrings before serialization. Exotic
    objects are stringified early so ``json.dumps(default=str)`` cannot bypass
    redaction for an object whose ``__str__`` contains a token-shaped value.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))
