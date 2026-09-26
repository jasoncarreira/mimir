"""Cross-path decision table for provider error classification."""

from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage
import pytest

from mimir._llm_retry import _is_retryable_error
from mimir._provider_errors import ProviderErrorKind, classify_provider_error
from mimir.quota_pause import is_quota_exhaustion


class _HTTPError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _RateLimitError(Exception):
    status_code = 429


class _ServerError(Exception):
    status_code = 500


class _AuthError(Exception):
    status_code = 401


class _BadRequestError(Exception):
    status_code = 400


class RateLimitError(Exception):
    pass


class AnthropicError(Exception):
    pass


class CodexResponseError(Exception):
    pass


def _response_error(status: int, message: str) -> Exception:
    exc = Exception(message)
    exc.response = SimpleNamespace(status_code=status)  # type: ignore[attr-defined]
    return exc


def _top_level_and_response_error(
    status: int,
    response_status: int,
    message: str,
) -> Exception:
    exc = Exception(message)
    exc.status_code = status  # type: ignore[attr-defined]
    exc.response = SimpleNamespace(status_code=response_status)  # type: ignore[attr-defined]
    return exc


_EMPTY_STRUCTURED_OUTPUT = StructuredOutputValidationError(
    "CriticFindings",
    ValueError("Native structured output expected valid JSON for CriticFindings"),
    AIMessage(content=""),
)
_NONEMPTY_STRUCTURED_OUTPUT = StructuredOutputValidationError(
    "CriticFindings",
    ValueError("missing verdict"),
    AIMessage(content='{"summary":"missing verdict"}'),
)


