"""Tests for provider-agnostic LLM retry logic (chainlink #841)."""

from __future__ import annotations

from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage
import pytest

from mimir._llm_retry import (
    _is_empty_structured_output_validation_error,
    _is_retryable_error,
    _retry_async,
    _retry_sync,
    _retry_config,
)


class _TransientError(Exception):
    pass


class _NonRetryableError(Exception):
    pass


class _RateLimitError(Exception):
    status_code = 429


class _ServerError(Exception):
    status_code = 500


class _AuthError(Exception):
    status_code = 401


class _BadRequestError(Exception):
    status_code = 400


class _HTTPError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class TestIsRetryableError:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (_HTTPError(429, "Rate limited. Please retry."), (True, "http_status_retryable:_HTTPError")),
            (
                _HTTPError(
                    429,
                    "This request would exceed your organization's rate limit of 80000 "
                    "max tokens per minute. Please try again later.",
                ),
                (True, "http_status_retryable:_HTTPError"),
            ),
            (
                _HTTPError(429, "rate_limit_error: request queue too long, retry shortly"),
                (True, "http_status_retryable:_HTTPError"),
            ),
            (_HTTPError(529, "Overloaded"), (True, "http_status_retryable:_HTTPError")),
            (_HTTPError(500, "internal server error"), (True, "http_status_retryable:_HTTPError")),
            (
                _HTTPError(400, "bad request: invalid schema"),
                (False, "non_retryable_client_error:_HTTPError"),
            ),
            (
                _HTTPError(400, "prompt is too long: 250000 tokens > 200000 maximum context"),
                (False, "non_retryable_client_error:_HTTPError"),
            ),
            (
                _HTTPError(400, "rate limit exceeded"),
                (False, "non_retryable_client_error:_HTTPError"),
            ),
            (_HTTPError(401), (False, "non_retryable_client_error:_HTTPError")),
            (_HTTPError(403), (False, "non_retryable_client_error:_HTTPError")),
            (Exception("context length exceeded"), (False, "non_retryable_client_error:Exception")),
            (Exception("content policy violation"), (False, "non_retryable_client_error:Exception")),
            (Exception("rate limit exceeded"), (True, "generic_retryable:Exception")),
            (Exception("internal server error"), (True, "generic_retryable:Exception")),
            (Exception("provider rejected the request"), (False, "unknown_non_retryable:Exception")),
        ],
        ids=[
            "429-rate-limit",
            "429-max-tokens",
            "429-queue-too-long",
            "529-overloaded",
            "500-server-error",
            "400-invalid-schema",
            "400-context-overflow",
            "400-rate-limit-message",
            "401-status-only",
            "403-status-only",
            "message-context-overflow",
            "message-content-policy",
            "message-rate-limit",
            "message-server-error",
            "no-signal",
        ],
    )
    def test_classification_table(
        self,
        exc: BaseException,
        expected: tuple[bool, str],
    ) -> None:
        assert _is_retryable_error(exc) == expected

    def test_connection_errors_retryable(self) -> None:
        exc = Exception("connection reset")
        is_retryable, reason = _is_retryable_error(exc)
        assert is_retryable

    def test_rate_limit_429_retryable(self) -> None:
        exc = _RateLimitError("rate limited")
        is_retryable, reason = _is_retryable_error(exc)
        assert is_retryable

    def test_server_5xx_retryable(self) -> None:
        exc = _ServerError("internal error")
        is_retryable, reason = _is_retryable_error(exc)
        assert is_retryable

    def test_auth_401_not_retryable(self) -> None:
        exc = _AuthError("unauthorized")
        is_retryable, reason = _is_retryable_error(exc)
        assert not is_retryable

    def test_bad_request_400_not_retryable(self) -> None:
        exc = _BadRequestError("bad request")
        is_retryable, reason = _is_retryable_error(exc)
        assert not is_retryable

    def test_codex_retryable_error(self) -> None:
        exc = Exception("CodexResponseError: you can retry")
        is_retryable, reason = _is_retryable_error(exc, provider="codex_plus")
        assert is_retryable

    def test_codex_rate_limit(self) -> None:
        exc = Exception("CodexResponseError: rate limit exceeded")
        is_retryable, reason = _is_retryable_error(exc, provider="codex_plus")
        assert is_retryable

    def test_codex_non_retryable(self) -> None:
        exc = Exception("CodexResponseError: content policy violation")
        is_retryable, reason = _is_retryable_error(exc, provider="codex_plus")
        assert not is_retryable

    def test_anthropic_rate_limit(self) -> None:
        exc = Exception("AnthropicError: rate limit")
        is_retryable, reason = _is_retryable_error(exc, provider="anthropic")
        assert is_retryable

    def test_anthropic_overloaded(self) -> None:
        exc = Exception("AnthropicError: overloaded")
        is_retryable, reason = _is_retryable_error(exc, provider="anthropic")
        assert is_retryable

    def test_openai_rate_limit(self) -> None:
        exc = Exception("OpenAIError: rate limit")
        is_retryable, reason = _is_retryable_error(exc, provider="openai_compat")
        assert is_retryable

    def test_context_length_not_retryable(self) -> None:
        exc = Exception("context length exceeded")
        is_retryable, reason = _is_retryable_error(exc)
        assert not is_retryable

    def test_unknown_unclassified_error_fails_closed(self) -> None:
        exc = _NonRetryableError("provider rejected the request")
        is_retryable, reason = _is_retryable_error(exc)
        assert not is_retryable
        assert reason == "unknown_non_retryable:_NonRetryableError"

    def test_numeric_substrings_do_not_imply_status_codes(self) -> None:
        retryable, _ = _is_retryable_error(Exception("prompt has 14000 tokens"))
        assert not retryable

        retryable, _ = _is_retryable_error(Exception("blocked waiting for worker"))
        assert not retryable

    def test_empty_structured_output_validation_error_is_retryable(self) -> None:
        exc = StructuredOutputValidationError(
            "CriticFindings",
            ValueError("Native structured output expected valid JSON for CriticFindings"),
            AIMessage(content=""),
        )

        assert _is_empty_structured_output_validation_error(exc)
        is_retryable, reason = _is_retryable_error(exc)
        assert is_retryable
        assert reason == "empty_structured_output:StructuredOutputValidationError"

    def test_non_empty_structured_output_validation_error_is_not_retryable(self) -> None:
        exc = StructuredOutputValidationError(
            "CriticFindings",
            ValueError("missing verdict"),
            AIMessage(content='{"summary":"missing verdict"}'),
        )

        assert not _is_empty_structured_output_validation_error(exc)
        is_retryable, reason = _is_retryable_error(exc)
        assert not is_retryable
        assert reason == "unknown_non_retryable:StructuredOutputValidationError"


