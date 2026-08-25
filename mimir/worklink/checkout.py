"""Per-issue checkout lifecycle for Worklink."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, Sequence

from .._rmtree import rmtree_missing_ok
from ..coding import coding_enabled
from .identities import get_identities

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
EventLogger = Callable[..., None]

_ENABLED_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/checkouts")
_REPO_TEST_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/repo-test-checkouts")
_OPENCODE_CHECKOUT_ROOT = Path("/var/lib/mimir-worklink/opencode-checkouts")
_AUTHORIZATION_FACTORY = object()


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class CheckoutLease:
    issue_id: int
    attempt: int
    repo: Path
    path: Path
    branch: str
    base_ref: str
    # ``base_ref`` is the operator-facing base name (PR target, worker fetch).
    # ``local_base`` is the locally-resolvable start point / diff floor it
    # resolved to (a local branch, ``origin/<base>``, a SHA, or the name as-is) —
    # see ``_resolve_local_base`` and ``create_isolated_checkout``.
    local_base: str = ""
    # Codex can resolve linked git worktrees back to the parent checkout because
    # their .git file points at ``<repo>/.git/worktrees/...``. Isolated checkouts
    # have their own .git directory and are removed with ``shutil.rmtree`` rather
    # than ``git worktree remove``.
    isolated_checkout: bool = False
    worker_authorized: bool = False
    authorization: Any | None = None


def create_worktree(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str = "main",
    worklink_dir: str = ".worklink",
    base_fetch: bool = True,
    event_logger: EventLogger | None = None,
    runner: Runner = _default_runner,
) -> CheckoutLease:
    """Create an attempt-scoped branch/worktree from a fresh base ref."""
    path = repo / worklink_dir / f"{issue_id}-{attempt}"
    branch = f"issue/{issue_id}-a{attempt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    start_point = _prepare_fresh_base(
        repo,
        base,
        base_fetch=base_fetch,
        runner=runner,
        event_logger=event_logger,
    )
    # ``--no-track`` + an explicit, locally-resolvable start point: without them
    # ``git worktree add -b <branch> <path> <base>`` DWIMs a remote-only base
    # name (e.g. a slash-named feature branch that exists only as
    # ``origin/<base>``) into a tracking checkout — silently ignoring ``-b`` and
    # leaving the worktree on the base branch instead of the attempt branch.
    result = runner(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "--no-track",
            "-b",
            branch,
            str(path),
            start_point,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git worktree add failed")
    return CheckoutLease(
        issue_id=issue_id,
        attempt=attempt,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=base,
        local_base=start_point,
    )


#: git's wording when ``clone --local`` cannot hardlink an object. Covers both
#: hardlink failure modes: EPERM, when an object is owned by another uid and is
#: not writable under ``fs.protected_hardlinks=1``, and EXDEV, when source and
#: destination sit on different filesystems.
_HARDLINK_FAILURE_MARKER = "failed to create link"


def _clone_attempt_checkout(
    repo: Path,
    path: Path,
    *,
    runner: Runner,
    event_logger: EventLogger | None,
    no_hardlinks: bool = False,
) -> None:
    """Clone ``repo`` into ``path``, degrading to an object copy if it must.

    ``--local`` hardlinks the object store and so costs essentially no disk
    (measured: ~0 MB against 179 MB for the same clone with ``--no-hardlinks``).
    It fails outright when even one object cannot be linked, which is not
    hypothetical: a root-owned mode-444 object in a repo checked out by another
    user is enough, and any process writing objects as a different uid creates
    one. Every Worklink build on 2026-07-28 died one second after claiming on
    exactly that, taking three attempts each off two leaves.

    The retry is keyed on git's own error rather than a pre-flight scan of the
    object store. A scan would have to guess which uid performs the clone --
    the caller and the cloning subprocess need not share one -- and would be
    wrong whenever that guess is wrong. Reacting to the failure cannot be wrong
    about it, needs no stat of ~5k objects, and pays the copy only in the broken
    case. An unrelated clone failure is re-raised untouched, so this never
    silently converts a real error into a slow success.
    """
    clone_args = ["git", "clone", "--local"]
    if no_hardlinks:
        clone_args.append("--no-hardlinks")
    clone_args.extend(["--quiet", str(repo), str(path)])
    clone = runner(clone_args)
    if clone.returncode == 0:
        return
    detail = (clone.stderr or clone.stdout).strip()
    if no_hardlinks or _HARDLINK_FAILURE_MARKER not in detail:
        raise RuntimeError(detail or "git clone failed")

    # The failed clone leaves a partial directory; --no-hardlinks needs a clean
    # target, and create_isolated_checkout already refused a pre-existing path.
    shutil.rmtree(path, ignore_errors=True)
    if event_logger is not None:
        event_logger(
            "worklink_checkout_hardlink_fallback",
            repo=str(repo),
            path=str(path),
            # Named rather than swallowed: a fallback that stays invisible turns
            # a fixable ownership problem into a permanent disk cost nobody
            # investigates.
            detail=detail[:300],
        )
    copied = runner(
        ["git", "clone", "--local", "--no-hardlinks", "--quiet", str(repo), str(path)],
    )
    if copied.returncode != 0:
        raise RuntimeError(
            (copied.stderr or copied.stdout).strip() or "git clone failed",
        )


class CheckoutAuthorization:
    __slots__ = ("path", "issue_id", "attempt", "device", "inode", "_fd")

    def __init__(
        self,
        path: Path,
        issue_id: int,
        attempt: int,
        fd: int,
        *,
        _factory: object | None = None,
    ) -> None:
        if _factory is not _AUTHORIZATION_FACTORY:
            raise TypeError("checkout authorizations are issued by the checkout factory")
        observed = os.fstat(fd)
        if not stat.S_ISDIR(observed.st_mode):
            raise RuntimeError("authorized checkout is not a directory")
        self.path = Path(os.path.abspath(path))
        self.issue_id = issue_id
        self.attempt = attempt
        self.device = observed.st_dev
        self.inode = observed.st_ino
        self._fd = fd

    def verify(self, local_checkout: Path | None) -> None:
        if self._fd < 0:
            raise ValueError("authorized checkout is closed")
        if local_checkout is None or Path(os.path.abspath(local_checkout)) != self.path:
            raise ValueError("work spec checkout does not match authorized checkout")
        observed = os.stat(local_checkout, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
            self.device,
            self.inode,
        ):
            raise ValueError("work spec checkout does not match authorized checkout")

    def duplicate_fd(self) -> int:
        if self._fd < 0:
            raise ValueError("authorized checkout is closed")
        return os.dup(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> CheckoutAuthorization:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _mint_checkout_authorization(
    path: Path, issue_id: int, attempt: int, fd: int
) -> CheckoutAuthorization:
    return CheckoutAuthorization(
        path, issue_id, attempt, fd, _factory=_AUTHORIZATION_FACTORY
    )


def _open_issued_checkout(root: Path, relative_path: Path) -> int:
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(component in {".", ".."} for component in relative_path.parts)
    ):
        raise ValueError("issued checkout path must remain beneath its trusted root")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    try:
        if sys.platform.startswith("linux"):
            class OpenHow(ctypes.Structure):
                _fields_ = (
                    ("flags", ctypes.c_uint64),
                    ("mode", ctypes.c_uint64),
                    ("resolve", ctypes.c_uint64),
                )

            how = OpenHow(flags=flags, mode=0, resolve=0x02 | 0x04 | 0x08)
            libc = ctypes.CDLL(None, use_errno=True)
            result = libc.syscall(
                437,
                root_fd,
                os.fsencode(relative_path),
                ctypes.byref(how),
                ctypes.sizeof(how),
            )
            if result >= 0:
                return int(result)
            error = ctypes.get_errno()
            raise RuntimeError("issued checkout is unavailable or unsafe") from OSError(
                error, os.strerror(error)
            )
        current_fd = os.dup(root_fd)
        try:
            for component in relative_path.parts:
                next_fd = os.open(
                    component,
                    flags | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            retained_fd = current_fd
            current_fd = -1
            return retained_fd
        finally:
            if current_fd >= 0:
                os.close(current_fd)
    finally:
        os.close(root_fd)


def _open_worklink_checkout(relative_path: Path) -> int:
    return _open_issued_checkout(_ENABLED_CHECKOUT_ROOT, relative_path)


def _open_repo_test_checkout(relative_path: Path) -> int:
    return _open_issued_checkout(_REPO_TEST_CHECKOUT_ROOT, relative_path)


def _open_opencode_checkout(relative_path: Path) -> int:
    return _open_issued_checkout(_OPENCODE_CHECKOUT_ROOT, relative_path)


def _preflight_directory_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(value.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                observed = os.fstat(child_fd)
                if (observed.st_dev, observed.st_ino) != (value.st_dev, value.st_ino):
                    raise RuntimeError("checkout entry changed during normalization")
                _preflight_directory_fd(child_fd)
            finally:
                os.close(child_fd)
        elif not (stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)):
            raise RuntimeError(f"special checkout entry refused: {name}")


def _copy_unlinked_file(directory_fd: int, name: str, value: os.stat_result) -> None:
    source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    temporary = f".mimir-normalize-{os.urandom(12).hex()}"
    target_fd = -1
    try:
        observed = os.fstat(source_fd)
        if not stat.S_ISREG(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
            value.st_dev, value.st_ino
        ):
            raise RuntimeError("checkout entry changed during normalization")
        target_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        while data := os.read(source_fd, 1024 * 1024):
            view = memoryview(data)
            while view:
                view = view[os.write(target_fd, view):]
        os.fsync(target_fd)
        os.close(target_fd)
        target_fd = -1
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _normalize_directory_fd(directory_fd: int, owner_uid: int, group_gid: int) -> None:
    entries = os.listdir(directory_fd)
    for name in entries:
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(value.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                observed = os.fstat(child_fd)
                if (observed.st_dev, observed.st_ino) != (value.st_dev, value.st_ino):
                    raise RuntimeError("checkout entry changed during normalization")
                _normalize_directory_fd(child_fd, owner_uid, group_gid)
                os.fchown(child_fd, owner_uid, group_gid)
                os.fchmod(child_fd, 0o2770)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(value.st_mode):
            execute_bits = 0o110 if value.st_mode & 0o111 else 0
            if value.st_nlink > 1:
                _copy_unlinked_file(directory_fd, name, value)
                value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            entry_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                observed = os.fstat(entry_fd)
                if not stat.S_ISREG(observed.st_mode) or (
                    observed.st_dev, observed.st_ino
                ) != (value.st_dev, value.st_ino):
                    raise RuntimeError("checkout entry changed during normalization")
                os.fchown(entry_fd, owner_uid, group_gid)
                os.fchmod(entry_fd, 0o660 | execute_bits)
            finally:
                os.close(entry_fd)
        elif stat.S_ISLNK(value.st_mode):
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISLNK(observed.st_mode) or (
                observed.st_dev, observed.st_ino
            ) != (value.st_dev, value.st_ino):
                raise RuntimeError("checkout entry changed during normalization")
            os.chown(name, owner_uid, group_gid, dir_fd=directory_fd, follow_symlinks=False)
        else:
            raise RuntimeError(f"special checkout entry refused: {name}")


def _normalize_checkout_fd(
    checkout_fd: int, *, owner_uid: int | None = None, group_gid: int | None = None
) -> None:
    if owner_uid is None or group_gid is None:
        identities = get_identities()
        if owner_uid is None:
            owner_uid = identities.mimir_uid
        if group_gid is None:
            group_gid = identities.worklink_gid
    root = os.fstat(checkout_fd)
    if not stat.S_ISDIR(root.st_mode):
        raise RuntimeError("authorized checkout is not a directory")
    _preflight_directory_fd(checkout_fd)
    _normalize_directory_fd(checkout_fd, owner_uid, group_gid)
    os.fchown(checkout_fd, owner_uid, group_gid)
    os.fchmod(checkout_fd, 0o2770)


def normalize_checkout(
    authorization: Any,
    *,
    safe_git: Any,
    owner_uid: int | None = None,
    group_gid: int | None = None,
) -> None:
    authorization.verify(authorization.path)
    safe_git.run("rev-parse", "--git-dir", check=True)
    checkout_fd = authorization.duplicate_fd()
    try:
        _normalize_checkout_fd(checkout_fd, owner_uid=owner_uid, group_gid=group_gid)
    finally:
        os.close(checkout_fd)



def create_isolated_checkout(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str = "main",
    worklink_dir: str = ".worklink",
    base_fetch: bool = True,
    event_logger: EventLogger | None = None,
    runner: Runner = _default_runner,
    worker_eligible: bool = False,
) -> CheckoutLease:
    """Create an attempt-scoped local clone with its own ``.git`` directory.

    Some coding CLIs inspect git metadata instead of honoring their process cwd.
    A normal ``git worktree`` stores a ``.git`` file that points back into the
    parent checkout's common git dir. Codex has treated that parent as the active
    repository, and OpenCode has exposed sibling worktrees through the resulting
    shared project identity. This checkout shape keeps the
    same branch/diff contract while giving the backend a real repository rooted
    at the attempt path. ``git clone --local`` uses self-contained hardlinks, not
    alternates; the post-clone assertion enforces that no factory checkout can
    depend on an object directory under the scratch janitor's swept roots.
    """

    enabled = coding_enabled() and worker_eligible
    identities = get_identities() if enabled else None
    path = _isolated_checkout_path(
        repo, worklink_dir, issue_id, attempt, worker_authorized=enabled
    )
    branch = f"issue/{issue_id}-a{attempt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if identities is not None:
        repo_parent = path.parent.parent
        os.chown(repo_parent, identities.mimir_uid, identities.worklink_gid)
        os.chmod(repo_parent, 0o710)
        os.chown(path.parent, identities.mimir_uid, identities.worklink_gid)
        os.chmod(path.parent, 0o700)
    if path.exists():
        raise RuntimeError(f"attempt checkout already exists: {path}")

    start_point = _prepare_fresh_base(
        repo,
        base,
        base_fetch=base_fetch,
        runner=runner,
        event_logger=event_logger,
    )
    start_sha = runner(["git", "-C", str(repo), "rev-parse", "--verify", start_point])
    if start_sha.returncode != 0:
        raise RuntimeError((start_sha.stderr or start_sha.stdout).strip() or "git rev-parse failed")
    local_base = start_sha.stdout.strip()

    parent_push = runner(["git", "-C", str(repo), "remote", "get-url", "--push", "origin"])
    if parent_push.returncode != 0 or not parent_push.stdout.strip():
        raise RuntimeError(
            (parent_push.stderr or parent_push.stdout).strip()
            or "git remote get-url --push origin failed"
        )
    wanted_push_target = parent_push.stdout.strip()

    previous_umask = os.umask(0o007) if enabled else None
    try:
        _clone_attempt_checkout(
            repo, path, runner=runner, event_logger=event_logger, no_hardlinks=enabled
        )
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)

    try:
        set_remote = runner(
            ["git", "-C", str(path), "remote", "set-url", "origin", wanted_push_target]
        )
        if set_remote.returncode != 0:
            raise RuntimeError((set_remote.stderr or set_remote.stdout).strip() or "git remote set-url failed")

        checkout_push = runner(
            ["git", "-C", str(path), "remote", "get-url", "--push", "origin"]
        )
        if checkout_push.returncode != 0:
            raise RuntimeError(
                (checkout_push.stderr or checkout_push.stdout).strip()
                or "isolated checkout push-target verification failed"
            )
        observed_push_target = checkout_push.stdout.strip()
        if observed_push_target != wanted_push_target:
            raise RuntimeError(
                "isolated checkout push-target mismatch: "
                f"wanted {wanted_push_target!r}, observed {observed_push_target!r}"
            )
    except RuntimeError:
        shutil.rmtree(path, ignore_errors=True)
        raise

    checkout = runner(["git", "-C", str(path), "checkout", "-B", branch, local_base])
    if checkout.returncode != 0:
        raise RuntimeError((checkout.stderr or checkout.stdout).strip() or "git checkout failed")

    # #517: verify the clone is a real, self-contained repo rooted at ``path`` and
    # does not resolve back to the parent before any backend inspects its git
    # metadata. Fail loud (and clean up the half-made checkout) rather than handing
    # codex a checkout that would walk up into the repo root.
    authorization = None
    try:
        _assert_self_contained_checkout(path, runner=runner)
        if enabled:
            relative_path = path.relative_to(_ENABLED_CHECKOUT_ROOT)
            checkout_fd = _open_worklink_checkout(relative_path)
            try:
                assert identities is not None
                _normalize_checkout_fd(
                    checkout_fd,
                    owner_uid=identities.mimir_uid,
                    group_gid=identities.worklink_gid,
                )
                authorization = _mint_checkout_authorization(path, issue_id, attempt, checkout_fd)
                checkout_fd = -1
            finally:
                if checkout_fd >= 0:
                    os.close(checkout_fd)
    except (OSError, RuntimeError):
        shutil.rmtree(path, ignore_errors=True)
        raise

    return CheckoutLease(
        issue_id=issue_id,
        attempt=attempt,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=base,
        local_base=local_base,
        isolated_checkout=True,
        worker_authorized=enabled,
        authorization=authorization,
    )


def _fetch_base_from_origin(
    repo: Path,
    base: str,
    *,
    runner: Runner,
    event_logger: EventLogger | None = None,
) -> bool:
    """Refresh the base, repairing reconstructible dangling refs once on failure."""
    remote_base = base.removeprefix("origin/")
    result = runner(["git", "-C", str(repo), "fetch", "origin", remote_base])
    # A reclaimed alternate leaves refs behind. Pruning the alternates file removes
    # the pointer but not the refs that resolved only through it, and git refuses
    # the whole fetch on the first such ref ("fatal: bad object <ref>"). Repair that
    # residue once too, otherwise a single stale remote-tracking ref permanently
    # fails every base fetch — which consumed six worklink attempts across three
    # leaves before it was diagnosed by hand.
    if result.returncode != 0:
        repaired_refs, retained_refs = _prune_dangling_refs(repo, runner=runner)
        if repaired_refs or retained_refs:
            if event_logger is not None:
                event_logger(
                    "worklink_base_refs_repaired",
                    repo=str(repo),
                    base=remote_base,
                    pruned=[f"{name}@{sha}" for name, sha in repaired_refs],
                    # Named, not silently skipped: a dangling refs/heads or
                    # refs/tags keeps the fetch failing, and the operator needs to
                    # know which name is holding it rather than re-deriving it.
                    retained=[f"{name}@{sha}" for name, sha in retained_refs],
                )
        if repaired_refs:
            result = runner(["git", "-C", str(repo), "fetch", "origin", remote_base])
    if result.returncode == 0 and not _dangling_alternates(repo):
        return True
    if event_logger is not None:
        event_logger(
            "worklink_base_fetch_failed",
            repo=str(repo),
            base=remote_base,
            returncode=result.returncode,
            stdout=_strip_for_event(result.stdout),
            stderr=_strip_for_event(result.stderr),
        )
    return False


def _prepare_fresh_base(
    repo: Path,
    base: str,
    *,
    base_fetch: bool,
    runner: Runner,
    event_logger: EventLogger | None,
) -> str:
    """Return a fetched, locally resolvable base that contains origin's fetched tip."""
    if not base_fetch:
        raise RuntimeError("base repo fetch is disabled; refusing to build on an unverified base")
    _repair_base_alternates(repo, base=base, runner=runner, event_logger=event_logger)
    if not _fetch_base_from_origin(repo, base, runner=runner, event_logger=event_logger):
        raise RuntimeError(f"base repo fetch failed for origin/{base.removeprefix('origin/')}")

    remote_base = base.removeprefix("origin/")
    fetched_ref = f"origin/{remote_base}"
    fetched = runner(["git", "-C", str(repo), "rev-parse", "--verify", fetched_ref])
    if fetched.returncode != 0 or not fetched.stdout.strip():
        raise RuntimeError(f"fetched base tip is not resolvable as {fetched_ref}")
    fetched_tip = fetched.stdout.strip()
    start_point = _resolve_local_base(repo, remote_base, prefer_origin=True, runner=runner)
    fresh = runner(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", fetched_tip, start_point]
    )
    if fresh.returncode == 0:
        return start_point

    local_sha = _rev_parse_for_error(repo, start_point, runner=runner)
    behind = runner(
        ["git", "-C", str(repo), "rev-list", "--count", f"{start_point}..{fetched_tip}"]
    )
    count = behind.stdout.strip() if behind.returncode == 0 and behind.stdout.strip() else "unknown"
    raise RuntimeError(
        f"stale base {local_sha}, {fetched_ref} {fetched_tip}, {count} commits behind"
    )


