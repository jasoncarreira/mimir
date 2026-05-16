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
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult


class WriteGuardBackend:
    """Filesystem backend that restricts writes to specific directories.

    ``writable_dirs`` are path fragments (e.g. ``"state"`` or
    ``"state/agent"``); each is normalized to an absolute POSIX path
    rooted at ``/`` for comparison. A write is allowed when its target
    is the writable root itself or a descendant.
    """

    def __init__(self, root_dir: Path, writable_dirs: list[str]) -> None:
        self._fs = FilesystemBackend(root_dir=root_dir, virtual_mode=True)
        self._writable_roots = [
            PurePosixPath("/" + d.strip("/")) for d in writable_dirs
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fs, name)

    def _is_write_allowed(self, file_path: str) -> bool:
        path = PurePosixPath("/" + file_path.lstrip("/"))
        return any(
            path == root or root in path.parents
            for root in self._writable_roots
        )

    def _allowed_dirs_label(self) -> str:
        return ", ".join(f"{r}/" for r in self._writable_roots)

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
        blocked = [path for path, _ in files if not self._is_write_allowed(path)]
        if blocked:
            return [FileUploadResponse(path=p, error="permission_denied") for p in blocked]
        return self._fs.upload_files(files)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)


class ReadOnlyFilesystemBackend:
    """Read-only wrapper: every write/edit/upload returns an error.

    Use via ``CompositeBackend`` to scope read-only treatment to a
    specific subtree (e.g. operator-shipped reference docs).
    """

    def __init__(self, root_dir: Path) -> None:
        self._fs = FilesystemBackend(root_dir=root_dir, virtual_mode=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fs, name)

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
