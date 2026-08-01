"""Mimir runtime fixes for DeepAgents conversation-history offloading."""

from __future__ import annotations

from functools import wraps
from typing import Any


_OFFLOAD_LOGGING_PATCH_MARKER = "_mimir_offload_traceback_logging"


class _LoggingBackendProxy:
    __slots__ = ("_backend", "_logger")

    def __init__(self, backend: Any, logger: Any) -> None:
        self._backend = backend
        self._logger = logger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def _log_exception(self, operation: str, path: str) -> None:
        self._logger.exception(
            "Exception during DeepAgents backend %s for %s", operation, path
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> Any:
        path = files[0][0] if files else "<empty upload>"
        try:
            return self._backend.upload_files(files)
        except Exception:
            self._log_exception("upload_files", path)
            raise

    def download_files(self, paths: list[str]) -> Any:
        path = paths[0] if paths else "<empty download>"
        try:
            return self._backend.download_files(paths)
        except Exception:
            self._log_exception("download_files", path)
            raise

    def write(self, file_path: str, content: str) -> Any:
        try:
            return self._backend.write(file_path, content)
        except Exception:
            self._log_exception("write", file_path)
            raise

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        try:
            return self._backend.edit(
                file_path, old_string, new_string, replace_all=replace_all
            )
        except Exception:
            self._log_exception("edit", file_path)
            raise

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> Any:
        path = files[0][0] if files else "<empty upload>"
        try:
            return await self._backend.aupload_files(files)
        except Exception:
            self._log_exception("aupload_files", path)
            raise

    async def adownload_files(self, paths: list[str]) -> Any:
        path = paths[0] if paths else "<empty download>"
        try:
            return await self._backend.adownload_files(paths)
        except Exception:
            self._log_exception("adownload_files", path)
            raise

    async def awrite(self, file_path: str, content: str) -> Any:
        try:
            return await self._backend.awrite(file_path, content)
        except Exception:
            self._log_exception("awrite", file_path)
            raise

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        try:
            return await self._backend.aedit(
                file_path, old_string, new_string, replace_all=replace_all
            )
        except Exception:
            self._log_exception("aedit", file_path)
            raise


def _wrap_sync_offload(original: Any, logger: Any) -> Any:
    @wraps(original)
    def wrapped(self: Any, backend: Any, messages: list[Any]) -> Any:
        return original(self, _LoggingBackendProxy(backend, logger), messages)

    setattr(wrapped, _OFFLOAD_LOGGING_PATCH_MARKER, True)
    return wrapped


def _wrap_async_offload(original: Any, logger: Any) -> Any:
    @wraps(original)
    async def wrapped(self: Any, backend: Any, messages: list[Any]) -> Any:
        return await original(self, _LoggingBackendProxy(backend, logger), messages)

    setattr(wrapped, _OFFLOAD_LOGGING_PATCH_MARKER, True)
    return wrapped


def install_offload_traceback_logging_patch() -> None:
    """Log DeepAgents offload exceptions while preserving upstream handling."""
    try:
        import deepagents.middleware.summarization as summarization
    except ImportError:
        return

    middleware = summarization.SummarizationMiddleware
    wrappers = {
        "_offload_inline_media": _wrap_sync_offload,
        "_aoffload_inline_media": _wrap_async_offload,
        "_offload_to_backend": _wrap_sync_offload,
        "_aoffload_to_backend": _wrap_async_offload,
    }
    for method_name, wrapper_factory in wrappers.items():
        current = getattr(middleware, method_name)
        if getattr(current, _OFFLOAD_LOGGING_PATCH_MARKER, False):
            continue
        setattr(
            middleware,
            method_name,
            wrapper_factory(current, summarization.logger),
        )
