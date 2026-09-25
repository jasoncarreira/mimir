"""Shared classification for errors raised by LLM providers."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ProviderErrorKind(Enum):
    """Behaviorally distinct classes of provider failure."""

    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    TRANSIENT = "transient"
    CLIENT = "client"
    UNKNOWN = "unknown"


# Ordered from terminal client errors to retryable errors so a policy or context
# failure containing incidental retry language still fails closed.
_PHRASE_TABLE: tuple[
    tuple[
        ProviderErrorKind,
        tuple[str, ...],
        frozenset[str] | None,
        tuple[str, ...] | None,
    ],
    ...,
] = (
    (
        ProviderErrorKind.CLIENT,
        (
            "unauthorized",
            "forbidden",
            "bad request",
            "context length",
            "max tokens",
            "too long",
            "maximum context",
            "content policy",
            "content policy violation",
            "safety policy",
            "prohibited",
        ),
        None,
        None,
    ),
    (
        ProviderErrorKind.RATE_LIMIT,
        ("rate limit", "rate_limit", "too many requests"),
        None,
        None,
    ),
    (
        ProviderErrorKind.TRANSIENT,
        (
            "connection reset",
            "connection refused",
            "connection timeout",
            "read timeout",
            "timed out",
            "temporary failure",
            "name or service not known",
            "5xx",
            "internal server error",
            "server error",
        ),
        None,
        None,
    ),
    (
        ProviderErrorKind.TRANSIENT,
        ("overloaded", "service unavailable"),
        frozenset({"anthropic"}),
        None,
    ),
    (
        ProviderErrorKind.TRANSIENT,
        ("service unavailable",),
        frozenset({"openai_compat"}),
        None,
    ),
    (
        ProviderErrorKind.TRANSIENT,
        ("you can retry", "overloaded", "temporarily unavailable"),
        frozenset({"codex_plus"}),
        None,
    ),
    (
        ProviderErrorKind.QUOTA,
        ("quota",),
        None,
        ("exhaust", "exceed", "limit"),
    ),
    (
        ProviderErrorKind.QUOTA,
        ("429",),
        None,
        None,
    ),
)

_TRANSIENT_EXCEPTION_NAMES = {
    "ReadError",
    "ConnectError",
    "RemoteProtocolError",
    "ReadTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "Timeout",
    "ConnectionError",
}
_CLIENT_EXCEPTION_NAMES = {
    "AuthenticationError",
    "AuthorizationError",
    "PermissionError",
    "BadRequestError",
    "BadRequest",
}


def _http_status_code(exc: BaseException) -> int | None:
    raw_status = getattr(exc, "status_code", None)
    if raw_status is None:
        response = getattr(exc, "response", None)
        raw_status = getattr(response, "status_code", None)
    try:
        return int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        return None


def _message_content_is_blank(message: Any) -> bool:
    content = getattr(message, "content", None)
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict):
                pieces.append(str(block.get("text") or block.get("content") or ""))
            else:
                pieces.append(str(getattr(block, "text", "") or getattr(block, "content", "")))
        return not "".join(pieces).strip()
    return not str(content).strip()


def _is_empty_structured_output_validation_error(exc: BaseException) -> bool:
    if exc.__class__.__name__ != "StructuredOutputValidationError":
        return False
    ai_message = getattr(exc, "ai_message", None)
    if ai_message is None or getattr(ai_message, "tool_calls", None):
        return False
    return _message_content_is_blank(ai_message)


def _effective_provider(provider: str | None, class_name: str) -> str | None:
    if provider is not None:
        return provider
    lowered = class_name.lower()
    if "anthropic" in lowered:
        return "anthropic"
    if "openai" in lowered:
        return "openai_compat"
    if "codex" in lowered:
        return "codex_plus"
    return None


def classify_provider_error(
    exc: BaseException,
    provider: str | None = None,
) -> ProviderErrorKind:
    """Classify an LLM provider error from structured evidence before text."""
    status = _http_status_code(exc)
    if status is not None:
        if status == 429:
            return ProviderErrorKind.RATE_LIMIT
        if 500 <= status < 600:
            return ProviderErrorKind.TRANSIENT
        if 400 <= status < 500:
            return ProviderErrorKind.CLIENT
        return ProviderErrorKind.UNKNOWN

    class_name = exc.__class__.__name__
    effective_provider = _effective_provider(provider, class_name)

    if _is_empty_structured_output_validation_error(exc):
        return ProviderErrorKind.TRANSIENT
    if "RateLimit" in class_name:
        return ProviderErrorKind.RATE_LIMIT
    if class_name == "OverloadedError":
        return ProviderErrorKind.TRANSIENT
    if class_name in _CLIENT_EXCEPTION_NAMES:
        return ProviderErrorKind.CLIENT
    if class_name in _TRANSIENT_EXCEPTION_NAMES:
        return ProviderErrorKind.TRANSIENT

    message = str(exc).lower()
    for kind, phrases, providers, required_phrases in _PHRASE_TABLE:
        if providers is not None and effective_provider not in providers:
            continue
        if providers == frozenset({"codex_plus"}):
            if class_name != "CodexResponseError" and "codexresponseerror" not in message:
                continue
        if required_phrases is not None and not any(
            phrase in message for phrase in required_phrases
        ):
            continue
        if any(phrase in message for phrase in phrases):
            return kind
    return ProviderErrorKind.UNKNOWN


__all__ = ["ProviderErrorKind", "classify_provider_error"]