def _rev_parse_for_error(repo: Path, ref: str, *, runner: Runner) -> str:
    result = runner(["git", "-C", str(repo), "rev-parse", "--verify", ref])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else ref


def _git_objects_dir(repo: Path) -> Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git / "objects"
    if dot_git.is_file():
        try:
            marker = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if marker.startswith("gitdir:"):
            git_dir = Path(marker.removeprefix("gitdir:").strip())
            if not git_dir.is_absolute():
                git_dir = repo / git_dir
            return git_dir.resolve() / "objects"
    if (repo / "objects").is_dir():
        return repo / "objects"
    return None


def report_foreign_owned_git_objects(
    repo: Path,
    *,
    expected_uid: int,
    event_logger: EventLogger,
) -> list[Path]:
    """Report regular files in ``repo``'s object store owned by another uid."""
    objects = _git_objects_dir(repo)
    if objects is None:
        return []

    foreign: list[Path] = []
    for object_path in sorted(objects.rglob("*")):
        try:
            metadata = object_path.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid == expected_uid:
            continue
        foreign.append(object_path)
        event_logger(
            "worklink_foreign_owned_git_object",
            repo=str(repo),
            object_path=str(object_path),
            owner_uid=metadata.st_uid,
            expected_owner_uid=expected_uid,
            mode=oct(stat.S_IMODE(metadata.st_mode)),
        )
    return foreign


