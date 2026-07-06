"""Per-issue git worktree lifecycle for Worklink."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Sequence

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
EventLogger = Callable[..., None]


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class WorktreeLease:
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


@dataclass(frozen=True)
class IntegrationBranchLease:
    epic_id: int
    repo: Path
    path: Path
    branch: str
    base_ref: str
    local_base: str


@dataclass(frozen=True)
class SliceMergeSuccess:
    slice_branch: str
    integration_branch: str
    merge_commit: str


@dataclass(frozen=True)
class SliceMergeConflict:
    slice_branch: str
    integration_branch: str
    stdout: str
    stderr: str


SliceMergeResult = SliceMergeSuccess | SliceMergeConflict


def create_integration_branch(
    repo: Path,
    *,
    epic_id: int,
    base_ref: str,
    slug: str = "integration",
    epic_branch_prefix: str = "epic/",
    worklink_dir: str = ".worklink",
    base_fetch: bool = True,
    event_logger: EventLogger | None = None,
    runner: Runner = _default_runner,
) -> IntegrationBranchLease:
    """Create one epic integration branch/worktree from ``origin/<base_ref>``."""
    branch_slug = _branch_slug(slug)
    branch = f"{epic_branch_prefix}{epic_id}-{branch_slug}"
    path = repo / worklink_dir / "epics" / f"{epic_id}-{branch_slug}"
    path.parent.mkdir(parents=True, exist_ok=True)
    fetch_ok = (
        _fetch_base_from_origin(repo, base_ref, runner=runner, event_logger=event_logger)
        if base_fetch
        else False
    )
    start_point = _resolve_local_base(repo, base_ref, prefer_origin=fetch_ok, runner=runner)
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
    return IntegrationBranchLease(
        epic_id=epic_id,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=base_ref,
        local_base=start_point,
    )


def create_slice_worktree(
    repo: Path,
    *,
    slice_id: int,
    integration_branch: str,
    worklink_dir: str = ".worklink",
    runner: Runner = _default_runner,
) -> WorktreeLease:
    """Create a slice branch/worktree from the current integration branch HEAD."""
    attempt = _next_attempt(repo, slice_id, worklink_dir=worklink_dir, runner=runner)
    path = repo / worklink_dir / f"{slice_id}-{attempt}"
    branch = f"issue/{slice_id}-a{attempt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = runner(["git", "-C", str(repo), "rev-parse", "--verify", integration_branch])
    if head.returncode != 0:
        raise RuntimeError((head.stderr or head.stdout).strip() or "git rev-parse failed")
    start_point = head.stdout.strip()
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
    return WorktreeLease(
        issue_id=slice_id,
        attempt=attempt,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=integration_branch,
        local_base=start_point,
    )


def merge_slice_into_integration(
    repo: Path,
    *,
    slice_branch: str,
    integration_branch: str,
    runner: Runner = _default_runner,
) -> SliceMergeResult:
    """Serially merge ``slice_branch`` into ``integration_branch`` with ``--no-ff``."""
    with _integration_merge_lock(repo, integration_branch, runner=runner):
        integration_path = _worktree_path_for_branch(
            repo, integration_branch, runner=runner
        ) or repo
        current = runner(["git", "-C", str(integration_path), "branch", "--show-current"])
        if current.returncode != 0:
            raise RuntimeError((current.stderr or current.stdout).strip() or "git branch failed")
        if current.stdout.strip() != integration_branch:
            raise RuntimeError(f"integration branch is not checked out: {integration_branch}")

        merge = runner(
            [
                "git",
                "-C",
                str(integration_path),
                "merge",
                "--no-ff",
                "--no-edit",
                slice_branch,
            ]
        )
        if merge.returncode != 0:
            abort = runner(["git", "-C", str(integration_path), "merge", "--abort"])
            if abort.returncode not in (0, 128):
                raise RuntimeError((abort.stderr or abort.stdout).strip() or "git merge abort failed")
            return SliceMergeConflict(
                slice_branch=slice_branch,
                integration_branch=integration_branch,
                stdout=merge.stdout,
                stderr=merge.stderr,
            )

        commit = runner(["git", "-C", str(integration_path), "rev-parse", "HEAD"])
        if commit.returncode != 0:
            raise RuntimeError((commit.stderr or commit.stdout).strip() or "git rev-parse failed")
        _cleanup_slice_worktree(repo, slice_branch, runner=runner)
        return SliceMergeSuccess(
            slice_branch=slice_branch,
            integration_branch=integration_branch,
            merge_commit=commit.stdout.strip(),
        )


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
) -> WorktreeLease:
    """Create an attempt-scoped branch/worktree from a fresh base ref."""
    path = repo / worklink_dir / f"{issue_id}-{attempt}"
    branch = f"issue/{issue_id}-a{attempt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    fetch_ok = (
        _fetch_base_from_origin(repo, base, runner=runner, event_logger=event_logger)
        if base_fetch
        else False
    )
    start_point = _resolve_local_base(repo, base, prefer_origin=fetch_ok, runner=runner)
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
    return WorktreeLease(
        issue_id=issue_id,
        attempt=attempt,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=base,
        local_base=start_point,
    )


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
) -> WorktreeLease:
    """Create an attempt-scoped local clone with its own ``.git`` directory.

    Some coding CLIs inspect git metadata instead of honoring their process cwd.
    A normal ``git worktree`` stores a ``.git`` file that points back into the
    parent checkout's common git dir; Codex has been observed to treat that
    parent as the active repository and edit it.  This checkout shape keeps the
    same branch/diff contract while giving the backend a real repository rooted
    at the attempt path.
    """

    path = _isolated_checkout_path(repo, worklink_dir, issue_id, attempt)
    branch = f"issue/{issue_id}-a{attempt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"attempt checkout already exists: {path}")

    fetch_ok = (
        _fetch_base_from_origin(repo, base, runner=runner, event_logger=event_logger)
        if base_fetch
        else False
    )
    start_point = _resolve_local_base(repo, base, prefer_origin=fetch_ok, runner=runner)
    start_sha = runner(["git", "-C", str(repo), "rev-parse", "--verify", start_point])
    if start_sha.returncode != 0:
        raise RuntimeError((start_sha.stderr or start_sha.stdout).strip() or "git rev-parse failed")
    local_base = start_sha.stdout.strip()

    clone = runner(["git", "clone", "--local", "--quiet", str(repo), str(path)])
    if clone.returncode != 0:
        raise RuntimeError((clone.stderr or clone.stdout).strip() or "git clone failed")

    remote = runner(["git", "-C", str(repo), "config", "--get", "remote.origin.url"])
    if remote.returncode == 0 and remote.stdout.strip():
        set_remote = runner(
            ["git", "-C", str(path), "remote", "set-url", "origin", remote.stdout.strip()]
        )
        if set_remote.returncode != 0:
            raise RuntimeError((set_remote.stderr or set_remote.stdout).strip() or "git remote set-url failed")

    checkout = runner(["git", "-C", str(path), "checkout", "-B", branch, local_base])
    if checkout.returncode != 0:
        raise RuntimeError((checkout.stderr or checkout.stdout).strip() or "git checkout failed")

    # #517: verify the clone is a real, self-contained repo rooted at ``path`` and
    # does not resolve back to the parent before any backend inspects its git
    # metadata. Fail loud (and clean up the half-made checkout) rather than handing
    # codex a checkout that would walk up into the repo root.
    try:
        _assert_self_contained_checkout(path, runner=runner)
    except RuntimeError:
        shutil.rmtree(path, ignore_errors=True)
        raise

    return WorktreeLease(
        issue_id=issue_id,
        attempt=attempt,
        repo=repo,
        path=path,
        branch=branch,
        base_ref=base,
        local_base=local_base,
        isolated_checkout=True,
    )


def _fetch_base_from_origin(
    repo: Path,
    base: str,
    *,
    runner: Runner,
    event_logger: EventLogger | None = None,
) -> bool:
    """Best-effort refresh of ``origin/<base>`` without touching local branches."""
    if base.startswith("origin/"):
        return False
    result = runner(["git", "-C", str(repo), "fetch", "origin", base])
    if result.returncode == 0:
        return True
    if event_logger is not None:
        event_logger(
            "worklink_base_fetch_failed",
            repo=str(repo),
            base=base,
            returncode=result.returncode,
            stdout=_strip_for_event(result.stdout),
            stderr=_strip_for_event(result.stderr),
        )
    return False


def _resolve_local_base(repo: Path, base: str, *, prefer_origin: bool = False, runner: Runner) -> str:
    """Resolve ``base`` to a locally-resolvable start point / diff floor.

    After a successful base fetch, prefer the freshly-updated remote-tracking
    ``origin/<base>``. Otherwise keep the historical local-first behavior so a
    failed/disabled fetch does not silently use a stale remote-tracking ref.
    Returning an explicit ref defeats ``git worktree add``'s DWIM for remote-only
    base names.
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


