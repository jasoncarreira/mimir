from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import subprocess
from typing import Callable, Iterable

from ._rmtree import rmtree_missing_ok
from .contained_snapshot import SnapshotResult, create_git_snapshot, preflight_git_snapshot
from .worklink.checkout import (
    CheckoutAuthorization,
    _mint_checkout_authorization,
    _normalize_checkout_fd,
    _open_opencode_checkout,
    _open_repo_test_checkout,
)
from .worklink.identities import get_identities

REPO_TEST_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/repo-test-checkouts")
OPENCODE_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/opencode-checkouts")


@dataclass
class ContainedCheckout:
    path: Path
    authorization: CheckoutAuthorization
    snapshot: SnapshotResult
    base_tree: str | None = None

    @property
    def capability(self) -> CheckoutAuthorization:
        return self.authorization

    def close(self) -> None:
        self.authorization.close()
        _remove_boundary(self.path.parent)

    def __enter__(self) -> ContainedCheckout:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _remove_boundary(boundary: Path) -> None:
    if boundary.exists():
        rmtree_missing_ok(boundary)
    try:
        boundary.parent.rmdir()
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY}:
            raise


def _positive_random() -> int:
    value = 0
    while value == 0:
        value = secrets.randbits(63)
    return value


def _scope_hash(value: str) -> str:
    if not value:
        raise ValueError("checkout scope must not be empty")
    return hashlib.sha256(value.encode()).hexdigest()


def _prepare_boundary(root: Path, scope: str, boundary_name: str) -> tuple[Path, Path]:
    identities = get_identities()
    observed_root = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(observed_root.st_mode):
        raise RuntimeError("contained checkout root is unavailable")
    scope_path = root / scope
    try:
        scope_path.mkdir(mode=0o700)
    except FileExistsError:
        observed_scope = scope_path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(observed_scope.st_mode):
            raise RuntimeError("contained checkout scope is unavailable")
    os.chown(
        scope_path, identities.mimir_uid, identities.mimir_uid, follow_symlinks=False
    )
    os.chmod(scope_path, 0o700, follow_symlinks=False)
    boundary = scope_path / boundary_name
    boundary.mkdir(mode=0o700)
    try:
        os.chown(
            boundary,
            identities.mimir_uid,
            identities.worklink_gid,
            follow_symlinks=False,
        )
        os.chmod(boundary, 0o700, follow_symlinks=False)
    except Exception:
        _remove_boundary(boundary)
        raise
    return boundary, boundary / "checkout"


def _issue_checkout(
    source: Path,
    *,
    root: Path,
    scope: str,
    issue_id: int,
    attempt: int,
    open_checkout: Callable[[Path], int],
    known_sensitive: Iterable[bytes],
    scan_tracked_credentials: bool = True,
    prepare: Callable[[Path], str | None] | None = None,
) -> tuple[SnapshotResult, CheckoutAuthorization, str | None]:
    boundary, destination = _prepare_boundary(root, scope, f"{issue_id}-{attempt}")
    checkout_fd = -1
    try:
        snapshot = create_git_snapshot(
            source,
            destination,
            known_sensitive=known_sensitive,
            scan_tracked_credentials=scan_tracked_credentials,
        )
        prepared = prepare(destination) if prepare is not None else None
        relative = destination.relative_to(root)
        checkout_fd = open_checkout(relative)
        identities = get_identities()
        _normalize_checkout_fd(
            checkout_fd,
            owner_uid=identities.mimir_uid,
            group_gid=identities.worklink_gid,
        )
        authorization = _mint_checkout_authorization(destination, issue_id, attempt, checkout_fd)
        checkout_fd = -1
        return snapshot, authorization, prepared
    except Exception:
        _remove_boundary(boundary)
        raise
    finally:
        if checkout_fd >= 0:
            os.close(checkout_fd)


def create_repo_test_checkout(
    source: str | os.PathLike[str],
    *,
    scope_id: str,
    pr_number: int,
    known_sensitive: Iterable[bytes] = (),
) -> ContainedCheckout:
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("pull request number must be positive")
    source_path = Path(source).resolve(strict=True)
    sensitive = tuple(known_sensitive)
    preflight_git_snapshot(
        source_path,
        known_sensitive=sensitive,
        scan_tracked_credentials=False,
    )
    attempt = _positive_random()
    snapshot, authorization, _base_tree = _issue_checkout(
        source_path,
        root=REPO_TEST_CHECKOUT_ROOT,
        scope=_scope_hash(scope_id),
        issue_id=pr_number,
        attempt=attempt,
        open_checkout=_open_repo_test_checkout,
        known_sensitive=sensitive,
        scan_tracked_credentials=False,
    )
    return ContainedCheckout(snapshot.destination, authorization, snapshot)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("contained checkout Git operation failed")
    return completed.stdout.strip()


def _validate_opencode_seed(seed: Path, default_cwd: Path) -> Path:
    resolved_seed = seed.resolve(strict=True)
    resolved_default = default_cwd.resolve(strict=True)
    try:
        resolved_seed.relative_to(resolved_default)
    except ValueError as exc:
        raise ValueError("OpenCode seed is outside the configured default tree") from exc
    top = Path(_git(resolved_seed, "rev-parse", "--show-toplevel")).resolve(strict=True)
    git_dir = Path(_git(resolved_seed, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    if top != resolved_seed or not git_dir.is_relative_to(resolved_seed):
        raise ValueError("OpenCode seed must be a self-contained Git worktree")
    return resolved_seed


def create_opencode_checkout(
    seed: str | os.PathLike[str],
    *,
    default_cwd: str | os.PathLike[str],
    known_sensitive: Iterable[bytes] = (),
) -> ContainedCheckout:
    source = _validate_opencode_seed(Path(seed), Path(default_cwd))
    sensitive = tuple(known_sensitive)
    preflight_git_snapshot(source, known_sensitive=sensitive)
    issue_id = _positive_random()

    def prepare(destination: Path) -> str:
        _git(destination, "config", "user.name", "Mimir Contained Worker")
        _git(destination, "config", "user.email", "contained@mimir.invalid")
        _git(destination, "add", "-A")
        _git(destination, "commit", "--allow-empty", "-m", "Contained seed")
        return _git(destination, "rev-parse", "HEAD^{tree}")

    snapshot, authorization, base_tree = _issue_checkout(
        source,
        root=OPENCODE_CHECKOUT_ROOT,
        scope=hashlib.sha256(os.fsencode(source)).hexdigest(),
        issue_id=issue_id,
        attempt=1,
        open_checkout=_open_opencode_checkout,
        known_sensitive=sensitive,
        prepare=prepare,
    )
    return ContainedCheckout(snapshot.destination, authorization, snapshot, base_tree)
