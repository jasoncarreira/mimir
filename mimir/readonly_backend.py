"""Filesystem backends that enforce per-directory write permissions.

``WriteGuardBackend`` wraps ``FilesystemBackend`` and blocks writes/edits
outside an operator-configured allowlist (``Config.writable_dirs``).
``ReadOnlyFilesystemBackend`` blocks every write. Both delegate read
operations to the underlying ``FilesystemBackend`` unchanged, so file
search and read tools keep working.

Wired into ``create_deep_agent(backend=...)`` from ``mimir.agent``.
Ported from open-strix; the ``LoggingWriteGuardBackend`` variant is
intentionally not carried over — mimir's ``turn_logger`` already
records the relevant events on the dispatcher side.

Security boundary: lexical ``..`` collapse + ``Path.resolve()``
defeat both ``"/state/../logs/x.txt"``-style traversal and symlink
escapes (a symlink under ``state/`` pointing into ``.mimir/`` resolves
out of the writable root and is rejected). Read methods are an
explicit allowlist — no ``__getattr__`` passthrough — so future
mutator methods added by deepagents (``delete_file``, ``rename``,
``mkdir``, …) don't silently bypass the guard.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult

log = logging.getLogger(__name__)


def _normalize_writable_dir(name: str) -> str | None:
    """Reject unsafe names (``.``, ``..``, absolute, traversal) up-front.

    Returns the cleaned name (no leading/trailing slash) or ``None`` if
    the input should be ignored. Logging the rejection at warning level
    keeps operator typos visible without crashing startup.
    """
    raw = name
    cleaned = name.strip().strip("/")
    if not cleaned:
        log.warning("WriteGuardBackend: ignoring empty writable_dir %r", raw)
        return None
    if cleaned in (".", ".."):
        log.warning(
            "WriteGuardBackend: ignoring writable_dir %r (would alias root)", raw,
        )
        return None
    parts = PurePosixPath(cleaned).parts
    if any(p in ("..", ".") for p in parts):
        log.warning(
            "WriteGuardBackend: ignoring writable_dir %r (contains traversal)", raw,
        )
        return None
    return cleaned


class WriteGuardBackend:
    """Filesystem backend that restricts writes to specific directories.

    ``writable_dirs`` are path fragments (e.g. ``"state"`` or
    ``"state/agent"``) interpreted relative to ``root_dir``. A write is
    allowed when the request's target, after lexical ``..`` collapse
    AND symlink resolution, resolves under one of the writable roots
    (which themselves must resolve under ``root_dir``).
    """

    # Methods we explicitly forward to the underlying FilesystemBackend.
    # Anything not in this list raises AttributeError — default-deny so
    # a future deepagents release adding ``delete_file`` / ``rename`` /
    # ``mkdir`` can't bypass the guard until we audit + wrap it.
    _ALLOWED_READS = frozenset({
        "read", "aread",
        "ls_info", "als_info",
        "grep_raw", "agrep_raw",
        "glob_info", "aglob_info",
        "execute", "aexecute",  # bash via backend — read-shaped from FS perspective
        "download_files", "adownload_files",
    })

    def __init__(self, root_dir: Path, writable_dirs: list[str]) -> None:
        self._root = Path(root_dir).resolve()
        self._fs = FilesystemBackend(root_dir=root_dir, virtual_mode=True)
        cleaned: list[str] = []
        for d in writable_dirs:
            normalized = _normalize_writable_dir(d)
            if normalized is not None:
                cleaned.append(normalized)
        # Pre-resolve writable roots so the per-write check just compares
        # resolved-target vs resolved-root. resolve(strict=False) handles
        # roots that don't exist yet (created lazily on first write).
        self._writable_roots: list[Path] = [
            (self._root / d).resolve() for d in cleaned
        ]
        # For error messages — the friendlier "/state/" form.
        self._writable_labels: list[str] = ["/" + d for d in cleaned]

    def __getattr__(self, name: str) -> Any:
        # Default-deny passthrough: only explicit reads forward.
        if name in self._ALLOWED_READS:
            return getattr(self._fs, name)
        raise AttributeError(
            f"{type(self).__name__} does not forward {name!r} — "
            f"add it to _ALLOWED_READS after auditing whether it's a mutator."
        )

    def _resolve_target(self, file_path: str) -> Path | None:
        """Resolve a tool-supplied path to a real filesystem location.

        Returns ``None`` when the input contains lexical traversal that
        survives normalization (cheap check before hitting the disk).
        """
        # Strip leading slashes so the input is always relative to root.
        relative = PurePosixPath(file_path.lstrip("/"))
        # Lexical traversal rejection before touching the disk: anything
        # whose normalized form starts with ``..`` is escaping.
        if any(p == ".." for p in relative.parts):
            return None
        try:
            return (self._root / relative).resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def _is_write_allowed(self, file_path: str) -> bool:
        resolved = self._resolve_target(file_path)
        if resolved is None:
            return False
        # Resolved target must live under at least one writable root.
        # ``is_relative_to`` was added in Python 3.9; we depend on >=3.11.
        return any(
            resolved == root or resolved.is_relative_to(root)
            for root in self._writable_roots
        )

    def _allowed_dirs_label(self) -> str:
        return ", ".join(f"{label}/" for label in self._writable_labels)

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._is_write_allowed(file_path):
            return WriteResult(
                error=f"Write blocked. Writable directories: {self._allowed_dirs_label()}",
            )
        return self._fs.write(file_path=file_path, content=content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path=file_path, content=content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not self._is_write_allowed(file_path):
            return EditResult(
                error=f"Edit blocked. Writable directories: {self._allowed_dirs_label()}",
            )
        return self._fs.edit(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        # Atomic: if ANY path is blocked we reject the entire batch.
        # The response shape is one entry per input path so the caller
        # can match input order; allowed-but-not-written paths get
        # ``permission_denied`` too (because nothing was uploaded).
        # This is the documented semantic — partial uploads with a
        # mixed response shape would be more surprising than failing
        # the batch cleanly.
        blocked_paths = {p for p, _ in files if not self._is_write_allowed(p)}
        if blocked_paths:
            return [
                FileUploadResponse(path=p, error="permission_denied")
                for p, _ in files
            ]
        return self._fs.upload_files(files)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)


class ReadOnlyFilesystemBackend:
    """Read-only wrapper: every write/edit/upload returns an error.

    Use via ``CompositeBackend`` to scope read-only treatment to a
    specific subtree (e.g. operator-shipped reference docs).
    """

    _ALLOWED_READS = WriteGuardBackend._ALLOWED_READS

    def __init__(self, root_dir: Path) -> None:
        self._fs = FilesystemBackend(root_dir=root_dir, virtual_mode=True)

    def __getattr__(self, name: str) -> Any:
        if name in self._ALLOWED_READS:
            return getattr(self._fs, name)
        raise AttributeError(
            f"{type(self).__name__} does not forward {name!r} — "
            f"add it to _ALLOWED_READS after auditing whether it's a mutator."
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"Write blocked. '{file_path}' is read-only.")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path=file_path, content=content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=f"Edit blocked. '{file_path}' is read-only.")

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)