def _alternate_entries(repo: Path) -> tuple[Path | None, list[tuple[str, Path]]]:
    objects = _git_objects_dir(repo)
    if objects is None:
        return None, []
    alternates = objects / "info" / "alternates"
    if not alternates.is_file():
        return alternates, []
    try:
        lines = alternates.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return alternates, []
    entries: list[tuple[str, Path]] = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = objects / path
        entries.append((line, path.resolve()))
    return alternates, entries


def _dangling_alternates(repo: Path) -> list[Path]:
    _alternates, entries = _alternate_entries(repo)
    return [path for _line, path in entries if not path.is_dir()]


def _dangling_refs(repo: Path, *, runner: Runner) -> list[tuple[str, str]]:
    """Refs whose target object is not present in this repository.

    This is the residue an alternate leaves behind. Pruning a reclaimed alternate
    removes the *pointer* to the vanished object store, but any ref that resolved
    only through it still names an object that is now absent, and ``git fetch``
    then refuses the whole operation with ``fatal: bad object <ref>``.

    Observed on 2026-07-28: the janitor reclaimed
    ``<home>/scratch/pr1188-object-db``, ``_prune_dangling_alternates`` had already
    dropped that entry on an earlier run, and the leftover
    ``refs/remotes/origin/pr/1188`` made every base fetch fail. Six worklink
    attempts across three leaves were consumed and demoted before anyone looked.
    """
    listing = runner([
        "git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)",
    ])
    if listing.returncode != 0:
        return []
    dangling: list[tuple[str, str]] = []
    for line in (listing.stdout or "").splitlines():
        name, _, sha = line.strip().partition(" ")
        if not name or not sha:
            continue
        if runner(["git", "-C", str(repo), "cat-file", "-e", sha]).returncode != 0:
            dangling.append((name, sha))
    return dangling


