"""Provider-agnostic retry/backoff for LLM calls.

Provides centralized error classification and retry logic for transient
provider errors across codex_plus, anthropic, openai_compat, and claude_code.

Retryable errors:
- Provider 429 (rate limit)
- Provider 5xx (server errors)
- Provider "overloaded" (Anthropic-specific)
- Connection/timeout errors

Non-retryable (fail fast):
- 400 Bad Request
- 401/403 Auth errors
- Context length / max tokens exceeded
- Content policy violations
- Other client errors
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY = 0.5
_DEFAULT_MAX_DELAY = 30.0


def _retry_config() -> dict[str, Any]:
    return {
        "max_attempts": _resolve_env_int(
            "MIMIR_LLM_RETRY_MAX_ATTEMPTS",
            _DEFAULT_MAX_ATTEMPTS,
        ),
        "base_delay": _resolve_env_float(
            "MIMIR_LLM_RETRY_BASE_DELAY",
            _DEFAULT_BASE_DELAY,
        ),
        "max_delay": _resolve_env_float(
            "MIMIR_LLM_RETRY_MAX_DELAY",
            _DEFAULT_MAX_DELAY,
        ),
    }


def _resolve_env_int(env: str, default: int) -> int:
    raw = os.environ.get(env, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("invalid %s=%r; using %d", env, raw, default)
        return default


def _resolve_env_float(env: str, default: float) -> float:
    raw = os.environ.get(env, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("invalid %s=%r; using %f", env, raw, default)
        return default


def _is_retryable_error(exc: BaseException, provider: str | None = None) -> tuple[bool, str]:
    """Classify an exception as retryable or not.

    Returns (is_retryable, reason) tuple.
    """
    exc_type = type(exc)
    exc_name = exc_type.__name__
    exc_msg = str(exc).lower()

    if _is_non_retryable_client_error(exc):
        return False, f"non_retryable_client_error:{exc_name}"

    if _is_transient_connection_error(exc):
        return True, f"connection_error:{exc_name}"

    if provider == "anthropic" or "anthropic" in exc_msg:
        if _is_anthropic_retryable(exc):
            return True, f"anthropic_retryable:{exc_name}"

    if provider == "openai_compat" or "openai" in exc_msg:
        if _is_openai_retryable(exc):
            return True, f"openai_retryable:{exc_name}"

    if provider == "codex_plus" or "codex" in exc_msg:
        if _is_codex_retryable(exc):
            return True, f"codex_retryable:{exc_name}"

    if _is_generic_retryable(exc):
        return True, f"generic_retryable:{exc_name}"

    return True, f"unknown_default_retry:{exc_name}"


def _is_transient_connection_error(exc: BaseException) -> bool:
    """Check for transient connection/timeout errors."""
    try:
        import httpx
    except ImportError:
        pass
    else:
        if isinstance(
            exc,
            (
                httpx.ReadError,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                httpx.ConnectTimeout,
            ),
        ):
            return True

    exc_name = exc.__class__.__name__
    if exc_name in {
        "ReadError",
        "ConnectError",
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "Timeout",
        "ConnectionError",
    }:
        return True

    msg = str(exc).lower()
    transient_keywords = [
        "connection reset",
        "connection refused",
        "connection timeout",
        "read timeout",
        "timed out",
        "temporary failure",
        "name or service not known",
    ]
    return any(kw in msg for kw in transient_keywords)


def _is_anthropic_retryable(exc: BaseException) -> bool:
    """Check for Anthropic-specific retryable errors."""
    exc_name = exc.__class__.__name__
    msg = str(exc).lower()

    if "rate" in exc_name.lower() or "429" in msg:
        return True

    if "overloaded" in msg or exc_name == "OverloadedError":
        return True

    if exc_name in {"APIStatusError", "APIError"}:
        status = getattr(exc, "status_code", None)
        if status is not None:
            if status == 429:
                return True
            if 500 <= status < 600:
                return True

    if "529" in msg or "service unavailable" in msg:
        return True

    return False


def _is_openai_retryable(exc: BaseException) -> bool:
    """Check for OpenAI-compatible API retryable errors."""
    exc_name = exc.__class__.__name__
    msg = str(exc).lower()

    if "rate" in exc_name.lower() or "429" in msg:
        return True

    if exc_name in {"APIStatusError", "APIError", "RateLimitError"}:
        status = getattr(exc, "status_code", None)
        if status is not None:
            if status == 429:
                return True
            if 500 <= status < 600:
                return True

    if "529" in msg or "service unavailable" in msg:
        return True

    return False


def _is_codex_retryable(exc: BaseException) -> bool:
    """Check for Codex-specific retryable errors."""
    exc_name = exc.__class__.__name__
    msg = str(exc).lower()

    if exc_name == "CodexResponseError" or "codexresponseerror" in msg:
        if "retry" in msg or "you can retry" in msg:
            return True

        if "rate" in msg or "429" in msg:
            return True

        status = getattr(exc, "status_code", None)
        if status is not None:
            if status == 429:
                return True
            if 500 <= status < 600:
                return True

        if "overloaded" in msg or "temporarily unavailable" in msg:
            return True

    return False


def _is_generic_retryable(exc: BaseException) -> bool:
    """Check for generic retryable errors by status code."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        if status == 429:
            return True
        if 500 <= status < 600:
            return True
        if status >= 600:
            return True

    msg = str(exc).lower()
    if any(kw in msg for kw in ["429", "rate limit", "too many requests"]):
        return True
    if any(kw in msg for kw in ["5xx", "500", "502", "503", "504", "internal error", "server error"]):
        return True

    return False


