"""Shared classification for errors raised by LLM providers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class ProviderErrorClassification:
    """Typed policy inputs derived from one pass over a provider error.

    ``kind`` drives immediate retry behavior. ``quota_exhausted`` is an
    independent, deliberately more sensitive brake: a client-status wrapper can
    still carry a trustworthy rate-limit message and should activate pause
    without making that same request retryable.
    """

    kind: "ProviderErrorKind"
    quota_exhausted: bool


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


def _effective_providers(provider: str | None, class_name: str) -> frozenset[str]:
    """Return every provider indicated explicitly or by the exception class."""
    providers = {provider} if provider is not None else set()
    lowered = class_name.lower()
    if "anthropic" in lowered:
        providers.add("anthropic")
    if "openai" in lowered:
        providers.add("openai_compat")
    if "codex" in lowered:
        providers.add("codex_plus")
    return frozenset(providers)


def _message_signals_quota(message: str) -> bool:
    return (
        "429" in message
        or "rate limit" in message
        or "rate_limit" in message
        or (
            "quota" in message
            and any(phrase in message for phrase in ("exhaust", "exceed", "limit"))
        )
    )


def _message_signals_client(message: str) -> bool:
    client_phrases = _PHRASE_TABLE[0][1]
    return any(phrase in message for phrase in client_phrases)


def classify_provider_error(
    exc: BaseException,
    provider: str | None = None,
) -> ProviderErrorClassification:
    """Classify retry policy and quota-pause policy from one evidence pass."""
    class_name = exc.__class__.__name__
    message = str(exc).lower()
    status = _http_status_code(exc)
    response_status = getattr(getattr(exc, "response", None), "status_code", None)
    quota_exhausted = (
        "RateLimit" in class_name
        or status == 429
        # Preserve the legacy pause path even when a wrapper also exposes a
        # different top-level status code.
        or response_status == 429
        or _message_signals_quota(message)
    )

    if status is not None:
        if status == 429:
            kind = ProviderErrorKind.RATE_LIMIT
        elif 500 <= status < 600:
            kind = ProviderErrorKind.TRANSIENT
        elif 400 <= status < 500:
            kind = ProviderErrorKind.CLIENT
        else:
            kind = ProviderErrorKind.UNKNOWN
        return ProviderErrorClassification(kind, quota_exhausted)

    effective_providers = _effective_providers(provider, class_name)

    if _is_empty_structured_output_validation_error(exc):
        kind = ProviderErrorKind.TRANSIENT
    elif "RateLimit" in class_name:
        kind = ProviderErrorKind.RATE_LIMIT
    elif class_name in _CLIENT_EXCEPTION_NAMES:
        kind = ProviderErrorKind.CLIENT
    elif class_name in _TRANSIENT_EXCEPTION_NAMES:
        kind = (
            ProviderErrorKind.CLIENT
            if _message_signals_client(message)
            else ProviderErrorKind.TRANSIENT
        )
    else:
        kind = ProviderErrorKind.UNKNOWN
        for candidate, phrases, providers, required_phrases in _PHRASE_TABLE:
            if providers is not None and effective_providers.isdisjoint(providers):
                continue
            if providers == frozenset({"codex_plus"}):
                if class_name != "CodexResponseError" and "codexresponseerror" not in message:
                    continue
            if required_phrases is not None and not any(
                phrase in message for phrase in required_phrases
            ):
                continue
            if any(phrase in message for phrase in phrases):
                kind = candidate
                break
        if (
            kind is not ProviderErrorKind.CLIENT
            and class_name == "OverloadedError"
            and "anthropic" in effective_providers
        ):
            kind = ProviderErrorKind.TRANSIENT
    return ProviderErrorClassification(kind, quota_exhausted)


__all__ = [
    "ProviderErrorClassification",
    "ProviderErrorKind",
    "classify_provider_error",
]
