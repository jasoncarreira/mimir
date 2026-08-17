from __future__ import annotations

from dataclasses import dataclass
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

from .._rmtree import rmtree_missing_ok


_MAX_FILE_BYTES = 1024 * 1024
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class SafeGitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitDirectoryIdentity:
    device: int
    inode: int

    @classmethod
    def from_fd(cls, fd: int) -> GitDirectoryIdentity:
        value = os.fstat(fd)
        if not stat.S_ISDIR(value.st_mode):
            raise SafeGitError("retained Git object is not a directory")
        return cls(value.st_dev, value.st_ino)


class ControllerGitPublication:
    def __init__(
        self,
        *,
        checkout_fd: int,
        git_fd: int,
        object_fd: int,
        checkout_identity: GitDirectoryIdentity,
        git_identity: GitDirectoryIdentity,
        object_identity: GitDirectoryIdentity,
        metadata_path: Path,
        metadata_identity: GitDirectoryIdentity,
        branch: str,
        branch_ref: str,
        initial_head: str,
        push_url: str,
        repo_slug: str | None,
        user_name: str | None,
        user_email: str | None,
        credential_helpers: tuple[str, ...],
    ) -> None:
        self._checkout_fd = checkout_fd
        self._git_fd = git_fd
        self._object_fd = object_fd
        self.checkout_identity = checkout_identity
        self.git_identity = git_identity
        self.object_identity = object_identity
        self.metadata_path = metadata_path
        self._metadata_identity = metadata_identity
        self.branch = branch
        self.branch_ref = branch_ref
        self.initial_head = initial_head
        self.push_url = push_url
        self.repo_slug = repo_slug
        self.user_name = user_name
        self.user_email = user_email
        self.credential_helpers = credential_helpers
        self._git_path = metadata_path / "git"
        self._index_path = metadata_path / "index"
        self._closed = False
        self._lock = threading.Lock()

    @classmethod
    def capture(
        cls,
        checkout_fd: int,
        trusted_repo: str | Path,
        branch: str,
        metadata_root: str | Path,
    ) -> ControllerGitPublication:
        branch_ref = _validated_branch_ref(branch)
        retained_checkout_fd = os.dup(checkout_fd)
        git_fd = -1
        object_fd = -1
        metadata_path: Path | None = None
        try:
            checkout_identity = GitDirectoryIdentity.from_fd(retained_checkout_fd)
            git_fd = _open_directory(retained_checkout_fd, ".git", "checkout Git directory")
            git_identity = GitDirectoryIdentity.from_fd(git_fd)
            object_fd = _open_directory(git_fd, "objects", "checkout Git object database")
            object_identity = GitDirectoryIdentity.from_fd(object_fd)
            initial_head = _read_head(git_fd)
            trusted = Path(trusted_repo)
            push_url = _capture_push_url(trusted, branch)
            repo_slug = _repo_slug(push_url)
            user_name = _git_config_value(trusted, "user.name")
            user_email = _git_config_value(trusted, "user.email")
            helpers = tuple(_git_config_values(trusted, "credential.helper"))
            root = Path(metadata_root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_value = root.stat()
            if not stat.S_ISDIR(root_value.st_mode) or root_value.st_uid != os.geteuid():
                raise SafeGitError("publication metadata root is not controller-owned")
            metadata_path = Path(tempfile.mkdtemp(prefix="publication-", dir=root))
            metadata_path.chmod(0o700)
            metadata_fd = os.open(metadata_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                metadata_identity = GitDirectoryIdentity.from_fd(metadata_fd)
            finally:
                os.close(metadata_fd)
            _initialize_metadata(
                metadata_path,
                branch_ref=branch_ref,
                initial_head=initial_head,
                object_fd=object_fd,
                user_name=user_name,
                user_email=user_email,
                credential_helpers=helpers,
                pass_fds=(retained_checkout_fd, git_fd, object_fd),
            )
            return cls(
                checkout_fd=retained_checkout_fd,
                git_fd=git_fd,
                object_fd=object_fd,
                checkout_identity=checkout_identity,
                git_identity=git_identity,
                object_identity=object_identity,
                metadata_path=metadata_path,
                metadata_identity=metadata_identity,
                branch=branch,
                branch_ref=branch_ref,
                initial_head=initial_head,
                push_url=push_url,
                repo_slug=repo_slug,
                user_name=user_name,
                user_email=user_email,
                credential_helpers=helpers,
            )
        except BaseException:
            if metadata_path is not None:
                shutil.rmtree(metadata_path, ignore_errors=True)
            if object_fd >= 0:
                os.close(object_fd)
            if git_fd >= 0:
                os.close(git_fd)
            os.close(retained_checkout_fd)
            raise

    def run(
        self,
        *args: str,
        check: bool = False,
        input: str | bytes | None = None,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        if not args or any(not isinstance(value, str) or "\x00" in value for value in args):
            raise ValueError("publication Git arguments must be non-empty strings")
        with self._lock:
            self._require_open()
            self._validate_identities()
            command = [
                "git",
                f"--git-dir={self._git_path}",
                f"--work-tree={_fd_path(self._checkout_fd)}",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.pager=cat",
                *args,
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                env=_publication_environment(self._index_path),
                input=input,
                pass_fds=(self._checkout_fd, self._git_fd, self._object_fd),
                text=text,
                timeout=timeout,
            )
            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    command,
                    output=result.stdout,
                    stderr=result.stderr,
                )
            return result

    def push(
        self,
        *,
        check: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        return self.run(
            "push",
            self.push_url,
            f"HEAD:{self.branch_ref}",
            check=check,
            timeout=timeout,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            os.close(self._object_fd)
            os.close(self._git_fd)
            os.close(self._checkout_fd)
            try:
                value = self.metadata_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                value = None
            if value is not None and GitDirectoryIdentity(value.st_dev, value.st_ino) == self._metadata_identity:
                rmtree_missing_ok(self.metadata_path)
            self._closed = True

    def __enter__(self) -> ControllerGitPublication:
        self._require_open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise SafeGitError("publication Git handle is closed")

    def _validate_identities(self) -> None:
        if GitDirectoryIdentity.from_fd(self._checkout_fd) != self.checkout_identity:
            raise SafeGitError("retained checkout identity changed")
        if GitDirectoryIdentity.from_fd(self._git_fd) != self.git_identity:
            raise SafeGitError("retained Git directory identity changed")
        if GitDirectoryIdentity.from_fd(self._object_fd) != self.object_identity:
            raise SafeGitError("retained Git object database identity changed")
        reopened_git = _open_directory(self._checkout_fd, ".git", "checkout Git directory")
        try:
            if GitDirectoryIdentity.from_fd(reopened_git) != self.git_identity:
                raise SafeGitError("checkout Git directory was replaced")
        finally:
            os.close(reopened_git)
        reopened_objects = _open_directory(self._git_fd, "objects", "checkout Git object database")
        try:
            if GitDirectoryIdentity.from_fd(reopened_objects) != self.object_identity:
                raise SafeGitError("checkout Git object database was replaced")
        finally:
            os.close(reopened_objects)


def _initialize_metadata(
    metadata_path: Path,
    *,
    branch_ref: str,
    initial_head: str,
    object_fd: int,
    user_name: str | None,
    user_email: str | None,
    credential_helpers: tuple[str, ...],
    pass_fds: tuple[int, ...],
) -> None:
    git_path = metadata_path / "git"
    (git_path / "objects" / "info").mkdir(parents=True)
    (git_path / "objects" / "pack").mkdir()
    ref_path = git_path.joinpath(*branch_ref.split("/"))
    ref_path.parent.mkdir(parents=True)
    (git_path / "HEAD").write_text(f"ref: {branch_ref}\n")
    ref_path.write_text(f"{initial_head}\n")
    (git_path / "objects" / "info" / "alternates").write_text(f"{_fd_path(object_fd)}\n")
    config_path = git_path / "config"
    for key, values in (
        ("user.name", (() if user_name is None else (user_name,))),
        ("user.email", (() if user_email is None else (user_email,))),
        ("credential.helper", credential_helpers),
    ):
        for value in values:
            result = subprocess.run(
                ["git", "config", "--file", str(config_path), "--add", key, value],
                capture_output=True,
                check=False,
                env=_capture_environment(),
                text=True,
            )
            if result.returncode != 0:
                raise SafeGitError(f"could not create private Git setting {key}")
    result = subprocess.run(
        ["git", f"--git-dir={git_path}", "read-tree", initial_head],
        capture_output=True,
        check=False,
        env=_publication_environment(metadata_path / "index"),
        pass_fds=pass_fds,
        text=True,
    )
    if result.returncode != 0:
        raise SafeGitError(f"could not initialize private Git index: {result.stderr.strip()}")


def _validated_branch_ref(branch: str) -> str:
    if not isinstance(branch, str) or not branch or "\x00" in branch:
        raise SafeGitError("publication branch is invalid")
    branch_ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "check-ref-format", branch_ref],
        capture_output=True,
        check=False,
        env=_capture_environment(),
        text=True,
    )
    if result.returncode != 0:
        raise SafeGitError("publication branch is invalid")
    return branch_ref


def _read_head(git_fd: int) -> str:
    head = _read_regular_file(git_fd, "HEAD").decode().strip()
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ")
        if not ref.startswith("refs/") or any(part in ("", ".", "..") for part in ref.split("/")):
            raise SafeGitError("checkout HEAD is invalid")
        try:
            value = _read_regular_file(git_fd, ref).decode().strip()
        except FileNotFoundError:
            value = _packed_ref(git_fd, ref)
    else:
        value = head
    if not _OBJECT_ID.fullmatch(value):
        raise SafeGitError("checkout HEAD is invalid")
    return value


def _packed_ref(git_fd: int, ref: str) -> str:
    try:
        contents = _read_regular_file(git_fd, "packed-refs").decode()
    except FileNotFoundError as exc:
        raise SafeGitError("checkout HEAD reference is unavailable") from exc
    for line in contents.splitlines():
        if line.startswith(("#", "^")):
            continue
        fields = line.split(" ", 1)
        if len(fields) == 2 and fields[1] == ref:
            return fields[0]
    raise SafeGitError("checkout HEAD reference is unavailable")


def _capture_push_url(repo: Path, branch: str) -> str:
    remote = _git_config_value(repo, f"branch.{branch}.remote") or "origin"
    if remote == ".":
        raise SafeGitError("trusted Git branch has no publication remote")
    values = _git_config_values(repo, f"remote.{remote}.pushurl")
    if not values:
        values = _git_config_values(repo, f"remote.{remote}.url")
    if not values or not values[-1]:
        raise SafeGitError("trusted Git publication URL is unavailable")
    return values[-1]


def _git_config_value(repo: Path, key: str) -> str | None:
    values = _git_config_values(repo, key)
    return values[-1] if values else None


def _git_config_values(repo: Path, key: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "config", "--get-all", key],
        capture_output=True,
        check=False,
        env=_capture_environment(),
        text=True,
    )
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise SafeGitError(f"could not capture trusted Git setting {key}")
    return result.stdout.splitlines()


def _repo_slug(url: str) -> str | None:
    value = url.rstrip("/")
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif "github.com/" in value:
        value = value.rsplit("github.com/", 1)[1]
    else:
        return None
    slug = value.removesuffix(".git")
    return slug if slug.count("/") == 1 and all(slug.split("/")) else None


def _capture_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not _is_git_or_prompt_environment(key)
    }


