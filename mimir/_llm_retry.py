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

from ._provider_errors import (
    ProviderErrorKind,
    _is_empty_structured_output_validation_error,
    classify_provider_error,
)

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
    classification = classify_provider_error(exc, provider)
    retryable = classification.kind in {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.TRANSIENT,
    }
    return retryable, f"provider_error_{classification.kind.value}:{type(exc).__name__}"


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
    max_attempts = config["max_attempts"] if max_attempts is None else max_attempts
    base_delay = config["base_delay"] if base_delay is None else base_delay
    max_delay = config["max_delay"] if max_delay is None else max_delay

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
    max_attempts = config["max_attempts"] if max_attempts is None else max_attempts
    base_delay = config["base_delay"] if base_delay is None else base_delay
    max_delay = config["max_delay"] if max_delay is None else max_delay

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
    "_is_empty_structured_output_validation_error",
]