def _is_non_retryable_client_error(exc: BaseException) -> bool:
    """Check for non-retryable client errors (fail fast)."""
    exc_name = exc.__class__.__name__
    msg = str(exc).lower()

    if exc_name in {"AuthenticationError", "AuthorizationError", "PermissionError"}:
        return True

    if "401" in msg or "unauthorized" in msg:
        return True

    if "403" in msg or "forbidden" in msg:
        return True

    if exc_name in {"BadRequestError", "BadRequest"}:
        return True

    if "400" in msg and "bad request" in msg:
        return True

    status = getattr(exc, "status_code", None)
    if status is not None:
        if status == 400:
            return True
        if 400 < status < 500 and status not in (429,):
            return True

    if any(kw in msg for kw in ["context length", "max tokens", "too long", "maximum context"]):
        return True

    if any(kw in msg for kw in ["content policy", "content policy violation", "blocked", "prohibited"]):
        return True

    return False


def _calculate_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Calculate exponential backoff delay with jitter."""
    delay = base_delay * (2 ** (attempt - 1))
    jitter = random.uniform(0, 0.5 * delay)
    return min(delay + jitter, max_delay)


async def _retry_async(
    func: Callable[..., Any],
    *args: Any,
    provider: str | None = None,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    **kwargs: Any,
) -> Any:
    """Async retry wrapper with exponential backoff and jitter."""
    config = _retry_config()
    max_attempts = max_attempts or config["max_attempts"]
    base_delay = base_delay or config["base_delay"]
    max_delay = max_delay or config["max_delay"]

    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = func(*args, **kwargs)

            if asyncio.iscoroutine(result):
                result = await result

            return result

        except Exception as exc:
            last_exc = exc

            is_retryable, reason = _is_retryable_error(exc, provider)

            if not is_retryable or attempt >= max_attempts:
                log.warning(
                    "LLM call failed (non-retryable or max attempts reached): "
                    "provider=%s, attempt=%s/%s, reason=%s, error=%s",
                    provider, attempt, max_attempts, reason, exc,
                )
                raise

            delay = _calculate_delay(attempt, base_delay, max_delay)

            log.warning(
                "LLM call transient error, retrying: "
                "provider=%s, attempt=%s/%s, reason=%s, delay=%.2fs, error=%s",
                provider, attempt, max_attempts, reason, delay, exc,
            )

            await asyncio.sleep(delay)

    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable retry loop exit")


def _retry_sync(
    func: Callable[..., Any],
    *args: Any,
    provider: str | None = None,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    **kwargs: Any,
) -> Any:
    """Sync retry wrapper with exponential backoff and jitter."""
    config = _retry_config()
    max_attempts = max_attempts or config["max_attempts"]
    base_delay = base_delay or config["base_delay"]
    max_delay = max_delay or config["max_delay"]

    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)

        except Exception as exc:
            last_exc = exc

            is_retryable, reason = _is_retryable_error(exc, provider)

            if not is_retryable or attempt >= max_attempts:
                log.warning(
                    "LLM call failed (non-retryable or max attempts reached): "
                    "provider=%s, attempt=%s/%s, reason=%s, error=%s",
                    provider, attempt, max_attempts, reason, exc,
                )
                raise

            delay = _calculate_delay(attempt, base_delay, max_delay)

            log.warning(
                "LLM call transient error, retrying: "
                "provider=%s, attempt=%s/%s, reason=%s, delay=%.2fs, error=%s",
                provider, attempt, max_attempts, reason, delay, exc,
            )

            time.sleep(delay)

    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable retry loop exit")


__all__ = [
    "_is_retryable_error",
    "_retry_async",
    "_retry_sync",
    "_retry_config",
]