def _branch_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "integration"


def _next_attempt(repo: Path, slice_id: int, *, worklink_dir: str, runner: Runner) -> int:
    attempt = 1
    while True:
        branch = f"refs/heads/issue/{slice_id}-a{attempt}"
        path = repo / worklink_dir / f"{slice_id}-{attempt}"
        exists = runner(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch])
        if exists.returncode != 0 and not path.exists():
            return attempt
        attempt += 1


def _integration_merge_lock(repo: Path, integration_branch: str, *, runner: Runner):
    common_dir = runner(["git", "-C", str(repo), "rev-parse", "--git-common-dir"])
    if common_dir.returncode == 0 and common_dir.stdout.strip():
        lock_root = Path(common_dir.stdout.strip())
        if not lock_root.is_absolute():
            lock_root = repo / lock_root
    else:
        lock_root = repo / ".git"
    lock_dir = lock_root / "worklink-integration-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / _branch_slug(integration_branch)
    return _FileLock(lock_file)


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


def _worktree_path_for_branch(repo: Path, branch: str, *, runner: Runner) -> Path | None:
    result = runner(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git worktree list failed")
    path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
        elif not line:
            path = None
    return None


def _cleanup_slice_worktree(repo: Path, slice_branch: str, *, runner: Runner) -> None:
    path = _worktree_path_for_branch(repo, slice_branch, runner=runner)
    if path is None:
        return
    result = runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git worktree remove failed")


def _isolated_checkout_path(repo: Path, worklink_dir: str, issue_id: int, attempt: int) -> Path:
    """Location for an isolated attempt checkout, OUTSIDE the parent repo (#517).

    Codex resolves the active git repository from the filesystem, so the clone
    must not live inside the repo it was cloned from: nesting invites both the
    parent-resolution walk-up and a ``git clone --local`` into the repo's own
    working tree under concurrent load. Placing it at a sibling
    ``<repo.parent>/<worklink_dir>/<repo.name>/<issue>-<attempt>`` keeps the
    independent clone fully detached, and the ``<repo.name>`` segment keeps
    attempts for repos that share a parent directory from colliding.
    """
    return repo.parent / worklink_dir / repo.name / f"{issue_id}-{attempt}"


def _assert_self_contained_checkout(path: Path, *, runner: Runner) -> None:
    """Assert the checkout is a real repo rooted at ``path`` (cheap, deterministic).

    A sound ``git clone`` resolves its own toplevel and keeps its git dir inside
    the checkout. If either resolves elsewhere (e.g. back to the parent), a
    metadata-inspecting backend like codex would operate on the wrong repository;
    refuse the checkout instead of leaking edits into the repo root (#517).
    """
    resolved = path.resolve()
    top = runner(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    gitdir = runner(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"])
    top_ok = top.returncode == 0 and Path(top.stdout.strip()).resolve() == resolved
    gitdir_ok = (
        gitdir.returncode == 0
        and Path(gitdir.stdout.strip()).resolve().is_relative_to(resolved)
    )
    if not (top_ok and gitdir_ok):
        raise RuntimeError(
            "isolated checkout failed self-containment check (#517): "
            f"toplevel={top.stdout.strip()!r} git-dir={gitdir.stdout.strip()!r} "
            f"expected rooted at {resolved}"
        )


def cleanup_worktree(lease: WorktreeLease, *, outcome: str, runner: Runner = _default_runner) -> bool:
    """Remove successful attempt checkouts; retain failed/blocked attempts for autopsy."""
    if outcome != "completed":
        return False
    if lease.isolated_checkout:
        shutil.rmtree(lease.path)
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


def prune_attempt_worktrees(
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
    returns True the attempt is skipped and never reaped.  This guards a live
    detached-factory run — whose top-level attempt-dir mtime freezes while its
    real work happens in deep subdirs — from being misclassified as abandoned by
    the mtime-only staleness test and having its checkout (and factory
    ``run.json``) removed mid-flight.  Defaults to ``None`` (legacy: no liveness
    check), so this stays import-light and callers opt in.
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
    isolated_root = _isolated_checkout_path(repo, worklink_dir, 0, 0).parent
    roots = [(legacy_root, False)]
    if isolated_root != legacy_root:
        roots.append((isolated_root, True))
    return roots


def _attempt_dir_name(name: str) -> bool:
    return _attempt_branch_name(name) is not None


def _attempt_branch_name(name: str) -> str | None:
    left, sep, right = name.partition("-")
    if not (sep and left.isdigit() and right.isdigit()):
        return None
    return f"issue/{left}-a{right}"
