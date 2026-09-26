"""Runtime compatibility patches for ``langchain-codex-plus``.

Mimir normally prefers upstream fixes in provider packages. This module is for
small, defensive patches that protect production turns until the provider can
ship the behavior itself. The functions are intentionally import-light: nothing
here imports ``langchain_codex_plus`` or ``httpx`` at module import time, so
operators not using the Codex Plus extra don't pay for it.

Keep patches at the boundary ``langchain-codex-plus`` actually uses. For
example, the Codex Plus streaming path consumes httpx ``iter_lines`` /
``aiter_lines`` and never instantiates OpenAI SDK ``SSEDecoder``; do not patch
that SDK decoder here as a proxy for Codex Plus transport failures.

Retry behavior (chainlink #841, #1841):
- Uses provider-agnostic error classification from ``mimir._llm_retry``.
- Transient errors (429, 5xx, connection/timeout, overloaded) are retried
  with exponential backoff + jitter.
- Non-transient errors (400, auth, context-length, content-policy) fail fast.
- The provider streams SSE incrementally. Interactive turns retry only before
  the first chunk is yielded, avoiding duplicated partial output.
- Selected non-interactive turns buffer a complete model step before yielding,
  allowing a partial attempt to be discarded and safely retried.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from typing import Any

from mimir._llm_retry import (
    _calculate_delay,
    _is_retryable_error,
    _resolve_env_float,
    _resolve_env_int,
)
from mimir.event_logger import log_event_sync

log = logging.getLogger(__name__)

_STREAMING_RETRY_MARKER = "_mimir_codex_plus_transient_retry_patched"
_SYNC_RETRY_MARKER = "_mimir_codex_plus_sync_transient_retry_patched"
_PARTIAL_JSON_PATCH_MARKER = "_mimir_codex_plus_partial_json_fast_path_patched"
_CODEX_STREAM_CHUNK_FAST_PATH: ContextVar[bool] = ContextVar(
    "mimir_codex_plus_stream_chunk_fast_path", default=False
)
_CODEX_PLUS_BUFFER_STREAM: ContextVar[bool] = ContextVar(
    "mimir_codex_plus_buffer_stream", default=False
)
_BUFFERED_TURN_TRIGGERS = frozenset({
    "poller",
    "scheduled_tick",
    "saga_session_end",
    "upgrade",
    "shell_job_complete",
})
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_SECONDS = 0.5


def set_codex_plus_stream_buffering(trigger: str) -> Token[bool]:
    """Set whether Codex Plus model steps buffer for the current turn."""
    override = os.environ.get("MIMIR_CODEX_PLUS_BUFFER_NONINTERACTIVE", "").strip().lower()
    enabled = override not in {"0", "false"} and trigger in _BUFFERED_TURN_TRIGGERS
    return _CODEX_PLUS_BUFFER_STREAM.set(enabled)


def reset_codex_plus_stream_buffering(token: Token[bool]) -> None:
    """Restore the buffering mode that preceded the current turn."""
    _CODEX_PLUS_BUFFER_STREAM.reset(token)


def codex_plus_stream_buffering_enabled() -> bool:
    """Return the buffering mode inherited by the current model call."""
    return _CODEX_PLUS_BUFFER_STREAM.get()


def install_codex_plus_transient_retry_patch(ChatCodexPlus: type[Any] | None = None) -> None:
    """Patch ``ChatCodexPlus`` to retry safe transient stream drops.

    ``langchain-codex-plus`` streams SSE events incrementally. Interactive turns
    pass those chunks through and retry only if the stream fails before yielding.
    Non-interactive turns selected by the turn runner instead buffer each model
    step until completion, allowing partial attempts to be discarded and retried
    before LangGraph observes output or tool calls.
    """
    if ChatCodexPlus is None:
        from langchain_codex_plus import ChatCodexPlus as _ChatCodexPlus  # type: ignore[import-untyped]
        ChatCodexPlus = _ChatCodexPlus

    _ensure_partial_json_fast_path()
    _patch_astream(ChatCodexPlus)
    _patch_generate(ChatCodexPlus)


def _ensure_partial_json_fast_path() -> None:
    """Avoid per-delta JSON repair while Codex streams tool-call chunks.

    ``langchain-codex-plus`` yields one ``AIMessageChunk`` for every Codex
    ``response.function_call_arguments.delta`` event. LangChain eagerly runs
    ``parse_partial_json`` inside the ``AIMessageChunk`` validator for each
    incomplete args delta, which can burn CPU on the asyncio loop for every
    tiny stream chunk. Mimir only needs the raw ``tool_call_chunks`` during
    streaming; LangChain can parse the merged args after the provider yields the
    chunk back to the caller.

    Patch the module global that ``AIMessageChunk.init_tool_calls`` resolves,
    but only short-circuit while ``_patch_astream`` is awaiting the provider
    chunk construction. The context is reset before the chunk is yielded, so
    downstream aggregation/final parsing keeps normal LangChain behavior.
    """
    try:
        import langchain_core.messages.ai as ai_mod
    except ImportError:  # pragma: no cover - langchain is always present here
        return

    current = ai_mod.parse_partial_json
    if getattr(current, _PARTIAL_JSON_PATCH_MARKER, False):
        return

    def _mimir_parse_partial_json_fast_path(s: str, *args: Any, **kwargs: Any) -> Any:
        if _CODEX_STREAM_CHUNK_FAST_PATH.get():
            return None
        return current(s, *args, **kwargs)

    setattr(_mimir_parse_partial_json_fast_path, _PARTIAL_JSON_PATCH_MARKER, True)
    ai_mod.parse_partial_json = _mimir_parse_partial_json_fast_path


def _patch_astream(ChatCodexPlus: type[Any]) -> None:
    if getattr(ChatCodexPlus, _STREAMING_RETRY_MARKER, False):
        return
    original = getattr(ChatCodexPlus, "_astream", None)
    if original is None:
        log.debug("ChatCodexPlus object has no _astream method; skipping stream retry patch")
        return

    async def _patched_astream(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        attempts = _resolve_env_int("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
        base_delay = _resolve_env_float(
            "MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", _DEFAULT_BASE_DELAY_SECONDS,
        )
        buffered = _CODEX_PLUS_BUFFER_STREAM.get()
        for attempt in range(1, attempts + 1):
            yielded = False
            buffered_chunks: list[Any] = []
            stream = original(self, *args, **kwargs)
            iterator = stream.__aiter__()
            try:
                while True:
                    token = _CODEX_STREAM_CHUNK_FAST_PATH.set(True)
                    try:
                        chunk = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        _CODEX_STREAM_CHUNK_FAST_PATH.reset(token)
                    if buffered:
                        buffered_chunks.append(chunk)
                    else:
                        yielded = True
                        yield chunk
            except Exception as exc:
                if (yielded and not buffered) or attempt >= attempts:
                    raise
                is_retryable, reason = _is_retryable_error(exc, provider="codex_plus")
                if not is_retryable:
                    raise
                delay = _calculate_delay(attempt, base_delay, math.inf)
                if buffered_chunks:
                    log.warning(
                        "ChatCodexPlus._astream transient %s (reason=%s) after %s buffered "
                        "chunks; retrying attempt %s/%s after %.2fs",
                        type(exc).__name__, reason, len(buffered_chunks),
                        attempt + 1, attempts, delay,
                    )
                    log_event_sync(
                        "codex_plus_stream_retry",
                        attempt=attempt + 1,
                        exception_type=type(exc).__name__,
                        reason=reason,
                        discarded_chunks=len(buffered_chunks),
                    )
                else:
                    log.warning(
                        "ChatCodexPlus._astream transient %s (reason=%s) before first chunk; "
                        "retrying attempt %s/%s after %.2fs: %s",
                        type(exc).__name__, reason, attempt + 1, attempts, delay, exc,
                    )
                if delay > 0:
                    await asyncio.sleep(delay)
                continue

            for chunk in buffered_chunks:
                yield chunk
            return

    ChatCodexPlus._astream = _patched_astream
    setattr(ChatCodexPlus, _STREAMING_RETRY_MARKER, True)


def _patch_generate(ChatCodexPlus: type[Any]) -> None:
    if getattr(ChatCodexPlus, _SYNC_RETRY_MARKER, False):
        return
    original = getattr(ChatCodexPlus, "_generate", None)
    if original is None:
        log.debug("ChatCodexPlus object has no _generate method; skipping sync retry patch")
        return

    def _patched_generate(self: Any, *args: Any, **kwargs: Any) -> Any:
        attempts = _resolve_env_int("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
        base_delay = _resolve_env_float(
            "MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", _DEFAULT_BASE_DELAY_SECONDS,
        )
        for attempt in range(1, attempts + 1):
            try:
                return original(self, *args, **kwargs)
            except Exception as exc:
                if attempt >= attempts:
                    raise
                is_retryable, reason = _is_retryable_error(exc, provider="codex_plus")
                if not is_retryable:
                    raise
                delay = _calculate_delay(attempt, base_delay, math.inf)
                log.warning(
                    "ChatCodexPlus._generate transient %s (reason=%s); retrying attempt "
                    "%s/%s after %.2fs: %s",
                    type(exc).__name__, reason, attempt + 1, attempts, delay, exc,
                )
                if delay > 0:
                    time.sleep(delay)
        raise RuntimeError("unreachable codex-plus retry loop exit")

    ChatCodexPlus._generate = _patched_generate
    setattr(ChatCodexPlus, _SYNC_RETRY_MARKER, True)


__all__ = [
    "codex_plus_stream_buffering_enabled",
    "install_codex_plus_transient_retry_patch",
    "reset_codex_plus_stream_buffering",
    "set_codex_plus_stream_buffering",
]
