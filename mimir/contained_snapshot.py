from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from ._rmtree import rmtree_missing_ok


class ContainedSnapshotError(RuntimeError):
    reason_code = "snapshot_unavailable"


class SnapshotCredentialsRefused(ContainedSnapshotError):
    reason_code = "snapshot_credentials"

    def __init__(self, relative_path_count: int = 1) -> None:
        super().__init__("Snapshot credentials refused")
        self.relative_path_count = relative_path_count


class SnapshotUnsafeEntry(ContainedSnapshotError):
    reason_code = "unsafe_snapshot_entry"


class SnapshotEmbeddedRepository(SnapshotUnsafeEntry):
    """The source tree contains a nested Git repository.

    A file-level walk cannot capture one, so refusing is correct — but the
    condition is ordinary (a worktree, a vendored checkout), not a malformed or
    hostile path. It subclasses ``SnapshotUnsafeEntry`` so existing handlers keep
    catching it; the separate type and ``reason_code`` exist so the refusal reads
    as "your tree has a nested checkout" instead of sending the reader off to look
    for an attack.

    The offending path is deliberately NOT carried. Entry names are content — a
    contributor's branch can choose them — and this module already refuses to echo
    a path into a refusal an operator or the agent will read. Naming the category
    is what makes it diagnosable; the tree can be searched with
    ``git ls-files --others | grep '/$'``.
    """

    reason_code = "embedded_repository"

    def __init__(self) -> None:
        super().__init__("Snapshot source contains an embedded Git repository")


class SnapshotSourceChanged(ContainedSnapshotError):
    reason_code = "snapshot_source_changed"


class SnapshotUnavailable(ContainedSnapshotError):
    reason_code = "snapshot_unavailable"


GitInventoryClass = Literal["tracked", "untracked", "ignored"]
SnapshotGitRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class SnapshotEntry:
    relative_path: bytes
    inventory_class: GitInventoryClass
    mode: int | None
    device: int | None
    inode: int | None
    size: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    symlink_target: bytes | None = None


@dataclass(frozen=True)
class SnapshotResult:
    destination: Path
    tracked_count: int
    untracked_count: int
    ignored_count: int


