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


def _response_error(status: int, message: str) -> Exception:
    exc = Exception(message)
    exc.response = SimpleNamespace(status_code=status)  # type: ignore[attr-defined]
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
        (_HTTPError(400, "rate limit exceeded"), None, ProviderErrorKind.CLIENT, False, False),
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
    assert classify_provider_error(exc, provider) is kind
    assert _is_retryable_error(exc, provider)[0] is retry
    assert is_quota_exhaustion(exc) is pause
    assert not (kind is ProviderErrorKind.TRANSIENT and retry and pause)
