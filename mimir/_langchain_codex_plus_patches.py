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
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

log = logging.getLogger(__name__)

_STREAMING_RETRY_MARKER = "_mimir_codex_plus_transient_retry_patched"
_SYNC_RETRY_MARKER = "_mimir_codex_plus_sync_transient_retry_patched"
_PARTIAL_JSON_PATCH_MARKER = "_mimir_codex_plus_partial_json_fast_path_patched"
_CODEX_STREAM_CHUNK_FAST_PATH: ContextVar[bool] = ContextVar(
    "mimir_codex_plus_stream_chunk_fast_path", default=False
)
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_SECONDS = 0.5


def _retry_attempts() -> int:
    raw = os.environ.get("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS", "").strip()
    if not raw:
        return _DEFAULT_MAX_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning(
            "invalid MIMIR_CODEX_PLUS_TRANSIENT_RETRY_ATTEMPTS=%r; using %s",
            raw, _DEFAULT_MAX_ATTEMPTS,
        )
        return _DEFAULT_MAX_ATTEMPTS


def _retry_base_delay() -> float:
    raw = os.environ.get("MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY", "").strip()
    if not raw:
        return _DEFAULT_BASE_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning(
            "invalid MIMIR_CODEX_PLUS_TRANSIENT_RETRY_BASE_DELAY=%r; using %.2f",
            raw, _DEFAULT_BASE_DELAY_SECONDS,
        )
        return _DEFAULT_BASE_DELAY_SECONDS


def _is_transient_connection_error(exc: BaseException) -> bool:
    """Return True for network failures worth re-issuing once or twice.

    Keep this deliberately narrower than the quota-pause classifier: 429s and
    provider ``CodexResponseError`` failures are semantic refusals, not transport
    drops. The motivating production failures were httpx ``ReadError`` stream
    drops against ``chatgpt.com/backend-api/codex/responses``.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - codex-plus pulls httpx in practice
        return type(exc).__name__ in {
            "ReadError",
            "ConnectError",
            "RemoteProtocolError",
            "ReadTimeout",
            "ConnectTimeout",
        }

    return isinstance(
        exc,
        (
            httpx.ReadError,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
        ),
    )


def install_codex_plus_transient_retry_patch(ChatCodexPlus: type[Any] | None = None) -> None:
    """Patch ``ChatCodexPlus`` to retry pre-yield transient stream drops.

    ``langchain-codex-plus``'s async streaming implementation reads the complete
    SSE response into memory before yielding any LangChain chunks. If the HTTP
    stream drops during that read, LangGraph sees an exception and the whole
    mimir turn fails. Retrying *inside* the model call is the safe boundary:
    LangGraph has not observed a tool call or assistant message yet, so a retry
    cannot duplicate tool side effects.

    The wrapper still guards for future provider versions that might yield
    incrementally: once any chunk has been yielded, retrying could duplicate a
    partial assistant/tool-call result, so the exception is re-raised.
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
        attempts = _retry_attempts()
        base_delay = _retry_base_delay()
        for attempt in range(1, attempts + 1):
            yielded = False
            stream = original(self, *args, **kwargs)
            iterator = stream.__aiter__()
            try:
                while True:
                    token = _CODEX_STREAM_CHUNK_FAST_PATH.set(True)
                    try:
                        chunk = await iterator.__anext__()
                    except StopAsyncIteration:
                        return
                    finally:
                        _CODEX_STREAM_CHUNK_FAST_PATH.reset(token)
                    yielded = True
                    yield chunk
            except Exception as exc:
                if yielded or attempt >= attempts or not _is_transient_connection_error(exc):
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                log.warning(
                    "ChatCodexPlus._astream transient %s before first chunk; "
                    "retrying attempt %s/%s after %.2fs: %s",
                    type(exc).__name__, attempt + 1, attempts, delay, exc,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

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
        attempts = _retry_attempts()
        base_delay = _retry_base_delay()
        for attempt in range(1, attempts + 1):
            try:
                return original(self, *args, **kwargs)
            except Exception as exc:
                if attempt >= attempts or not _is_transient_connection_error(exc):
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                log.warning(
                    "ChatCodexPlus._generate transient %s; retrying attempt "
                    "%s/%s after %.2fs: %s",
                    type(exc).__name__, attempt + 1, attempts, delay, exc,
                )
                if delay > 0:
                    time.sleep(delay)
        raise RuntimeError("unreachable codex-plus retry loop exit")

    ChatCodexPlus._generate = _patched_generate
    setattr(ChatCodexPlus, _SYNC_RETRY_MARKER, True)


__all__ = [
    "install_codex_plus_transient_retry_patch",
]
