"""Mimir runtime fixes for DeepAgents conversation-history offloading."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import get_buffer_string


_OFFLOAD_LOGGING_PATCH_MARKER = "_mimir_offload_traceback_logging"


def install_offload_traceback_logging_patch() -> None:
    """Log DeepAgents history-offload exceptions without making them fatal."""
    try:
        import deepagents.middleware.summarization as summarization
    except ImportError:
        return

    middleware = summarization.SummarizationMiddleware
    current = middleware._offload_to_backend
    if getattr(current, _OFFLOAD_LOGGING_PATCH_MARKER, False):
        return

    def _offload_to_backend(self: Any, backend: Any, messages: list[Any]) -> str | None:
        path = self._get_history_path()
        filtered_messages = self._filter_summary_messages(messages)
        timestamp = datetime.now(UTC).isoformat()
        new_section = (
            f"## Summarized at {timestamp}\n\n"
            f"{get_buffer_string(filtered_messages)}\n\n"
        )

        existing_content = ""
        try:
            responses = backend.download_files([path])
            if (
                responses
                and responses[0].content is not None
                and responses[0].error is None
            ):
                existing_content = responses[0].content.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            summarization.logger.debug(
                "Exception reading existing history from %s "
                "(treating as new file): %s: %s",
                path,
                type(exc).__name__,
                exc,
            )

        combined_content = existing_content + new_section
        try:
            result = (
                backend.edit(path, existing_content, combined_content)
                if existing_content
                else backend.write(path, combined_content)
            )
            if result is None or result.error:
                error_msg = result.error if result else "backend returned None"
                summarization.logger.warning(
                    "Failed to offload conversation history to %s (%d messages): %s",
                    path,
                    len(filtered_messages),
                    error_msg,
                )
                return None
        except Exception:  # noqa: BLE001
            summarization.logger.exception(
                "Exception offloading conversation history to %s (%d messages)",
                path,
                len(filtered_messages),
            )
            return None

        summarization.logger.debug(
            "Offloaded %d messages to %s", len(filtered_messages), path
        )
        return path

    setattr(_offload_to_backend, _OFFLOAD_LOGGING_PATCH_MARKER, True)
    _offload_to_backend.__wrapped__ = current  # type: ignore[attr-defined]
    middleware._offload_to_backend = _offload_to_backend