#: The only namespace this module will prune automatically. A remote-tracking ref
#: under ``origin`` is reconstructible: the next fetch that can see the remote
#: re-creates it, so deleting one destroys no name for any history.
_DISPOSABLE_REF_PREFIX = "refs/remotes/origin/"


def _prune_dangling_refs(repo: Path, *, runner: Runner) -> tuple[
    list[tuple[str, str]], list[tuple[str, str]]
]:
    """Delete only reconstructible refs whose objects are gone.

    Returns ``(pruned, retained)``.

    Scoped to ``refs/remotes/origin/*`` on purpose. An earlier version deleted
    every unresolvable ref, reasoning that a local branch whose tip object is
    absent "was unusable anyway". That is wrong when the alternate holding those
    objects is only *temporarily* unavailable — an unmounted volume, or a store
    that can be restored. The objects come back; the branch name does not, and it
    may be the only local name for that history. So a ``refs/heads/*`` or
    ``refs/tags/*`` casualty is reported and left alone, and the fetch fails closed
    with the retained refs named so an operator can decide.

    Objects and history are never touched here in any case — only refs.
    """
    pruned: list[tuple[str, str]] = []
    retained: list[tuple[str, str]] = []
    for name, sha in _dangling_refs(repo, runner=runner):
        if not name.startswith(_DISPOSABLE_REF_PREFIX):
            retained.append((name, sha))
            continue
        if runner(["git", "-C", str(repo), "update-ref", "-d", name]).returncode == 0:
            pruned.append((name, sha))
    return pruned, retained