def _publication_environment(index_path: Path) -> dict[str, str]:
    environment = _capture_environment()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
            "GIT_INDEX_FILE": str(index_path),
        }
    )
    return environment


def _is_git_or_prompt_environment(key: str) -> bool:
    upper = key.upper()
    return (
        upper.startswith("GIT_")
        or upper.startswith("SSH_")
        or upper in {"PAGER", "EDITOR", "VISUAL"}
    )


def _open_directory(directory_fd: int, path: str, label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if sys.platform.startswith("linux"):
        return _openat2(directory_fd, path, flags, label)
    try:
        return os.open(path, flags | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise SafeGitError(f"{label} is unavailable or unsafe") from exc


def _openat2(directory_fd: int, path: str, flags: int, label: str) -> int:
    class OpenHow(ctypes.Structure):
        _fields_ = (("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64))

    how = OpenHow(flags=flags, mode=0, resolve=0x02 | 0x04 | 0x08)
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(437, directory_fd, path.encode(), ctypes.byref(how), ctypes.sizeof(how))
    if result >= 0:
        return int(result)
    error = ctypes.get_errno()
    if error == errno.ENOSYS:
        try:
            return os.open(path, flags | os.O_NOFOLLOW, dir_fd=directory_fd)
        except OSError as exc:
            raise SafeGitError(f"{label} is unavailable or unsafe") from exc
    raise SafeGitError(f"{label} is unavailable or unsafe") from OSError(error, os.strerror(error))


def _read_regular_file(directory_fd: int, name: str) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        raise
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_size > _MAX_FILE_BYTES:
            raise SafeGitError(f"Git {name} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > _MAX_FILE_BYTES:
            raise SafeGitError(f"Git {name} is too large")
        return contents
    finally:
        os.close(fd)


def _fd_path(fd: int) -> str:
    if sys.platform.startswith("linux"):
        return f"/proc/self/fd/{fd}"
    if sys.platform == "darwin":
        value = fcntl.fcntl(fd, 50, b"\x00" * 1024)
        return os.fsdecode(value.split(b"\x00", 1)[0])
    return f"/dev/fd/{fd}"
