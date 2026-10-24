"""Mimir runtime fixes for DeepAgents conversation-history offloading."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any


_OFFLOAD_LOGGING_PATCH_MARKER = "_mimir_offload_traceback_logging"


def _history_file(path: str) -> Path | None:
    """Only the DeepAgents session file under the configured home is eligible."""
    home = os.environ.get("MIMIR_HOME", "").strip()
    requested = Path(path)
    if not home or requested.suffix != ".md":
        return None
    root = Path(home).resolve()
    candidate = root / str(requested).lstrip("/") if requested.parts[:2] == ("/", "conversation_history") else requested
    try:
        parent = root / "conversation_history"
        return candidate if (candidate.parent == parent or candidate.parent.parent == parent) and candidate.resolve(strict=True) == candidate else None
    except (OSError, RuntimeError, ValueError):
        return None


def _label_file(path: Path) -> Path:
    home = Path(os.environ["MIMIR_HOME"]).resolve()
    from .access_control import _valid_turn_scratch_id
    turn_id = path.parent.name if path.parent.parent == home / "conversation_history" else None
    parent = home / ".mimir" / "conversation_history_labels"
    if turn_id is not None and _valid_turn_scratch_id(turn_id):
        parent /= turn_id
    return parent / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def read_offload_labels(path: str) -> tuple[Any, ...] | None:
    """Return authenticated-by-content provenance; absent or stale records fail closed."""
    from .models import SourceLabel

    file = _history_file(path)
    if file is None:
        return None
    try:
        record = json.loads(_label_file(file).read_text(encoding="utf-8"))
        if record["version"] != 1 or record["sha256"] != hashlib.sha256(file.read_bytes()).hexdigest():
            return None
        sources = tuple(SourceLabel.from_record(item) for item in record["sources"])
        return sources if sources and all(source.is_complete for source in sources) else None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _source_record(source: Any) -> dict[str, Any]:
    # Do not persist owner attestations: only the server's original ingress can
    # mint them. Dropping one restricts egress rather than granting authority.
    return {
        "principal": source.principal, "domain": source.domain,
        "domain_qualifier": source.domain_qualifier, "resource_id": source.resource_id,
        "bridge_instance": source.bridge_instance, "sensitivity": source.sensitivity,
        "authorized_principals": sorted(source.authorized_principals),
        "source_kind": source.source_kind, "integrity": source.integrity,
        "integrity_effect": source.integrity_effect,
    }


def _record_offload(
    path: str,
    previous_hash: str | None,
    previous_sources: tuple[Any, ...] | None,
    expected_hash: str | None,
    *,
    path_mismatch: bool = False,
) -> None:
    from ._context import get_current_turn
    from .models import InformationFlowLabels
    from .access_control import protected_result_source

    file = _history_file(path)
    turn = get_current_turn()
    auth = getattr(turn, "auth_context", None)
    state = getattr(auth, "ifc_state", None)
    labels = state.current(getattr(auth, "ifc_labels", None)) if state is not None else getattr(auth, "ifc_labels", None)
    if file is None or expected_hash is None or not isinstance(labels, InformationFlowLabels) or not labels.sources:
        return
    # Upstream appends to an existing file. If those bytes were not attested,
    # retain their untrusted provenance even if this turn itself is clean.
    sources = [] if path_mismatch else list(previous_sources or ())
    if path_mismatch or (previous_hash is not None and previous_sources is None):
        sources.append(protected_result_source(
            auth, principal="filesystem", domain="filesystem",
            resource_id=str(file), bridge_instance="filesystem",
        ))
    sources.extend(labels.sources)
    try:
        # Bind the record to the bytes supplied to the successful backend
        # mutation, never to whatever a concurrent writer left on disk.
        record = {"version": 1, "sha256": expected_hash,
                  "sources": [_source_record(source) for source in dict.fromkeys(sources)]}
        destination = _label_file(file)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".offload-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, ValueError, TypeError, RuntimeError):
        # A missing or stale record is always an untrusted filesystem read.
        _discard_offload_record(path)
        return


def _previous_offload(path: str) -> tuple[str | None, tuple[Any, ...] | None]:
    file = _history_file(path)
    if file is None:
        return None, None
    try:
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
    except (OSError, RuntimeError):
        return None, None
    return digest, read_offload_labels(path)


class _LoggingBackendProxy:
    __slots__ = ("_backend", "_logger", "_downloaded", "written_hashes", "provenance_failed")

    def _capture_safely(self, capture: Any, *args: Any) -> Any:
        # The backend operation has already succeeded. Provenance is optional;
        # never turn a bookkeeping exception into an upstream operation failure.
        try:
            return capture(*args)
        except Exception:
            self.provenance_failed = True
            self.written_hashes.clear()
            self._logger.exception("DeepAgents offload provenance capture failed")
            return args[-1]

    def __init__(self, backend: Any, logger: Any) -> None:
        self._backend = backend
        self._logger = logger
        self._downloaded: dict[str, bytes] = {}
        self.written_hashes: dict[str, str] = {}
        self.provenance_failed = False

    def _capture_download(self, results: Any) -> Any:
        for result in results:
            if result.error is None and isinstance(result.content, bytes):
                self._downloaded[result.path] = result.content
        return results

    def _capture_write(self, path: str, content: str, result: Any) -> Any:
        if result.error is None:
            self.written_hashes[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return result

    def _capture_edit(
        self, path: str, old: str, new: str, replace_all: bool, result: Any,
    ) -> Any:
        previous = self._downloaded.get(path)
        if previous is not None and old and result.error is None:
            text = previous.decode("utf-8")
            # Only attest an edit whose exact input and replacement count we
            # observed; an upstream API change must fail closed.
            count = text.count(old)
            if count and (replace_all or count == 1):
                content = text.replace(old, new, -1 if replace_all else 1)
                self._capture_write(path, content, result)
        return result

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
            return self._capture_safely(self._capture_download, self._backend.download_files(paths))
        except Exception:
            self._log_exception("download_files", path)
            raise

    def write(self, file_path: str, content: str) -> Any:
        try:
            return self._capture_safely(self._capture_write, file_path, content, self._backend.write(file_path, content))
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
            result = self._backend.edit(
                file_path, old_string, new_string, replace_all=replace_all
            )
            return self._capture_safely(self._capture_edit, file_path, old_string, new_string, replace_all, result)
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
            return self._capture_safely(self._capture_download, await self._backend.adownload_files(paths))
        except Exception:
            self._log_exception("adownload_files", path)
            raise

    async def awrite(self, file_path: str, content: str) -> Any:
        try:
            result = await self._backend.awrite(file_path, content)
            return await asyncio.to_thread(self._capture_safely, self._capture_write, file_path, content, result)
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
            result = await self._backend.aedit(
                file_path, old_string, new_string, replace_all=replace_all
            )
            return await asyncio.to_thread(
                self._capture_safely, self._capture_edit,
                file_path, old_string, new_string, replace_all, result,
            )
        except Exception:
            self._log_exception("aedit", file_path)
            raise


def _finish_offload(
    proxy: _LoggingBackendProxy, path: str | None, result: str,
    previous_hash: str | None, previous_sources: tuple[Any, ...] | None,
) -> None:
    downloaded = proxy._downloaded.get(result)
    if downloaded is not None:
        downloaded_hash = hashlib.sha256(downloaded).hexdigest()
        if downloaded_hash != previous_hash:
            previous_hash, previous_sources = downloaded_hash, None
    _record_offload(
        result, previous_hash, previous_sources, proxy.written_hashes.get(result),
        path_mismatch=path != result,
    )


def _discard_offload_record(path: str) -> None:
    """Best-effort invalidation: history bytes must remain readable as untrusted."""
    try:
        file = _history_file(path)
        if file is not None:
            _label_file(file).unlink(missing_ok=True)
    except Exception:
        # If invalidation itself is unavailable, changed bytes still invalidate
        # the old digest. No fallback may mint a replacement provenance record.
        pass


def _prepare_offload(self: Any, session_id: Any, logger: Any) -> tuple[Any, ...]:
    try:
        path = self._get_history_path(session_id)
        previous_hash, previous_sources = _previous_offload(path) if path else (None, None)
        return path, previous_hash, previous_sources, False
    except Exception:
        logger.exception("DeepAgents offload provenance snapshot failed")
        return None, None, None, True


def _complete_offload(
    proxy: _LoggingBackendProxy, path: str | None, result: str,
    previous_hash: str | None, previous_sources: tuple[Any, ...] | None,
    failed: bool,
) -> None:
    try:
        if failed or proxy.provenance_failed:
            _discard_offload_record(result)
            return
        _finish_offload(proxy, path, result, previous_hash, previous_sources)
    except Exception:
        _discard_offload_record(result)
        proxy._logger.exception("DeepAgents offload provenance persistence failed")


def _wrap_sync_offload(original: Any, logger: Any) -> Any:
    @wraps(original)
    def wrapped(self: Any, backend: Any, *args: Any, **kwargs: Any) -> Any:
        # Forward everything after ``backend`` untouched. The wrapper exists only to
        # substitute the backend with a logging proxy, so it must not restate
        # upstream's parameter list: deepagents 0.7.10 added ``session_id`` to
        # ``_offload_to_backend``/``_aoffload_to_backend`` and a wrapper hardcoded to
        # ``(self, backend, messages)`` then raises "takes 3 positional arguments but
        # 4 were given" on every turn that offloads. The pin is ``>=0.7.1,<0.8``, so a
        # signature change inside that range is expected rather than exceptional.
        history = getattr(original, "__name__", "") == "_offload_to_backend"
        from ._context import get_current_turn
        from .turn_stores import turn_store
        try:
            turn = get_current_turn()
        except Exception:
            # Provenance bookkeeping is non-fatal; upstream may still offload.
            turn = None
        if history and hasattr(turn, "turn_scratch_path") and turn_store("conversation_history") is None:
            return None
        session_id = kwargs.get("session_id", args[1] if len(args) > 1 else None)
        path, previous_hash, previous_sources, failed = (
            _prepare_offload(self, session_id, logger) if history else (None, None, None, False)
        )
        proxy = _LoggingBackendProxy(backend, logger)
        result = original(self, proxy, *args, **kwargs)
        if history and result is not None:
            _complete_offload(proxy, path, result, previous_hash, previous_sources, failed)
        return result

    setattr(wrapped, _OFFLOAD_LOGGING_PATCH_MARKER, True)
    return wrapped


def _wrap_async_offload(original: Any, logger: Any) -> Any:
    @wraps(original)
    async def wrapped(self: Any, backend: Any, *args: Any, **kwargs: Any) -> Any:
        # See _wrap_sync_offload: forward past ``backend`` without restating upstream's
        # signature.
        history = getattr(original, "__name__", "") == "_aoffload_to_backend"
        from ._context import get_current_turn
        from .turn_stores import turn_store
        try:
            turn = get_current_turn()
        except Exception:
            turn = None
        if history and hasattr(turn, "turn_scratch_path") and turn_store("conversation_history") is None:
            return None
        session_id = kwargs.get("session_id", args[1] if len(args) > 1 else None)
        path, previous_hash, previous_sources, failed = (
            await asyncio.to_thread(_prepare_offload, self, session_id, logger)
            if history else (None, None, None, False)
        )
        proxy = _LoggingBackendProxy(backend, logger)
        result = await original(self, proxy, *args, **kwargs)
        if history and result is not None:
            await asyncio.to_thread(
                _complete_offload, proxy, path, result, previous_hash, previous_sources, failed,
            )
        return result

    setattr(wrapped, _OFFLOAD_LOGGING_PATCH_MARKER, True)
    return wrapped


def install_offload_traceback_logging_patch() -> None:
    """Log DeepAgents offload exceptions while preserving upstream handling."""
    try:
        import deepagents.middleware.summarization as summarization
    except ImportError:
        return

    middleware = summarization.SummarizationMiddleware
    if not getattr(middleware._get_history_path, "_mimir_turn_scoped", False):
        original_path = middleware._get_history_path

        @wraps(original_path)
        def turn_history_path(self: Any, session_id: str) -> str:
            from .turn_stores import turn_store

            prefix = turn_store("conversation_history")
            return f"{prefix}/{session_id}.md" if prefix else original_path(self, session_id)

        turn_history_path._mimir_turn_scoped = True
        middleware._get_history_path = turn_history_path
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