def _alternates_backup_path(alternates: Path) -> Path:
    backup = alternates.with_name(f"{alternates.name}.worklink-backup")
    suffix = 1
    while backup.exists():
        backup = alternates.with_name(f"{alternates.name}.worklink-backup.{suffix}")
        suffix += 1
    return backup


def _recover_interrupted_alternates_probe(
    repo: Path,
    *,
    base: str,
    event_logger: EventLogger | None,
) -> None:
    """Restore an alternates file hidden by a probe that was killed mid-flight."""
    objects = _git_objects_dir(repo)
    if objects is None:
        return
    alternates = objects / "info" / "alternates"
    if alternates.exists():
        return

    interrupted = sorted(
        path
        for path in alternates.parent.glob(f"{alternates.name}.worklink-check*")
        if path.is_file()
    )
    if not interrupted:
        return
    if len(interrupted) > 1:
        candidates = ", ".join(str(path) for path in interrupted)
        raise RuntimeError(
            "base repo alternates probe recovery refused; multiple interrupted "
            f"probe files require operator review: {candidates}"
        )

    hidden = interrupted[0]
    hidden.replace(alternates)
    if event_logger is not None:
        event_logger(
            "worklink_base_alternates_probe_recovered",
            repo=str(repo),
            base=base.removeprefix("origin/"),
            restored_from=str(hidden),
            restored_to=str(alternates),
        )


