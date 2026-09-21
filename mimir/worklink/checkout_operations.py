"""Contained operations for an already-admitted retained checkout."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import tempfile
from typing import Callable, Iterator, Literal, Mapping, Protocol, Sequence
import uuid

from ..repository_config import RepositoryConfig, RepositoryInventory
from ..secret_scan import secret_matches


MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 4 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_LISTED_FILES = 10_000
GIT_TIMEOUT_SECONDS = 60.0
TEST_TIMEOUT_SECONDS = 1800.0
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FIXED_GIT = "/usr/bin/git"


class CheckoutOperationError(RuntimeError):
    """The retained checkout could not safely perform the requested operation."""


class CheckoutConflictError(CheckoutOperationError):
    """The retained identity or operation precondition no longer matches."""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int,
    ) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True)
class CheckoutIdentity:
    path: Path
    device: int
    inode: int
    tree: str
    head: str
    branch: str
    repository_slug: str
    issue_id: int
    attempt: int

    def __post_init__(self) -> None:
        path = Path(self.path)
        if (
            not path.is_absolute()
            or path != Path(os.path.normpath(path))
            or ".." in path.parts
            or type(self.device) is not int
            or self.device < 0
            or type(self.inode) is not int
            or self.inode < 0
            or _OBJECT_ID.fullmatch(self.tree) is None
            or _OBJECT_ID.fullmatch(self.head) is None
            or not self.branch
            or "\x00" in self.branch
            or not self.repository_slug
            or type(self.issue_id) is not int
            or self.issue_id < 1
            or type(self.attempt) is not int
            or self.attempt < 1
        ):
            raise ValueError("retained checkout identity is invalid")
        object.__setattr__(self, "path", path)

    @property
    def digest(self) -> str:
        payload = {
            "attempt": self.attempt,
            "branch": self.branch,
            "device": self.device,
            "head": self.head,
            "inode": self.inode,
            "issue_id": self.issue_id,
            "path": str(self.path),
            "repository_slug": self.repository_slug,
            "tree": self.tree,
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()


@dataclass(frozen=True)
class FileRead:
    relative_path: str
    content: bytes
    sha256: str
    size: int
    offset: int
    complete: bool


@dataclass(frozen=True)
class FileIntent:
    operation_id: str
    action: Literal["write", "delete"]
    relative_path: str
    expected_sha256: str
    resulting_sha256: str | None
    request_digest: str
    checkout_digest: str


@dataclass(frozen=True)
class FileMutationResult:
    intent: FileIntent
    outcome: Literal["applied", "already_applied"]
    sha256: str | None


@dataclass(frozen=True)
class TestResult:
    command: str
    command_digest: str
    head: str
    tree: str
    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class CommitResult:
    commit: str
    tree: str
    message: str
    outcome: Literal["applied", "already_applied"]


class RetainedCheckoutOperations:
    """Operate through one path/device/inode identity captured after admission.

    ``revalidate`` is the recovery service's current-state check.  Leaf callers
    use it to prove the exact process remains held; factory callers use it to
    recheck the retained record and claim.  It is deliberately called around
    every side effect rather than being treated as an admission-time fact.
    """

    def __init__(
        self,
        identity: CheckoutIdentity,
        *,
        revalidate: Callable[[], None],
        runner: CommandRunner | None = None,
    ) -> None:
        if not callable(revalidate):
            raise TypeError("retained checkout operations require a state revalidator")
        self.identity = identity
        self._revalidate = revalidate
        self._runner = runner or _run_bounded

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        expected_device: int,
        expected_inode: int,
        branch: str,
        repository_slug: str,
        issue_id: int,
        attempt: int,
        revalidate: Callable[[], None],
        runner: CommandRunner | None = None,
    ) -> RetainedCheckoutOperations:
        command_runner = runner or _run_bounded
        revalidate()
        fd = _open_absolute_directory(Path(path))
        try:
            observed = os.fstat(fd)
            if (observed.st_dev, observed.st_ino) != (expected_device, expected_inode):
                raise CheckoutConflictError("retained checkout admission identity changed")
        finally:
            os.close(fd)
        def git_text(*args: str) -> str:
            result = command_runner(
                (_FIXED_GIT, *args),
                cwd=Path(path),
                env=_git_environment(None),
                timeout=GIT_TIMEOUT_SECONDS,
                output_limit=MAX_COMMAND_OUTPUT_BYTES,
            )
            if result.returncode != 0:
                raise CheckoutConflictError("retained checkout Git identity is unavailable")
            return _bytes_output(result.stdout).decode("utf-8", errors="strict").strip()

        current_branch = git_text("symbolic-ref", "--quiet", "--short", "HEAD")
        if current_branch != branch:
            raise CheckoutConflictError("retained checkout branch changed")
        head = git_text("rev-parse", "--verify", "HEAD")
        tree = git_text("rev-parse", "--verify", "HEAD^{tree}")
        if _OBJECT_ID.fullmatch(head) is None or _OBJECT_ID.fullmatch(tree) is None:
            raise CheckoutConflictError("retained checkout returned an invalid Git object identity")
        identity = CheckoutIdentity(
            Path(path), expected_device, expected_inode, tree, head, branch,
            repository_slug, issue_id, attempt,
        )
        result = cls(identity, revalidate=revalidate, runner=runner)
        result._verify_checkout()
        return result

    def list_files(self, *, limit: int = MAX_LISTED_FILES) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= MAX_LISTED_FILES:
            raise ValueError(f"file listing limit must be between 1 and {MAX_LISTED_FILES}")
        self._verify_checkout()
        root_fd = self._open_root()
        try:
            files: list[str] = []
            entries: list[tuple[tuple[str, ...], tuple[int, int, int]]] = []
            pending: list[tuple[int, tuple[str, ...]]] = [(os.dup(root_fd), ())]
            try:
                while pending:
                    directory_fd, prefix = pending.pop()
                    try:
                        for name in sorted(os.listdir(directory_fd), reverse=True):
                            if name == ".git":
                                continue
                            value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                            relative = (*prefix, name)
                            if stat.S_ISLNK(value.st_mode):
                                raise CheckoutOperationError(
                                    f"retained checkout path {_display_path(relative)!r} is a symlink"
                                )
                            if stat.S_ISDIR(value.st_mode):
                                child = os.open(
                                    name,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                                    dir_fd=directory_fd,
                                )
                                opened = os.fstat(child)
                                identity = (value.st_dev, value.st_ino, value.st_mode)
                                if (opened.st_dev, opened.st_ino, opened.st_mode) != identity:
                                    os.close(child)
                                    raise CheckoutConflictError(
                                        "retained checkout directory was replaced during listing"
                                    )
                                entries.append((relative, identity))
                                pending.append((child, relative))
                            elif stat.S_ISREG(value.st_mode):
                                entries.append(
                                    (relative, (value.st_dev, value.st_ino, value.st_mode))
                                )
                                files.append(_display_path(relative))
                                if len(files) > limit:
                                    raise CheckoutOperationError("retained checkout file listing exceeds bounds")
                            else:
                                raise CheckoutOperationError(
                                    f"retained checkout path {_display_path(relative)!r} is not a regular file"
                                )
                    finally:
                        os.close(directory_fd)
            finally:
                for directory_fd, _ in pending:
                    os.close(directory_fd)
            for relative, identity in entries:
                _verify_path_identity(root_fd, relative, identity)
            self._verify_named_root(root_fd)
            self._revalidate()
            return tuple(sorted(files))
        finally:
            os.close(root_fd)

    def read_file(self, relative_path: str, *, offset: int = 0, limit: int = MAX_READ_BYTES) -> FileRead:
        parts = _validated_relative_path(relative_path)
        if type(offset) is not int or offset < 0:
            raise ValueError("file read offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_READ_BYTES:
            raise ValueError(f"file read limit must be between 1 and {MAX_READ_BYTES}")
        self._verify_checkout()
        root_fd = self._open_root()
        parent_fd = -1
        file_fd = -1
        try:
            parent_fd, directory_identities = _open_parent(root_fd, parts)
            file_fd = _open_regular(parent_fd, parts[-1])
            value = os.fstat(file_fd)
            target_identity = (value.st_dev, value.st_ino, value.st_mode)
            os.lseek(file_fd, offset, os.SEEK_SET)
            content = os.read(file_fd, limit)
            digest = _sha256_fd(file_fd)
            _verify_parent_walk(root_fd, parts, directory_identities)
            _verify_path_identity(root_fd, parts, target_identity)
            self._verify_named_root(root_fd)
            self._revalidate()
            return FileRead(
                relative_path, content, digest, value.st_size, offset,
                offset + len(content) >= value.st_size,
            )
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(root_fd)

    def prepare_write(
        self,
        operation_id: str,
        relative_path: str,
        content: str,
        *,
        expected_sha256: str,
    ) -> FileIntent:
        _validated_operation_id(operation_id)
        _validated_relative_path(relative_path)
        if not isinstance(content, str):
            raise TypeError("retained checkout writes require UTF-8 text")
        try:
            document = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("retained checkout write content must be valid UTF-8") from exc
        if len(document) > MAX_WRITE_BYTES:
            raise ValueError("retained checkout write exceeds 4 MiB")
        _validated_expected_digest(expected_sha256, allow_absent=True)
        self._verify_checkout()
        observed = self._file_digest(relative_path, missing_ok=True)
        _require_expected_digest(observed, expected_sha256)
        resulting = hashlib.sha256(document).hexdigest()
        return self._intent(
            operation_id, "write", relative_path, expected_sha256, resulting
        )

    def prepare_delete(
        self,
        operation_id: str,
        relative_path: str,
        *,
        expected_sha256: str,
    ) -> FileIntent:
        _validated_operation_id(operation_id)
        _validated_relative_path(relative_path)
        _validated_expected_digest(expected_sha256, allow_absent=False)
        self._verify_checkout()
        observed = self._file_digest(relative_path, missing_ok=False)
        _require_expected_digest(observed, expected_sha256)
        return self._intent(
            operation_id, "delete", relative_path, expected_sha256, None
        )

    def write_file(
        self,
        operation_id: str,
        relative_path: str,
        content: str,
        *,
        expected_sha256: str,
        persist_intent: Callable[[FileIntent], None],
    ) -> FileMutationResult:
        """Persist a write intent before applying its atomic replacement."""
        if not callable(persist_intent):
            raise TypeError("file mutation requires an intent persistence callback")
        intent = self.prepare_write(
            operation_id,
            relative_path,
            content,
            expected_sha256=expected_sha256,
        )
        persist_intent(intent)
        return self.apply_file_intent(intent, content=content)

    def delete_file(
        self,
        operation_id: str,
        relative_path: str,
        *,
        expected_sha256: str,
        persist_intent: Callable[[FileIntent], None],
    ) -> FileMutationResult:
        """Persist a delete intent before applying its atomic unlink."""
        if not callable(persist_intent):
            raise TypeError("file mutation requires an intent persistence callback")
        intent = self.prepare_delete(
            operation_id,
            relative_path,
            expected_sha256=expected_sha256,
        )
        persist_intent(intent)
        return self.apply_file_intent(intent)

    def apply_file_intent(self, intent: FileIntent, *, content: str | None = None) -> FileMutationResult:
        self._validate_intent(intent)
        parts = _validated_relative_path(intent.relative_path)
        document: bytes | None = None
        if intent.action == "write":
            if not isinstance(content, str):
                raise ValueError("write intent replay requires its UTF-8 content")
            try:
                document = content.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("retained checkout write content must be valid UTF-8") from exc
            if len(document) > MAX_WRITE_BYTES:
                raise ValueError("retained checkout write exceeds 4 MiB")
            if hashlib.sha256(document).hexdigest() != intent.resulting_sha256:
                raise CheckoutConflictError("write intent content digest changed")
        elif content is not None:
            raise ValueError("delete intent cannot carry content")

        self._verify_checkout()
        root_fd = self._open_root()
        parent_fd = -1
        try:
            parent_fd, directory_identities = _open_parent(root_fd, parts)
            observed, target_identity = _digest_at(parent_fd, parts[-1], missing_ok=True)

            def verify_named_result(
                expected_target: tuple[int, int, int] | None,
            ) -> None:
                _verify_parent_walk(root_fd, parts, directory_identities)
                self._verify_named_root(root_fd)
                if expected_target is None:
                    _verify_path_absent(root_fd, parts)
                else:
                    _verify_path_identity(root_fd, parts, expected_target)
                self._revalidate()

            if intent.action == "write" and observed == intent.resulting_sha256:
                assert target_identity is not None
                verify_named_result(target_identity)
                return FileMutationResult(intent, "already_applied", observed)
            if intent.action == "delete" and observed is None:
                verify_named_result(None)
                return FileMutationResult(intent, "already_applied", None)
            _require_expected_digest(observed, intent.expected_sha256)

            self._revalidate()
            _verify_parent_walk(root_fd, parts, directory_identities)
            self._verify_named_root(root_fd)
            _verify_target_identity(parent_fd, parts[-1], target_identity)

            def final_pre_effect_check() -> None:
                _verify_parent_walk(root_fd, parts, directory_identities)
                self._verify_named_root(root_fd)
                _verify_target_identity(parent_fd, parts[-1], target_identity)
                self._revalidate()

            final_pre_effect_check()
            if intent.action == "write":
                assert document is not None
                applied_identity = _atomic_replace(
                    parent_fd,
                    parts[-1],
                    document,
                    target_identity,
                    pre_effect=final_pre_effect_check,
                )
                resulting, resulting_identity = _digest_at(
                    parent_fd, parts[-1], missing_ok=False
                )
                if resulting_identity != applied_identity:
                    raise CheckoutConflictError(
                        "retained checkout file was replaced after write"
                    )
            else:
                os.unlink(parts[-1], dir_fd=parent_fd)
                os.fsync(parent_fd)
                resulting = None
                resulting_identity = None
            if resulting != intent.resulting_sha256:
                raise CheckoutConflictError("file mutation result digest changed")
            verify_named_result(resulting_identity)
            return FileMutationResult(intent, "applied", resulting)
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(root_fd)

    def candidate_tree(self) -> str:
        self._verify_checkout()
        with self._candidate_index() as (_, tree):
            self._verify_checkout()
            return tree

    def run_tests(self, inventory: RepositoryInventory) -> TestResult:
        repository = self._authorized_repository(inventory)
        command = repository.test_command
        if command is None or not command.strip():
            raise CheckoutOperationError("coding-target repository has no test command")
        self._verify_checkout()
        head = self._head()
        before = self.candidate_tree()
        with _test_environment() as environment:
            self._revalidate()
            result = self._runner(
                ("/bin/sh", "-c", command),
                cwd=self.identity.path,
                env=environment,
                timeout=TEST_TIMEOUT_SECONDS,
                output_limit=MAX_COMMAND_OUTPUT_BYTES,
            )
        self._verify_checkout()
        after = self.candidate_tree()
        self._revalidate()
        if after != before:
            raise CheckoutConflictError("retained checkout tree changed while tests ran")
        return TestResult(
            command=command,
            command_digest=hashlib.sha256(command.encode()).hexdigest(),
            head=head,
            tree=before,
            exit_code=result.returncode,
            stdout=_bytes_output(result.stdout),
            stderr=_bytes_output(result.stderr),
        )

    def commit_tested_tree(self, tests: TestResult) -> CommitResult:
        if not isinstance(tests, TestResult) or not tests.passed:
            raise CheckoutOperationError("only a passing test result may be committed")
        message = f"worklink: issue #{self.identity.issue_id}"
        self._verify_checkout(allow_applied_commit=(tests, message))
        current_head = self._head()
        if current_head != tests.head:
            if self._commit_matches(current_head, tests, message):
                self._synchronize_index()
                return CommitResult(current_head, tests.tree, message, "already_applied")
            raise CheckoutConflictError("retained checkout HEAD changed after tests")
        if self.candidate_tree() != tests.tree:
            raise CheckoutConflictError("retained checkout tree differs from the tested tree")

        with self._candidate_index() as (index_path, tree):
            if tree != tests.tree:
                raise CheckoutConflictError("staged candidate differs from the tested tree")
            self._scan_index_for_secrets(tests.head, index_path)
            self._revalidate()
            self._verify_checkout()
            self._revalidate()
            result = self._git(
                "-c", "commit.gpgSign=false", "commit", "-m", message,
                index_path=index_path,
            )
            if result.returncode != 0:
                raise CheckoutOperationError("could not commit retained checkout changes")
        commit = self._head()
        if not self._commit_matches(commit, tests, message):
            raise CheckoutConflictError("retained checkout commit result is ambiguous")
        self._synchronize_index()
        self._revalidate()
        return CommitResult(commit, tests.tree, message, "applied")

    def _authorized_repository(self, inventory: RepositoryInventory) -> RepositoryConfig:
        if not isinstance(inventory, RepositoryInventory):
            raise TypeError("tests require the server repository inventory")
        matches = [
            item for item in inventory.repositories
            if item.slug == self.identity.repository_slug
        ]
        if len(matches) != 1 or matches[0].mode != "rw":
            raise CheckoutOperationError("retained checkout repository is not one authorized rw target")
        return matches[0]

    def _intent(
        self,
        operation_id: str,
        action: Literal["write", "delete"],
        relative_path: str,
        expected_sha256: str,
        resulting_sha256: str | None,
    ) -> FileIntent:
        request = {
            "action": action,
            "checkout_digest": self.identity.digest,
            "expected_sha256": expected_sha256,
            "operation_id": operation_id,
            "relative_path": relative_path,
            "resulting_sha256": resulting_sha256,
        }
        digest = hashlib.sha256(
            json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        return FileIntent(
            operation_id, action, relative_path, expected_sha256,
            resulting_sha256, digest, self.identity.digest,
        )

    def _validate_intent(self, intent: FileIntent) -> None:
        if not isinstance(intent, FileIntent) or intent.checkout_digest != self.identity.digest:
            raise CheckoutConflictError("file intent belongs to another retained checkout")
        _validated_operation_id(intent.operation_id)
        _validated_relative_path(intent.relative_path)
        if intent.action not in {"write", "delete"}:
            raise CheckoutConflictError("file intent action is invalid")
        _validated_expected_digest(
            intent.expected_sha256,
            allow_absent=intent.action == "write",
        )
        if (
            (intent.action == "write" and (
                not isinstance(intent.resulting_sha256, str)
                or _SHA256.fullmatch(intent.resulting_sha256) is None
            ))
            or (intent.action == "delete" and intent.resulting_sha256 is not None)
        ):
            raise CheckoutConflictError("file intent result digest is invalid")
        expected = self._intent(
            intent.operation_id,
            intent.action,
            intent.relative_path,
            intent.expected_sha256,
            intent.resulting_sha256,
        )
        if expected != intent:
            raise CheckoutConflictError("file intent request digest changed")

    def _file_digest(self, relative_path: str, *, missing_ok: bool) -> str | None:
        parts = _validated_relative_path(relative_path)
        root_fd = self._open_root()
        parent_fd = -1
        try:
            parent_fd, directory_identities = _open_parent(root_fd, parts)
            digest, target_identity = _digest_at(
                parent_fd, parts[-1], missing_ok=missing_ok
            )
            _verify_parent_walk(root_fd, parts, directory_identities)
            if target_identity is not None:
                _verify_path_identity(root_fd, parts, target_identity)
            self._verify_named_root(root_fd)
            return digest
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(root_fd)

    def _open_root(self) -> int:
        fd = _open_absolute_directory(self.identity.path)
        value = os.fstat(fd)
        if (value.st_dev, value.st_ino) != (self.identity.device, self.identity.inode):
            os.close(fd)
            raise CheckoutConflictError("retained checkout identity was replaced")
        return fd

    def _verify_named_root(self, held_fd: int) -> None:
        current_fd = self._open_root()
        try:
            current = os.fstat(current_fd)
            held = os.fstat(held_fd)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise CheckoutConflictError("retained checkout was replaced during operation")
        finally:
            os.close(current_fd)

    def _verify_checkout(
        self,
        *,
        allow_applied_commit: tuple[TestResult, str] | None = None,
    ) -> None:
        self._revalidate()
        fd = self._open_root()
        os.close(fd)
        branch = self._git_text("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != self.identity.branch:
            raise CheckoutConflictError("retained checkout branch changed")
        head = self._head()
        if head != self.identity.head:
            if allow_applied_commit is None or not self._commit_matches(
                head, allow_applied_commit[0], allow_applied_commit[1]
            ):
                raise CheckoutConflictError("retained checkout HEAD changed")
        elif self._git_object("rev-parse", "--verify", "HEAD^{tree}") != self.identity.tree:
            raise CheckoutConflictError("retained checkout HEAD tree changed")

    def _head(self) -> str:
        return self._git_object("rev-parse", "--verify", "HEAD")

    @contextlib.contextmanager
    def _candidate_index(self) -> Iterator[tuple[Path, str]]:
        descriptor, raw_path = tempfile.mkstemp(prefix="worklink-index-")
        os.close(descriptor)
        index_path = Path(raw_path)
        index_path.unlink()
        try:
            read = self._git("read-tree", self._head(), index_path=index_path)
            if read.returncode != 0:
                raise CheckoutOperationError("could not initialize retained checkout candidate index")
            add = self._git("add", "-A", index_path=index_path)
            if add.returncode != 0:
                raise CheckoutOperationError("could not stage retained checkout candidate tree")
            tree = self._git_object("write-tree", index_path=index_path)
            yield index_path, tree
        finally:
            index_path.unlink(missing_ok=True)

    def _scan_index_for_secrets(self, base: str, index_path: Path) -> None:
        listed = self._git(
            "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB",
            index_path=index_path,
        )
        if listed.returncode != 0:
            raise CheckoutOperationError("cannot scan staged retained checkout changes for secrets")
        for raw_path in _bytes_output(listed.stdout).split(b"\0"):
            if not raw_path:
                continue
            path = os.fsdecode(raw_path)
            staged = self._git(
                "cat-file",
                "blob",
                f":{path}",
                index_path=index_path,
                output_limit=MAX_WRITE_BYTES,
            )
            if staged.returncode != 0:
                raise CheckoutOperationError(
                    f"cannot scan staged retained checkout path {path!r} for secrets"
                )
            staged_matches = secret_matches(
                _bytes_output(staged.stdout).decode("utf-8", errors="surrogateescape")
            )
            if not staged_matches:
                continue
            previous = self._git(
                "cat-file",
                "blob",
                f"{base}:{path}",
                index_path=index_path,
                output_limit=MAX_WRITE_BYTES,
            )
            if previous.returncode == 0:
                base_matches = secret_matches(
                    _bytes_output(previous.stdout).decode("utf-8", errors="surrogateescape")
                )
            else:
                exists = self._git("ls-tree", "-z", base, "--", path, index_path=index_path)
                if exists.returncode != 0 or _bytes_output(exists.stdout):
                    raise CheckoutOperationError(
                        f"cannot scan base retained checkout path {path!r} for secrets"
                    )
                base_matches = set()
            if staged_matches - base_matches:
                raise CheckoutOperationError(
                    f"staged retained checkout path {path!r} contains a secret-shaped token; "
                    "refusing to commit"
                )

    def _commit_matches(self, commit: str, tests: TestResult, message: str) -> bool:
        if _OBJECT_ID.fullmatch(commit) is None:
            return False
        metadata = self._git_text(
            "show", "-s", "--format=%P%x00%T%x00%B", commit,
            check=False,
        )
        if not metadata:
            return False
        fields = metadata.split("\x00", 2)
        return (
            len(fields) == 3
            and fields[0] == tests.head
            and fields[1] == tests.tree
            and fields[2].strip() == message
        )

    def _synchronize_index(self) -> None:
        result = self._git("reset", "--mixed", "-q", "HEAD")
        if result.returncode != 0:
            raise CheckoutOperationError("could not synchronize retained checkout index")

    def _git(
        self,
        *args: str,
        index_path: Path | None = None,
        output_limit: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = _git_environment(index_path)
        return self._runner(
            (_FIXED_GIT, *args),
            cwd=self.identity.path,
            env=environment,
            timeout=GIT_TIMEOUT_SECONDS,
            output_limit=output_limit,
        )

    def _git_text(
        self,
        *args: str,
        check: bool = True,
        index_path: Path | None = None,
    ) -> str:
        result = self._git(*args, index_path=index_path)
        if result.returncode != 0:
            if check:
                raise CheckoutConflictError("retained checkout Git identity is unavailable")
            return ""
        return _bytes_output(result.stdout).decode("utf-8", errors="strict").strip()

    def _git_object(self, *args: str, index_path: Path | None = None) -> str:
        value = self._git_text(*args, index_path=index_path)
        if _OBJECT_ID.fullmatch(value) is None:
            raise CheckoutConflictError("retained checkout returned an invalid Git object identity")
        return value


def _validated_relative_path(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("retained checkout path must be a normalized relative POSIX path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or str(relative) != value
        or any(part in {"", ".", "..", ".git"} for part in relative.parts)
    ):
        raise ValueError("retained checkout path must be normalized and cannot address .git")
    return relative.parts


def _validated_operation_id(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("file operation id must be a canonical UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("file operation id must be a canonical UUIDv4")


def _validated_expected_digest(value: str, *, allow_absent: bool) -> None:
    if _SHA256.fullmatch(value) is None and not (allow_absent and value == "absent"):
        expected = "a SHA-256 digest or 'absent'" if allow_absent else "a SHA-256 digest"
        raise ValueError(f"file precondition must be {expected}")


def _require_expected_digest(observed: str | None, expected: str) -> None:
    normalized = "absent" if observed is None else observed
    if normalized != expected:
        raise CheckoutConflictError("retained checkout file digest precondition failed")


def _display_path(parts: Sequence[str]) -> str:
    return "/".join(parts)


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise CheckoutConflictError("retained checkout path is not canonical")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_parent(
    root_fd: int, parts: Sequence[str]
) -> tuple[int, tuple[tuple[int, int], ...]]:
    current = os.dup(root_fd)
    identities: list[tuple[int, int]] = []
    try:
        value = os.fstat(current)
        identities.append((value.st_dev, value.st_ino))
        for part in parts[:-1]:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = following
            value = os.fstat(current)
            identities.append((value.st_dev, value.st_ino))
        return current, tuple(identities)
    except BaseException:
        os.close(current)
        raise


def _verify_parent_walk(
    root_fd: int,
    parts: Sequence[str],
    expected: Sequence[tuple[int, int]],
) -> None:
    try:
        reopened, observed = _open_parent(root_fd, parts)
    except OSError as exc:
        raise CheckoutConflictError(
            "retained checkout path was replaced during traversal"
        ) from exc
    os.close(reopened)
    if tuple(observed) != tuple(expected):
        raise CheckoutConflictError("retained checkout path was replaced during traversal")


def _verify_path_identity(
    root_fd: int,
    parts: Sequence[str],
    expected: tuple[int, int, int],
) -> None:
    parent_fd = -1
    try:
        parent_fd, _ = _open_parent(root_fd, parts)
        value = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise CheckoutConflictError(
            "retained checkout path was replaced during operation"
        ) from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    if stat.S_ISLNK(value.st_mode) or (value.st_dev, value.st_ino, value.st_mode) != expected:
        raise CheckoutConflictError("retained checkout path was replaced during operation")


def _verify_path_absent(root_fd: int, parts: Sequence[str]) -> None:
    try:
        parent_fd, _ = _open_parent(root_fd, parts)
    except OSError as exc:
        raise CheckoutConflictError(
            "retained checkout path was replaced during operation"
        ) from exc
    try:
        os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CheckoutConflictError(
            "retained checkout path was replaced during operation"
        ) from exc
    finally:
        os.close(parent_fd)
    raise CheckoutConflictError("retained checkout path is no longer absent")


def _open_regular(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
    value = os.fstat(fd)
    if not stat.S_ISREG(value.st_mode):
        os.close(fd)
        raise CheckoutOperationError("retained checkout path is not a regular file")
    return fd


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 64 * 1024)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def _digest_at(
    parent_fd: int, name: str, *, missing_ok: bool
) -> tuple[str | None, tuple[int, int, int] | None]:
    try:
        descriptor = _open_regular(parent_fd, name)
    except FileNotFoundError:
        if missing_ok:
            return None, None
        raise CheckoutConflictError("retained checkout file is absent") from None
    try:
        value = os.fstat(descriptor)
        return _sha256_fd(descriptor), (value.st_dev, value.st_ino, value.st_mode)
    finally:
        os.close(descriptor)


def _verify_target_identity(
    parent_fd: int, name: str, expected: tuple[int, int, int] | None
) -> None:
    try:
        value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if expected is None:
            return
        raise CheckoutConflictError("retained checkout file was replaced") from None
    if expected is None or stat.S_ISLNK(value.st_mode):
        raise CheckoutConflictError("retained checkout file was replaced")
    if (value.st_dev, value.st_ino, value.st_mode) != expected:
        raise CheckoutConflictError("retained checkout file was replaced")


def _atomic_replace(
    parent_fd: int,
    name: str,
    content: bytes,
    previous: tuple[int, int, int] | None,
    *,
    pre_effect: Callable[[], None],
) -> tuple[int, int, int]:
    temporary = f".worklink-{uuid.uuid4()}"
    mode = stat.S_IMODE(previous[2]) if previous is not None else 0o644
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        value = os.fstat(descriptor)
        resulting_identity = (value.st_dev, value.st_ino, value.st_mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=parent_fd)
        raise
    finally:
        os.close(descriptor)
    try:
        _verify_target_identity(parent_fd, name, previous)
        pre_effect()
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=parent_fd)
        raise
    return resulting_identity


def _git_environment(index_path: Path | None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("GIT_", "SSH_"))
        and key.upper() not in {"PAGER", "EDITOR", "VISUAL"}
    }
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    })
    if index_path is not None:
        environment["GIT_INDEX_FILE"] = str(index_path)
    return environment


@contextlib.contextmanager
def _test_environment() -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="worklink-retained-test-") as raw_home:
        home = Path(raw_home)
        paths = {
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_DATA_HOME": home / ".local" / "share",
            "XDG_CACHE_HOME": home / ".cache",
        }
        for path in paths.values():
            path.mkdir(parents=True, mode=0o700)
        yield {
            "USER": "worklink",
            "LOGNAME": "worklink",
            "SHELL": "/bin/sh",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(home),
            **{name: str(path) for name, path in paths.items()},
        }


def _run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    output_limit: int,
) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise CheckoutOperationError("retained checkout command timed out") from None
        stdout.seek(0)
        stderr.seek(0)
        captured_stdout = stdout.read(output_limit + 1)
        captured_stderr = stderr.read(output_limit + 1)
        if len(captured_stdout) > output_limit or len(captured_stderr) > output_limit:
            raise CheckoutOperationError("retained checkout command output exceeds bounds")
        return subprocess.CompletedProcess(list(argv), returncode, captured_stdout, captured_stderr)


def _bytes_output(value: bytes | str | None) -> bytes:
    if isinstance(value, bytes):
        return value
    if value is None:
        return b""
    raise CheckoutOperationError("retained checkout command did not return byte output")