@pytest.mark.asyncio
async def test_retry_async_success_on_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    async def succeed():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = await _retry_async(succeed, provider="test")
    assert result == "ok"
    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_async_transient_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    async def transient_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _ServerError("internal server error")
        return "ok"

    result = await _retry_async(transient_then_succeed, provider="test")
    assert result == "ok"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_async_persistent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    async def always_fail():
        nonlocal call_count
        call_count += 1
        raise _ServerError("internal server error")

    with pytest.raises(_ServerError):
        await _retry_async(always_fail, provider="test")

    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_async_non_retryable_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    async def non_retryable():
        nonlocal call_count
        call_count += 1
        raise _NonRetryableError("401 unauthorized")

    with pytest.raises(_NonRetryableError):
        await _retry_async(non_retryable, provider="test")

    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_async_unknown_error_fails_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    async def unknown_error():
        nonlocal call_count
        call_count += 1
        raise _NonRetryableError("unclassified terminal failure")

    with pytest.raises(_NonRetryableError):
        await _retry_async(unknown_error, provider="test")

    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_async_honors_explicit_zero_options(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[tuple[float, float]] = []
    call_count = 0

    def calculate_delay(attempt: int, base_delay: float, max_delay: float) -> float:
        delays.append((base_delay, max_delay))
        return 0

    async def transient_then_succeed() -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _ServerError("internal server error")
        return "ok"

    monkeypatch.setattr("mimir._llm_retry._calculate_delay", calculate_delay)

    assert await _retry_async(transient_then_succeed, max_attempts=2, base_delay=0, max_delay=0) == "ok"
    assert delays == [(0, 0)]

    call_count = 0
    with pytest.raises(RuntimeError, match="unreachable retry loop exit"):
        await _retry_async(transient_then_succeed, max_attempts=0)
    assert call_count == 0


def test_retry_sync_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    def succeed():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = _retry_sync(succeed, provider="test")
    assert result == "ok"
    assert call_count == 1


def test_retry_sync_transient_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    def transient_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _ServerError("internal server error")
        return "ok"

    result = _retry_sync(transient_then_succeed, provider="test")
    assert result == "ok"
    assert call_count == 3


def test_retry_sync_honors_explicit_zero_options(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[tuple[float, float]] = []
    call_count = 0

    def calculate_delay(attempt: int, base_delay: float, max_delay: float) -> float:
        delays.append((base_delay, max_delay))
        return 0

    def transient_then_succeed() -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _ServerError("internal server error")
        return "ok"

    monkeypatch.setattr("mimir._llm_retry._calculate_delay", calculate_delay)

    assert _retry_sync(transient_then_succeed, max_attempts=2, base_delay=0, max_delay=0) == "ok"
    assert delays == [(0, 0)]

    call_count = 0
    with pytest.raises(RuntimeError, match="unreachable retry loop exit"):
        _retry_sync(transient_then_succeed, max_attempts=0)
    assert call_count == 0


def test_retry_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("MIMIR_LLM_RETRY_BASE_DELAY", "1.0")
    monkeypatch.setenv("MIMIR_LLM_RETRY_MAX_DELAY", "60.0")

    config = _retry_config()
    assert config["max_attempts"] == 5
    assert config["base_delay"] == 1.0
    assert config["max_delay"] == 60.0