def _check_without_alternates(
    repo: Path,
    alternates: Path,
    *,
    runner: Runner,
) -> tuple[bool, list[tuple[str, str]], list[str], str]:
    hidden = alternates.with_name(f"{alternates.name}.worklink-check")
    suffix = 1
    while hidden.exists():
        hidden = alternates.with_name(f"{alternates.name}.worklink-check.{suffix}")
        suffix += 1
    alternates.replace(hidden)
    try:
        head = runner(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{object}"])
        fsck = runner(["git", "-C", str(repo), "fsck", "--connectivity-only"])
        dangling_refs = _dangling_refs(repo, runner=runner)
    finally:
        hidden.replace(alternates)

    details = "\n".join(
        value.strip()
        for value in (head.stdout, head.stderr, fsck.stdout, fsck.stderr)
        if value and value.strip()
    )
    object_ids = sorted({
        *re.findall(r"(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])", details.lower()),
        *(sha for _name, sha in dangling_refs),
    })
    return head.returncode == 0 and fsck.returncode == 0, dangling_refs, object_ids, details


def _repair_base_alternates(
    repo: Path,
    *,
    base: str,
    runner: Runner,
    event_logger: EventLogger | None,
) -> None:
    """Remove every base-repo alternate, but only after proving it is unnecessary."""
    objects = _git_objects_dir(repo)
    if objects is None:
        return
    alternates = objects / "info" / "alternates"
    if not alternates.is_file() and not any(
        alternates.parent.glob(f"{alternates.name}.worklink-check*")
    ):
        return

    # A missing alternates file plus a check file means "interrupted" only after
    # every live probe has released this lock.  Without the lock, a concurrent
    # Worklink attempt could mistake an active probe for a crashed one and restore
    # the alternate while the first attempt is trying to verify its absence.
    lock = alternates.with_name(f"{alternates.name}.worklink-probe.lock")
    with _FileLock(lock):
        _repair_base_alternates_locked(
            repo, base=base, runner=runner, event_logger=event_logger
        )


def _repair_base_alternates_locked(
    repo: Path,
    *,
    base: str,
    runner: Runner,
    event_logger: EventLogger | None,
) -> None:
    _recover_interrupted_alternates_probe(repo, base=base, event_logger=event_logger)
    alternates, entries = _alternate_entries(repo)
    if alternates is None or not alternates.is_file():
        return

    # This probe is deliberately first. No ref, worktree registration, or alternate
    # is pruned until Git has shown what becomes unreachable without the file.
    safe, dangling_refs, object_ids, details = _check_without_alternates(
        repo, alternates, runner=runner
    )

    worktree_prune = runner(["git", "-C", str(repo), "worktree", "prune", "--verbose"])
    pruned_refs: list[tuple[str, str]] = []
    retained_refs: list[tuple[str, str]] = []
    for name, sha in dangling_refs:
        if not name.startswith(_DISPOSABLE_REF_PREFIX):
            retained_refs.append((name, sha))
        elif runner(["git", "-C", str(repo), "update-ref", "-d", name]).returncode == 0:
            pruned_refs.append((name, sha))

    # Stale worktree reflogs and reconstructible remote refs can be the only failed
    # roots. Recheck after those narrow repairs; local branches and tags remain.
    if not safe or pruned_refs or _strip_for_event(worktree_prune.stdout + worktree_prune.stderr):
        safe, dangling_refs_after, object_ids, details = _check_without_alternates(
            repo, alternates, runner=runner
        )
        retained_refs = [
            item for item in dangling_refs_after if not item[0].startswith(_DISPOSABLE_REF_PREFIX)
        ]

    retained = [str(path) for _line, path in entries]
    if not safe:
        if event_logger is not None:
            event_logger(
                "worklink_base_alternates_repair_refused",
                repo=str(repo),
                base=base.removeprefix("origin/"),
                pruned=[f"{name}@{sha}" for name, sha in pruned_refs],
                retained=retained,
                retained_refs=[f"{name}@{sha}" for name, sha in retained_refs],
                at_risk_objects=object_ids,
                worktree_prune=_strip_for_event(worktree_prune.stdout + worktree_prune.stderr),
            )
        risks = ", ".join(object_ids) or _strip_for_event(details) or "unknown objects"
        raise RuntimeError(
            "base repo alternates repair refused; objects are reachable only through "
            f"the retained alternate: {risks}"
        )

    backup = _alternates_backup_path(alternates)
    shutil.copy2(alternates, backup)
    alternates.unlink()
    if event_logger is not None:
        event_logger(
            "worklink_base_alternates_repaired",
            repo=str(repo),
            base=base.removeprefix("origin/"),
            pruned=retained,
            retained=[],
            pruned_refs=[f"{name}@{sha}" for name, sha in pruned_refs],
            retained_refs=[],
            backup=str(backup),
            worktree_prune=_strip_for_event(worktree_prune.stdout + worktree_prune.stderr),
        )


def _resolve_local_base(repo: Path, base: str, *, prefer_origin: bool = False, runner: Runner) -> str:
    """Resolve ``base`` to a locally-resolvable start point / diff floor.

    After a successful base fetch, prefer the freshly-updated remote-tracking
    ``origin/<base>``. Returning an explicit ref defeats ``git worktree add``'s
    DWIM for remote-only base names.
    """
    if base.startswith("origin/"):
        return base
    local = (f"refs/heads/{base}", base)
    remote = (f"refs/remotes/origin/{base}", f"origin/{base}")
    candidates = (remote, local) if prefer_origin else (local, remote)
    for ref, resolved in candidates:
        check = runner(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref])
        if check.returncode == 0:
            return resolved
    return base


