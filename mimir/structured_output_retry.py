"""Retry middleware for transient native structured-output parse failures.

LangChain's provider-native structured-output strategy parses the provider's
AIMessage after ``model.invoke``/``model.ainvoke`` returns. Transport-level retry
wrappers do not see failures raised at that downstream parse seam. Mimir retries
only the observed transient case: an empty assistant completion that cannot be
parsed as the requested schema.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from mimir._llm_retry import _calculate_delay, _is_retryable_error, _retry_config

log = logging.getLogger(__name__)


class StructuredOutputRetryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Retry model calls when native structured-output parsing sees empties."""

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        config = _retry_config()
        max_attempts = config["max_attempts"]
        base_delay = config["base_delay"]
        max_delay = config["max_delay"]

        for attempt in range(1, max_attempts + 1):
            try:
                return handler(request)
            except Exception as exc:
                is_retryable, reason = _is_retryable_error(exc)
                if not is_retryable or attempt >= max_attempts:
                    raise
                delay = _calculate_delay(attempt, base_delay, max_delay)
                log.warning(
                    "structured-output parse transient %s (reason=%s); retrying "
                    "attempt %s/%s after %.2fs: %s",
                    type(exc).__name__, reason, attempt + 1, max_attempts, delay, exc,
                )
                if delay > 0:
                    time.sleep(delay)
        raise RuntimeError("unreachable structured-output retry loop exit")

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> ModelResponse[Any]:
        config = _retry_config()
        max_attempts = config["max_attempts"]
        base_delay = config["base_delay"]
        max_delay = config["max_delay"]

        for attempt in range(1, max_attempts + 1):
            try:
                return await handler(request)
            except Exception as exc:
                is_retryable, reason = _is_retryable_error(exc)
                if not is_retryable or attempt >= max_attempts:
                    raise
                delay = _calculate_delay(attempt, base_delay, max_delay)
                log.warning(
                    "structured-output parse transient %s (reason=%s); retrying "
                    "attempt %s/%s after %.2fs: %s",
                    type(exc).__name__, reason, attempt + 1, max_attempts, delay, exc,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable structured-output retry loop exit")


__all__ = ["StructuredOutputRetryMiddleware"]
