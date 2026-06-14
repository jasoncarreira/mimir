"""Per-issue git worktree lifecycle for Worklink."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


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
    # resolved to (a local branch, ``origin/<base>``, or the name as-is) — see
    # ``_resolve_local_base``.
    local_base: str = ""


def create_worktree(
    repo: Path,
    *,
    issue_id: int,
    attempt: int,
    base: str = "main",
    worklink_dir: str = ".worklink",
    runner: Runner = _default_runner,
) -> WorktreeLease:
    """Create an attempt-scoped branch/worktree from a fresh base ref."""
    path = repo / worklink_dir / f"{issue_id}-{attempt}"
    branch = f"issue/{issue_id}-a{attempt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    start_point = _resolve_local_base(repo, base, runner=runner)
    # ``--no-track`` + an explicit, locally-resolvable start point: without them
    # ``git worktree add -b <branch> <path> <base>`` DWIMs a remote-only base
    # name (e.g. a slash-named feature branch that exists only as
    # ``origin/<base>``) into a tracking checkout — silently ignoring ``-b`` and
    # leaving the worktree on the base branch instead of the attempt branch.
    result = runner([
        "git", "-C", str(repo), "worktree", "add", "--no-track", "-b", branch, str(path), start_point
    ])
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


def _resolve_local_base(repo: Path, base: str, *, runner: Runner) -> str:
    """Resolve ``base`` to a locally-resolvable start point / diff floor.

    Prefers an existing local branch, then the remote-tracking ``origin/<base>``,
    else returns ``base`` unchanged (already ``origin/``-prefixed, a SHA, or a
    tag — let git resolve it). Returning an explicit ref defeats
    ``git worktree add``'s DWIM for remote-only base names.
    """
    if base.startswith("origin/"):
        return base
    candidates = ((f"refs/heads/{base}", base), (f"refs/remotes/origin/{base}", f"origin/{base}"))
    for ref, resolved in candidates:
        check = runner(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref])
        if check.returncode == 0:
            return resolved
    return base


def cleanup_worktree(lease: WorktreeLease, *, outcome: str, runner: Runner = _default_runner) -> bool:
    """Remove successful worktrees; retain failed/blocked attempts for autopsy."""
    if outcome != "completed":
        return False
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
) -> list[Path]:
    """Prune retained attempt worktrees older than ``older_than``.

    This is intentionally conservative: only directories with ``<issue>-<attempt>``
    numeric names under the configured worklink directory are eligible.
    """
    root = repo / worklink_dir
    if not root.exists():
        return []
    pruned: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir() or not _attempt_dir_name(child.name):
            continue
        mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=now.tzinfo)
        if now - mtime <= older_than:
            continue
        result = runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(child)])
        if result.returncode != 0:
            # If git no longer knows about it, remove the stale directory so the
            # next attempt will not collide forever.
            shutil.rmtree(child, ignore_errors=True)
        branch = _attempt_branch_name(child.name)
        if branch:
            runner(["git", "-C", str(repo), "branch", "-D", branch])
        pruned.append(child)
    return pruned


def _attempt_dir_name(name: str) -> bool:
    return _attempt_branch_name(name) is not None


def _attempt_branch_name(name: str) -> str | None:
    left, sep, right = name.partition("-")
    if not (sep and left.isdigit() and right.isdigit()):
        return None
    return f"issue/{left}-a{right}"