def _strip_for_event(value: Any) -> str:
    return str(value or "").strip()


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> None:
        self._handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def _isolated_checkout_path(
    repo: Path,
    worklink_dir: str,
    issue_id: int,
    attempt: int,
    *,
    worker_authorized: bool | None = None,
) -> Path:
    """Location for an isolated attempt checkout, OUTSIDE the parent repo (#517).

    Codex resolves the active git repository from the filesystem, so the clone
    must not live inside the repo it was cloned from: nesting invites both the
    parent-resolution walk-up and a ``git clone --local`` into the repo's own
    working tree under concurrent load. Placing it at a sibling
    ``<repo.parent>/<worklink_dir>/<repo.name>/<issue>-<attempt>`` keeps the
    independent clone fully detached, and the ``<repo.name>`` segment keeps
    attempts for repos that share a parent directory from colliding. Worker
    checkouts add ``<issue>-<attempt>/checkout``: the attempt directory is a
    controller-owned mode-0700 pathname barrier, while the worker enters the
    writable checkout through its issued directory FD.
    """
    if worker_authorized is None:
        worker_authorized = coding_enabled()
    if worker_authorized:
        repo_id = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()
        return (
            _ENABLED_CHECKOUT_ROOT
            / repo_id
            / f"{issue_id}-{attempt}"
            / "checkout"
        )
    return repo.parent / worklink_dir / repo.name / f"{issue_id}-{attempt}"