def _legacy_status(exc: BaseException) -> int | None:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        raw = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _legacy_retry(exc: BaseException, provider: str | None) -> bool:
    """Frozen boolean behavior of main before the shared classifier."""
    name = type(exc).__name__
    lowered_name = name.lower()
    message = str(exc).lower()
    status = _legacy_status(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600

    if name in {"AuthenticationError", "AuthorizationError", "PermissionError"}:
        return False
    if "unauthorized" in message or "forbidden" in message:
        return False
    if name in {"BadRequestError", "BadRequest"} or "bad request" in message:
        return False
    if any(
        phrase in message
        for phrase in (
            "context length",
            "max tokens",
            "too long",
            "maximum context",
            "content policy",
            "content policy violation",
            "safety policy",
            "prohibited",
        )
    ):
        return False

    if name in {
        "ReadError",
        "ConnectError",
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "Timeout",
        "ConnectionError",
    } or any(
        phrase in message
        for phrase in (
            "connection reset",
            "connection refused",
            "connection timeout",
            "read timeout",
            "timed out",
            "temporary failure",
            "name or service not known",
        )
    ):
        return True

    if provider == "anthropic" or "anthropic" in lowered_name:
        if name in {"RateLimitError", "OverloadedError"}:
            return True
        if "overloaded" in message or "service unavailable" in message:
            return True
    if provider == "openai_compat" or "openai" in lowered_name:
        if name == "RateLimitError" or "service unavailable" in message:
            return True
    if provider == "codex_plus" or "codex" in lowered_name:
        if name == "CodexResponseError" or "codexresponseerror" in message:
            if any(
                phrase in message
                for phrase in (
                    "you can retry",
                    "rate limit",
                    "too many requests",
                    "overloaded",
                    "temporarily unavailable",
                )
            ):
                return True

    return any(
        phrase in message
        for phrase in (
            "rate limit",
            "too many requests",
            "5xx",
            "internal server error",
            "server error",
        )
    )


def _legacy_pause(exc: BaseException) -> bool:
    """Frozen boolean behavior of main before the shared classifier."""
    if "RateLimit" in type(exc).__name__:
        return True
    if getattr(getattr(exc, "response", None), "status_code", None) == 429:
        return True
    message = str(exc).lower()
    return (
        "429" in message
        or "rate limit" in message
        or "rate_limit" in message
        or (
            "quota" in message
            and any(phrase in message for phrase in ("exhaust", "exceed", "limit"))
        )
    )


def _synthetic_error(class_name: str, message: str, status: int | None, shape: str) -> BaseException:
    exc = type(class_name, (Exception,), {})(message)
    if status is not None:
        if shape == "attribute":
            exc.status_code = status  # type: ignore[attr-defined]
        else:
            exc.response = SimpleNamespace(status_code=status)  # type: ignore[attr-defined]
    return exc


def test_shared_classifier_preserves_legacy_decisions_over_cross_product() -> None:
    """Differential guard: behavior may move only through reviewed allow-lists."""
    class_names = (
        "Exception",
        "AnthropicError",
        "OpenAIError",
        "CodexResponseError",
        "RateLimitError",
        "OverloadedError",
        "AuthenticationError",
        "BadRequestError",
        "ConnectionError",
    )
    messages = (
        "plain failure",
        "overloaded",
        "service unavailable",
        "you can retry your request",
        "rate limit exceeded",
        "too many requests",
        "HTTP 429",
        "quota exceeded",
        "bad request: rate limit",
        "unauthorized",
        "context length exceeded",
        "content policy violation",
        "connection reset",
        "internal server error",
    )
    statuses = (None, 400, 403, 429, 500, 503)
    providers = (None, "anthropic", "openai_compat", "codex_plus")

    for class_name in class_names:
        for message in messages:
            for status in statuses:
                for shape in ("attribute", "response"):
                    for provider in providers:
                        exc = _synthetic_error(class_name, message, status, shape)
                        old_retry = _legacy_retry(exc, provider)
                        new_retry = _is_retryable_error(exc, provider)[0]
                        old_pause = _legacy_pause(exc)
                        new_pause = is_quota_exhaustion(exc)

                        # Deliberate retry gain: provider-independent recognition
                        # of RateLimit-named exceptions without structured status.
                        retry_gain_allowed = (
                            not old_retry
                            and new_retry
                            and status is None
                            and "RateLimit" in class_name
                        )
                        assert new_retry == old_retry or retry_gain_allowed, (
                            class_name,
                            message,
                            status,
                            shape,
                            provider,
                            old_retry,
                            new_retry,
                        )

                        # Deliberate pause gain: a top-level 429 is now recognized
                        # in addition to the legacy response.status_code shape.
                        pause_gain_allowed = (
                            not old_pause
                            and new_pause
                            and status == 429
                            and shape == "attribute"
                        )
                        assert new_pause == old_pause or pause_gain_allowed, (
                            class_name,
                            message,
                            status,
                            shape,
                            provider,
                            old_pause,
                            new_pause,
                        )


def test_nested_429_preserves_pause_when_top_level_status_differs() -> None:
    """A wrapper status must not hide the nested legacy quota signal."""
    exc = _top_level_and_response_error(500, 429, "not obvious from message")

    classification = classify_provider_error(exc)

    assert classification.kind is ProviderErrorKind.TRANSIENT
    assert classification.quota_exhausted is True
    assert is_quota_exhaustion(exc) is True


@pytest.mark.parametrize(
    ("exc", "provider", "kind", "retry", "pause"),
    [
        (_HTTPError(429, "Rate limited. Please retry."), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (
            _HTTPError(429, "This request would exceed your organization's rate limit"),
            None,
            ProviderErrorKind.RATE_LIMIT,
            True,
            True,
        ),
        (_HTTPError(429, "rate_limit_error: queue too long"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (_HTTPError(529, "Overloaded"), None, ProviderErrorKind.TRANSIENT, True, False),
        (_HTTPError(500, "internal server error"), None, ProviderErrorKind.TRANSIENT, True, False),
        (_HTTPError(400, "bad request: invalid schema"), None, ProviderErrorKind.CLIENT, False, False),
        (_HTTPError(400, "prompt is too long"), None, ProviderErrorKind.CLIENT, False, False),
        # Retry still fails fast on a 400, but quota pause preserves main's
        # message-sensitive brake instead of trusting the wrapper status alone.
        (_HTTPError(400, "rate limit exceeded"), None, ProviderErrorKind.CLIENT, False, True),
        (_HTTPError(401), None, ProviderErrorKind.CLIENT, False, False),
        (_HTTPError(403), None, ProviderErrorKind.CLIENT, False, False),
        (Exception("context length exceeded"), None, ProviderErrorKind.CLIENT, False, False),
        (Exception("content policy violation"), None, ProviderErrorKind.CLIENT, False, False),
        (Exception("rate limit exceeded"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (Exception("internal server error"), None, ProviderErrorKind.TRANSIENT, True, False),
        (Exception("provider rejected the request"), None, ProviderErrorKind.UNKNOWN, False, False),
        (Exception("connection reset"), None, ProviderErrorKind.TRANSIENT, True, False),
        (_RateLimitError("rate limited"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (_ServerError("internal error"), None, ProviderErrorKind.TRANSIENT, True, False),
        (_AuthError("unauthorized"), None, ProviderErrorKind.CLIENT, False, False),
        (_BadRequestError("bad request"), None, ProviderErrorKind.CLIENT, False, False),
        (
            Exception("CodexResponseError: you can retry"),
            "codex_plus",
            ProviderErrorKind.TRANSIENT,
            True,
            False,
        ),
        (
            Exception("CodexResponseError: rate limit exceeded"),
            "codex_plus",
            ProviderErrorKind.RATE_LIMIT,
            True,
            True,
        ),
        (
            Exception("CodexResponseError: content policy violation"),
            "codex_plus",
            ProviderErrorKind.CLIENT,
            False,
            False,
        ),
        (Exception("AnthropicError: rate limit"), "anthropic", ProviderErrorKind.RATE_LIMIT, True, True),
        (Exception("AnthropicError: overloaded"), "anthropic", ProviderErrorKind.TRANSIENT, True, False),
        (Exception("OpenAIError: rate limit"), "openai_compat", ProviderErrorKind.RATE_LIMIT, True, True),
        (Exception("prompt has 14000 tokens"), None, ProviderErrorKind.UNKNOWN, False, False),
        (Exception("blocked waiting for worker"), None, ProviderErrorKind.UNKNOWN, False, False),
        (_EMPTY_STRUCTURED_OUTPUT, None, ProviderErrorKind.TRANSIENT, True, False),
        (_NONEMPTY_STRUCTURED_OUTPUT, None, ProviderErrorKind.UNKNOWN, False, False),
        (RateLimitError("x"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (_response_error(429, "not obvious from message"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (Exception("HTTP 429 Too Many Requests"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (Exception("hit rate limit"), None, ProviderErrorKind.RATE_LIMIT, True, True),
        (Exception("quota exceeded"), None, ProviderErrorKind.QUOTA, False, True),
        (ValueError("bad input"), None, ProviderErrorKind.UNKNOWN, False, False),
        (TimeoutError("network slow"), None, ProviderErrorKind.UNKNOWN, False, False),
        (Exception("file not found"), None, ProviderErrorKind.UNKNOWN, False, False),
        (KeyError("missing"), None, ProviderErrorKind.UNKNOWN, False, False),
        (Exception("some errors=429 happened"), None, ProviderErrorKind.QUOTA, False, True),
    ],
)
def test_provider_error_decision_table(
    exc: BaseException,
    provider: str | None,
    kind: ProviderErrorKind,
    retry: bool,
    pause: bool,
) -> None:
    assert classify_provider_error(exc, provider).kind is kind
    assert _is_retryable_error(exc, provider)[0] is retry
    assert is_quota_exhaustion(exc) is pause
    assert not (kind is ProviderErrorKind.TRANSIENT and retry and pause)
