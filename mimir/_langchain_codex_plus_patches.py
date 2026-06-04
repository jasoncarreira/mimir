"""Runtime compatibility patches for ``langchain-codex-plus``.

Mimir normally prefers upstream fixes in provider packages. This module is for
small, defensive patches that protect production turns until the provider can
ship the behavior itself. The functions are intentionally import-light: nothing
here imports ``langchain_codex_plus`` or ``httpx`` at module import time, so
operators not using the Codex Plus extra don't pay for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

_STREAMING_RETRY_MARKER = "_mimir_codex_plus_transient_retry_patched"
_SYNC_RETRY_MARKER = "_mimir_codex_plus_sync_transient_retry_patched"
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

    _patch_astream(ChatCodexPlus)
    _patch_generate(ChatCodexPlus)


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
            try:
                async for chunk in original(self, *args, **kwargs):
                    yielded = True
                    yield chunk
                return
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