def _assert_self_contained_checkout(path: Path, *, runner: Runner) -> None:
    """Assert the checkout is a real repo rooted at ``path`` (cheap, deterministic).

    A sound ``git clone --local`` resolves its own toplevel, keeps its git dir
    inside the checkout, and has no alternates file. If any condition fails, a
    backend could operate on the wrong repository or depend on janitor-swept
    objects; refuse the checkout instead (#517, #967).
    """
    resolved = path.resolve()
    top = runner(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    gitdir = runner(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"])
    top_ok = top.returncode == 0 and Path(top.stdout.strip()).resolve() == resolved
    gitdir_ok = (
        gitdir.returncode == 0
        and Path(gitdir.stdout.strip()).resolve().is_relative_to(resolved)
    )
    alternates = _git_objects_dir(path)
    has_alternates = alternates is not None and (alternates / "info" / "alternates").exists()
    if not (top_ok and gitdir_ok) or has_alternates:
        raise RuntimeError(
            "isolated checkout failed self-containment check (#517): "
            f"toplevel={top.stdout.strip()!r} git-dir={gitdir.stdout.strip()!r} "
            f"expected rooted at {resolved}; alternates={has_alternates}"
        )


def cleanup_checkout(
    lease: CheckoutLease,
    *,
    outcome: str,
    runner: Runner = _default_runner,
    safe_git: Any | None = None,
) -> bool:
    """Remove successful attempt checkouts; retain failed/blocked attempts for autopsy."""
    if outcome != "completed":
        return False
    if lease.isolated_checkout:
        if lease.worker_authorized:
            if safe_git is None:
                raise RuntimeError("authorized checkout cleanup requires safe Git")
            safe_git.run("update-ref", "-d", f"refs/heads/{lease.branch}", check=True)
            if lease.authorization is not None:
                lease.authorization.close()
            rmtree_missing_ok(lease.path.parent)
            return True
        rmtree_missing_ok(lease.path)
        delete = runner(["git", "-C", str(lease.repo), "branch", "-D", lease.branch])
        # Isolated-checkout branches usually exist only inside the clone that was
        # just removed; deleting the same name from the parent repo is a tolerated
        # legacy no-op if an older attempt shape happened to create it there.
        if delete.returncode not in (0, 1):
            raise RuntimeError((delete.stderr or delete.stdout).strip() or "git branch delete failed")
        return True
    result = runner(["git", "-C", str(lease.repo), "worktree", "remove", "--force", str(lease.path)])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git worktree remove failed")
    return True


def prune_attempt_checkouts(
    repo: Path,
    *,
    older_than: timedelta,
    now: datetime,
    worklink_dir: str = ".worklink",
    runner: Runner = _default_runner,
    is_active: Callable[[Path], bool] | None = None,
) -> list[Path]:
    """Prune retained attempt checkouts older than ``older_than``.

    This is intentionally conservative: only directories with ``<issue>-<attempt>``
    numeric names under known Worklink attempt roots are eligible.  Legacy git
    worktrees live at ``repo/<worklink_dir>/<issue>-<attempt>``; isolated Codex
    checkouts (#517) live outside the repo at
    ``repo.parent/<worklink_dir>/<repo.name>/<issue>-<attempt>``.  Both shapes
    retain failed/blocked attempts for autopsy, so both must be covered by the
    TTL prune path (#613).

    ``is_active`` (optional) is consulted for each over-TTL attempt; when it
    returns True the attempt is skipped and never reaped.
    """
    pruned: list[Path] = []
    for root, isolated in _attempt_roots(repo, worklink_dir):
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir() or not _attempt_dir_name(child.name):
                continue
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=now.tzinfo)
            if now - mtime <= older_than:
                continue
            if is_active is not None and is_active(child):
                continue
            if isolated:
                shutil.rmtree(child, ignore_errors=True)
            else:
                result = runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(child)])
                if result.returncode != 0:
                    # If git no longer knows about it, remove the stale directory
                    # so the next attempt will not collide forever.
                    shutil.rmtree(child, ignore_errors=True)
            branch = _attempt_branch_name(child.name)
            if branch:
                runner(["git", "-C", str(repo), "branch", "-D", branch])
            pruned.append(child)
    return pruned


def _attempt_roots(repo: Path, worklink_dir: str) -> list[tuple[Path, bool]]:
    """Return ``(root, isolated_checkout)`` attempt roots for ``repo`` (#613)."""
    legacy_root = repo / worklink_dir
    relocated_root = repo.parent / worklink_dir / repo.name
    roots = [(legacy_root, False)]
    if relocated_root != legacy_root:
        roots.append((relocated_root, True))
    if coding_enabled():
        enabled_root = _isolated_checkout_path(
            repo, worklink_dir, 0, 0, worker_authorized=True
        ).parent.parent
        if enabled_root not in {legacy_root, relocated_root}:
            roots.append((enabled_root, True))
    return roots


def _attempt_dir_name(name: str) -> bool:
    return _attempt_branch_name(name) is not None


def _attempt_branch_name(name: str) -> str | None:
    left, sep, right = name.partition("-")
    if not (sep and left.isdigit() and right.isdigit()):
        return None
    return f"issue/{left}-a{right}"