_EXACT_SENSITIVE_NAMES = frozenset(
    {
        b".env",
        b".netrc",
        b".npmrc",
        b".pypirc",
        b".git-credentials",
        b"credentials",
        b"credentials.json",
        b"auth.json",
        b"id_rsa",
        b"id_dsa",
        b"id_ecdsa",
        b"id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = (b".pem", b".key", b".p12", b".pfx")
_SECRET_LINE = re.compile(
    rb"^[ \t]*[A-Za-z0-9_.-]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CLIENT_SECRET|REFRESH_TOKEN)[ \t]*[=:][ \t]*[^ \t\r\n]",
    re.IGNORECASE,
)
_PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")
_CHUNK_SIZE = 64 * 1024
_SOURCE_GIT_CONFIG = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
)


def _subprocess_git_runner(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _run_git(
    source: bytes,
    args: Sequence[bytes],
    *,
    git_runner: SnapshotGitRunner | None = None,
) -> bytes:
    argv = (
        "git", "-C", os.fsdecode(source), *_SOURCE_GIT_CONFIG,
        *(os.fsdecode(value) for value in args),
    )
    try:
        completed = (git_runner or _subprocess_git_runner)(argv)
    except OSError as exc:
        raise SnapshotUnavailable("Snapshot unavailable") from exc
    if completed.returncode != 0:
        raise SnapshotUnavailable("Snapshot unavailable")
    return completed.stdout


def _inventory(
    source: bytes,
    kind: GitInventoryClass,
    *,
    git_runner: SnapshotGitRunner | None = None,
) -> tuple[bytes, ...]:
    args: dict[GitInventoryClass, tuple[bytes, ...]] = {
        "tracked": (b"ls-files", b"-z"),
        "untracked": (b"ls-files", b"-z", b"--others", b"--exclude-standard"),
        "ignored": (
            b"ls-files",
            b"-z",
            b"--others",
            b"--ignored",
            b"--exclude-standard",
        ),
    }
    output = (
        _run_git(source, args[kind])
        if git_runner is None
        else _run_git(source, args[kind], git_runner=git_runner)
    )
    if output and not output.endswith(b"\0"):
        raise SnapshotUnavailable("Snapshot unavailable")
    entries = output.split(b"\0")
    return tuple(entries[:-1] if output else ())


def _head_revision(
    source: bytes, *, git_runner: SnapshotGitRunner | None = None,
) -> bytes:
    arguments = (b"rev-parse", b"--verify", b"HEAD")
    revision = (
        _run_git(source, arguments)
        if git_runner is None
        else _run_git(source, arguments, git_runner=git_runner)
    ).strip()
    if not re.fullmatch(rb"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
        raise SnapshotUnavailable("Snapshot unavailable")
    return revision.lower()


def _validate_relative_path(relative: bytes) -> None:
    if relative.endswith(b"/"):
        # ``_inventory`` runs ``git ls-files --others`` WITHOUT ``--directory``, so
        # git lists files individually -- except for a nested repository, which it
        # refuses to descend into and reports as a single directory-shaped entry.
        # A path component can never contain "/", so a trailing slash identifies
        # that case unambiguously. Checked before the component scan below, which
        # would otherwise reject the trailing empty component as a malformed path
        # and lose the reason.
        raise SnapshotEmbeddedRepository()
    if not relative or relative.startswith(b"/") or b"\0" in relative:
        raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")
    components = relative.split(b"/")
    if any(component in (b"", b".", b"..") for component in components):
        raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")


def _name_is_sensitive(relative: bytes) -> bool:
    components = relative.split(b"/")
    for index, original in enumerate(components):
        component = original.lower()
        if component in _EXACT_SENSITIVE_NAMES:
            return True
        if component.startswith(b"service-account") and component.endswith(b".json"):
            return True
        if component.endswith(_SENSITIVE_SUFFIXES):
            return True
        if component.startswith(b".env."):
            is_exempt_basename = index == len(components) - 1 and original == b".env.example"
            if not is_exempt_basename:
                return True
    return False


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _entry_signature(entry: SnapshotEntry) -> tuple[int, int, int, int, int, int]:
    assert entry.device is not None
    assert entry.inode is not None
    assert entry.mode is not None
    assert entry.size is not None
    assert entry.mtime_ns is not None
    assert entry.ctime_ns is not None
    return (
        entry.device,
        entry.inode,
        entry.mode,
        entry.size,
        entry.mtime_ns,
        entry.ctime_ns,
    )


def _content_is_sensitive(fd: int, known_sensitive: tuple[bytes, ...]) -> bool:
    line_buffer = b""
    overlap = max(
        len(_PRIVATE_KEY_HEADER.pattern),
        *(len(value) for value in known_sensitive),
        1,
    ) - 1
    carry = b""
    while True:
        chunk = os.read(fd, _CHUNK_SIZE)
        if not chunk:
            break
        searchable = carry + chunk
        if _PRIVATE_KEY_HEADER.search(searchable):
            return True
        if any(value in searchable for value in known_sensitive):
            return True
        carry = searchable[-overlap:] if overlap else b""
        line_buffer += chunk
        lines = line_buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            line_buffer = lines.pop()
        else:
            line_buffer = b""
        if any(_SECRET_LINE.search(line) for line in lines):
            return True
    return bool(line_buffer and _SECRET_LINE.search(line_buffer))


def _open_regular(source_path: bytes, expected: SnapshotEntry | None = None) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source_path, flags)
        observed = os.fstat(fd)
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        raise SnapshotSourceChanged("Snapshot source changed") from exc
    if not stat.S_ISREG(observed.st_mode):
        os.close(fd)
        raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")
    if expected is not None and _stat_signature(observed) != _entry_signature(expected):
        os.close(fd)
        raise SnapshotSourceChanged("Snapshot source changed")
    return fd, observed


def _scan_regular(
    source_path: bytes,
    relative: bytes,
    kind: GitInventoryClass,
    known_sensitive: tuple[bytes, ...],
    *,
    scan_credentials: bool,
) -> SnapshotEntry:
    fd, before = _open_regular(source_path)
    try:
        sensitive = scan_credentials and (
            _name_is_sensitive(relative) or _content_is_sensitive(fd, known_sensitive)
        )
        after = os.fstat(fd)
    except OSError as exc:
        raise SnapshotSourceChanged("Snapshot source changed") from exc
    finally:
        os.close(fd)
    if _stat_signature(before) != _stat_signature(after):
        raise SnapshotSourceChanged("Snapshot source changed")
    if sensitive:
        raise SnapshotCredentialsRefused("Snapshot credentials refused")
    return SnapshotEntry(
        relative,
        kind,
        before.st_mode,
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def _safe_symlink_target(relative: bytes, target: bytes) -> bool:
    if not target or target.startswith(b"/") or b"\0" in target:
        return False
    stack = relative.split(b"/")[:-1]
    for component in target.split(b"/"):
        if component in (b"", b"."):
            continue
        if component == b"..":
            if not stack:
                return False
            stack.pop()
        else:
            stack.append(component)
    return True


def _inspect_entry(
    source: bytes,
    relative: bytes,
    kind: GitInventoryClass,
    known_sensitive: tuple[bytes, ...],
    *,
    scan_credentials: bool,
) -> SnapshotEntry:
    _validate_relative_path(relative)
    source_path = os.path.join(source, relative)
    try:
        observed = os.lstat(source_path)
    except FileNotFoundError:
        if kind == "tracked":
            return SnapshotEntry(relative, kind, None, None, None, None, None, None)
        raise SnapshotSourceChanged("Snapshot source changed")
    except OSError as exc:
        raise SnapshotSourceChanged("Snapshot source changed") from exc
    if stat.S_ISREG(observed.st_mode):
        return _scan_regular(
            source_path,
            relative,
            kind,
            known_sensitive,
            scan_credentials=scan_credentials,
        )
    if stat.S_ISLNK(observed.st_mode):
        try:
            target = os.readlink(source_path)
            after = os.lstat(source_path)
        except OSError as exc:
            raise SnapshotSourceChanged("Snapshot source changed") from exc
        target_bytes = os.fsencode(target)
        if _stat_signature(observed) != _stat_signature(after):
            raise SnapshotSourceChanged("Snapshot source changed")
        if not _safe_symlink_target(relative, target_bytes):
            raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")
        return SnapshotEntry(
            relative,
            kind,
            observed.st_mode,
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
            target_bytes,
        )
    raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")


def _excluded(relative: bytes, excluded_prefixes: tuple[bytes, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix + b"/")
        for prefix in excluded_prefixes
    )


def _validated_excluded_prefixes(values: Iterable[bytes]) -> tuple[bytes, ...]:
    result = tuple(values)
    for value in result:
        _validate_relative_path(value)
        if b"/" in value or value == b".git":
            raise ValueError("snapshot exclusions must name exact top-level directories")
    if len(set(result)) != len(result):
        raise ValueError("snapshot exclusions must be unique")
    return result


def _refuse_special_files(source: bytes, excluded_prefixes: tuple[bytes, ...]) -> None:
    def unavailable(_error: OSError) -> None:
        raise SnapshotSourceChanged("Snapshot source changed")

    for directory, names, files in os.walk(source, topdown=True, followlinks=False, onerror=unavailable):
        if directory == source:
            names[:] = [
                name for name in names
                if name != b".git" and not _excluded(name, excluded_prefixes)
            ]
        for name in [*names, *files]:
            path = os.path.join(directory, name)
            try:
                observed = os.lstat(path)
            except OSError as exc:
                raise SnapshotSourceChanged("Snapshot source changed") from exc
            if not (stat.S_ISDIR(observed.st_mode) or stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode)):
                raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")


def preflight_git_snapshot(
    source: str | os.PathLike[str],
    *,
    known_sensitive: Iterable[bytes] = (),
    scan_tracked_credentials: bool = True,
    excluded_prefixes: Iterable[bytes] = (),
    git_runner: SnapshotGitRunner | None = None,
) -> tuple[SnapshotEntry, ...]:
    source_bytes = os.fsencode(os.path.abspath(os.fspath(source)))
    excluded = _validated_excluded_prefixes(excluded_prefixes)
    _refuse_special_files(source_bytes, excluded)
    sensitive = tuple(value for value in known_sensitive if value)
    inventories = {
        kind: _inventory(source_bytes, kind, git_runner=git_runner)
        for kind in ("tracked", "untracked", "ignored")
    }
    seen: set[bytes] = set()
    result: list[SnapshotEntry] = []
    credential_count = 0
    for kind in ("tracked", "untracked", "ignored"):
        for relative in inventories[kind]:
            if _excluded(relative.rstrip(b"/"), excluded):
                continue
            if relative in seen:
                raise SnapshotUnavailable("Snapshot unavailable")
            seen.add(relative)
            try:
                # A repo_test checkout may exempt only content Git inventories as
                # tracked: it is already committed and visible to the same agent.
                # Untracked and ignored material crosses a real disclosure boundary
                # and must always take the existing credential-scanning path.
                scan_credentials = kind != "tracked" or scan_tracked_credentials
                result.append(
                    _inspect_entry(
                        source_bytes,
                        relative,
                        kind,
                        sensitive,
                        scan_credentials=scan_credentials,
                    )
                )
            except SnapshotCredentialsRefused:
                credential_count += 1
    if credential_count:
        raise SnapshotCredentialsRefused(credential_count)
    return tuple(result)


def _remove_destination_entry(path: bytes) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
        rmtree_missing_ok(path)
    else:
        os.unlink(path)


def _copy_regular(source_path: bytes, destination_path: bytes, entry: SnapshotEntry) -> None:
    fd, before = _open_regular(source_path, entry)
    temporary = destination_path + b".mimir-snapshot-tmp"
    _remove_destination_entry(temporary)
    try:
        destination_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            while True:
                chunk = os.read(fd, _CHUNK_SIZE)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        after = os.fstat(fd)
        if _stat_signature(before) != _stat_signature(after):
            raise SnapshotSourceChanged("Snapshot source changed")
        executable = bool(before.st_mode & stat.S_IXUSR)
        os.chmod(temporary, 0o755 if executable else 0o644)
        os.replace(temporary, destination_path)
    except OSError as exc:
        raise SnapshotSourceChanged("Snapshot source changed") from exc
    finally:
        os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _overlay_entry(source: bytes, destination: bytes, entry: SnapshotEntry) -> None:
    source_path = os.path.join(source, entry.relative_path)
    destination_path = os.path.join(destination, entry.relative_path)
    if entry.mode is None:
        _remove_destination_entry(destination_path)
        return
    parent = os.path.dirname(destination_path)
    try:
        os.makedirs(parent, mode=0o755, exist_ok=True)
    except OSError as exc:
        raise SnapshotUnavailable("Snapshot unavailable") from exc
    _remove_destination_entry(destination_path)
    if stat.S_ISREG(entry.mode):
        _copy_regular(source_path, destination_path, entry)
        return
    if stat.S_ISLNK(entry.mode):
        try:
            observed = os.lstat(source_path)
            target = os.fsencode(os.readlink(source_path))
        except OSError as exc:
            raise SnapshotSourceChanged("Snapshot source changed") from exc
        if _stat_signature(observed) != _entry_signature(entry) or target != entry.symlink_target:
            raise SnapshotSourceChanged("Snapshot source changed")
        try:
            os.symlink(target, destination_path)
        except OSError as exc:
            raise SnapshotUnavailable("Snapshot unavailable") from exc
        return
    raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")


def create_git_snapshot(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    known_sensitive: Iterable[bytes] = (),
    scan_tracked_credentials: bool = True,
    excluded_prefixes: Iterable[bytes] = (),
    source_git_runner: SnapshotGitRunner | None = None,
    clone_runner: SnapshotGitRunner | None = None,
) -> SnapshotResult:
    try:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve(strict=False)
        destination_path.relative_to(source_path)
    except ValueError:
        pass
    except OSError as exc:
        raise SnapshotUnavailable("Snapshot unavailable") from exc
    else:
        raise SnapshotUnsafeEntry("Snapshot contains an unsafe entry")
    if destination_path.exists() or destination_path.is_symlink():
        raise SnapshotUnavailable("Snapshot unavailable")
    source_bytes = os.fsencode(source_path)
    destination_bytes = os.fsencode(destination_path)
    revision = _head_revision(source_bytes, git_runner=source_git_runner)
    excluded = _validated_excluded_prefixes(excluded_prefixes)
    entries = preflight_git_snapshot(
        source_path,
        known_sensitive=known_sensitive,
        scan_tracked_credentials=scan_tracked_credentials,
        excluded_prefixes=excluded,
        git_runner=source_git_runner,
    )
    try:
        clone_argv = (
            b"git", *(os.fsencode(value) for value in _SOURCE_GIT_CONFIG),
            b"clone", b"--no-hardlinks", b"--no-checkout", b"--quiet",
            b"--", source_bytes, destination_bytes,
        )
        completed = (
            subprocess.run(
                clone_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if clone_runner is None
            else clone_runner(tuple(os.fsdecode(part) for part in clone_argv))
        )
        if completed.returncode != 0:
            raise SnapshotUnavailable("Snapshot unavailable")
        checkout = subprocess.run(
            [
                b"git", b"-C", destination_bytes,
                *(os.fsencode(value) for value in _SOURCE_GIT_CONFIG),
                b"checkout", b"--detach", b"--force", b"--quiet", revision,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if checkout.returncode != 0:
            raise SnapshotSourceChanged("Snapshot source changed")
        for prefix in excluded:
            _remove_destination_entry(os.path.join(destination_bytes, prefix))
        for entry in entries:
            _overlay_entry(source_bytes, destination_bytes, entry)
        verified_entries = preflight_git_snapshot(
            source_path,
            known_sensitive=known_sensitive,
            scan_tracked_credentials=scan_tracked_credentials,
            excluded_prefixes=excluded,
            git_runner=source_git_runner,
        )
        if (
            _head_revision(source_bytes, git_runner=source_git_runner) != revision
            or verified_entries != entries
        ):
            raise SnapshotSourceChanged("Snapshot source changed")
    except ContainedSnapshotError:
        shutil.rmtree(destination_path, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(destination_path, ignore_errors=True)
        raise SnapshotUnavailable("Snapshot unavailable") from exc
    counts = {kind: 0 for kind in ("tracked", "untracked", "ignored")}
    for entry in entries:
        counts[entry.inventory_class] += 1
    return SnapshotResult(destination_path, counts["tracked"], counts["untracked"], counts["ignored"])
