"""Bounded regular-file sinks for durable Worklink child output."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile


@dataclass
class OutputSink:
    """An open regular file retained by the supervisor while the child owns a dup."""

    fd: int
    limit: int
    path: Path | None = None
    observed_size: int = 0
    did_overflow: bool = False

    @property
    def file(self) -> object:
        class _Writer:
            def __init__(self, fd: int) -> None:
                self.fd = fd

            def fileno(self) -> int:
                return self.fd

            def write(self, value: bytes) -> int:
                remaining = memoryview(value)
                while remaining:
                    written = os.write(self.fd, remaining)
                    if written <= 0:
                        raise OSError("incomplete Worklink output write")
                    remaining = remaining[written:]
                return len(value)

            def flush(self) -> None:
                pass

        return _Writer(self.fd)

    def truncate_to_limit(self, extra_bytes: int = 0) -> None:
        ceiling = self.limit + max(0, extra_bytes)
        size = os.fstat(self.fd).st_size
        self.observed_size = max(self.observed_size, size)
        if size > ceiling:
            self.did_overflow = True
            os.ftruncate(self.fd, ceiling)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def overflowed(self) -> bool:
        overflowed, _size = _enforce_limit(self)
        return overflowed

    def read_bounded(self, *, scrubber: object | None = None) -> tuple[bytes, int]:
        overflowed, size = _enforce_limit(self)
        read_limit = self.limit
        if scrubber is not None:
            read_limit += getattr(scrubber, "lookahead_bytes")()
        value = _read_sink(self, min(size, read_limit))
        if scrubber is not None and overflowed:
            keep = getattr(scrubber, "safe_truncation_length")(value, self.limit)
            value = value[:keep]
        else:
            value = value[:self.limit]
        return value, max(0, self.observed_size - len(value))


def _open_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )


def _ensure_private_root(root: Path) -> int:
    """Create/open *root* without following links through writable parents.

    Existing non-writable directories may be traversed; an existing final root is
    tightened to 0700 when owned by this process. Sticky directories (notably
    /tmp) are safe traversal points, but an ordinary group/world-writable parent
    is not.
    """
    if not root.is_absolute():
        raise ValueError("Worklink output root must be absolute")
    parts = root.parts
    current = Path(parts[0])
    fd = _open_directory(current)
    try:
        for index, component in enumerate(parts[1:], start=1):
            parent = os.fstat(fd)
            if parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
                parent.st_mode & stat.S_ISVTX
            ):
                raise PermissionError(f"unsafe writable Worklink output parent: {current}")
            final = index == len(parts) - 1
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=fd)
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
            except OSError as exc:
                raise PermissionError(
                    f"unsafe Worklink output directory component: {current / component}"
                ) from exc
            os.close(fd)
            fd = child_fd
            current /= component
            metadata = os.fstat(fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise PermissionError(f"Worklink output component is not a directory: {current}")
            if final:
                if metadata.st_uid != os.geteuid():
                    raise PermissionError(f"Worklink output root is not owner-controlled: {root}")
                if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise PermissionError(f"Worklink output root is writable by another user: {root}")
                os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_output_sink(path: Path | None, limit: int) -> OutputSink:
    if limit <= 0:
        raise ValueError("worker output limit must be positive")
    if path is None:
        temporary = tempfile.TemporaryFile(mode="w+b")
        fd = os.dup(temporary.fileno())
        temporary.close()
        return OutputSink(fd=fd, limit=limit)
    root_fd = _ensure_private_root(path.parent)
    try:
        if path.name in {"", ".", ".."}:
            raise ValueError("Worklink output filename is invalid")
        fd = os.open(
            path.name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=root_fd,
        )
    finally:
        os.close(root_fd)
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError("Worklink output sink is not an owner-controlled regular file")
    os.fchmod(fd, 0o600)
    return OutputSink(fd=fd, limit=limit, path=path)


def open_output_pair(
    stdout_path: Path | None,
    stdout_limit: int,
    stderr_path: Path | None,
    stderr_limit: int,
) -> tuple[OutputSink, OutputSink]:
    stdout = open_output_sink(stdout_path, stdout_limit)
    try:
        stderr = open_output_sink(stderr_path, stderr_limit)
    except BaseException:
        stdout.close()
        if stdout.path is not None:
            try:
                stdout.path.unlink()
            except FileNotFoundError:
                pass
        raise
    return stdout, stderr


def _enforce_limit(sink: OutputSink) -> tuple[bool, int]:
    size = os.fstat(sink.fd).st_size
    sink.observed_size = max(sink.observed_size, size)
    if size <= sink.limit:
        return sink.did_overflow, size
    sink.did_overflow = True
    return True, size


def _read_sink(sink: OutputSink, size: int) -> bytes:
    result = bytearray()
    offset = 0
    while offset < size:
        chunk = os.pread(sink.fd, min(64 * 1024, size - offset), offset)
        if not chunk:
            break
        result.extend(chunk)
        offset += len(chunk)
    return bytes(result)
