"""Scratch retention janitor — TTL sweep of the home's ephemeral roots.

``scratch/`` is documented as an ephemeral working area (the writable-dirs
table in ``config.py``): PR-review clones, throwaway checkouts, smoke-test
homes. Nothing ever deleted them — poller-driven turns alone left a live
deployment with 140 GB under ``scratch/`` in six weeks (~2-3 GB/day of
full clones + node_modules + venvs). This module enforces the "ephemeral"
contract with a mtime-TTL sweep of each configured root's *top-level*
entries, run daily from the scheduler (``add_scratch_janitor_job``) in a
worker thread.

Safety properties:

- Roots are **home-relative paths** (no absolute paths, no ``..``) and
  must resolve inside the home — the sweep can never reach outside it.
  Missing roots are skipped silently.
- Top-level entries of a root are deletion candidates; a directory is
  removed as a unit or kept as a unit. The real ``scratch/turns`` container
  is retained and its children are independent retention units instead.
  Live turn workspaces are protected even when their mtimes exceed the TTL.
- A directory is "recent" if **any** file inside it (lstat, symlinks not
  followed) is newer than the cutoff — a six-week-old clone the agent
  touched yesterday survives. The recency walk early-exits on the first
  fresh path, so keeping a live directory costs almost nothing; only
  genuinely stale trees get walked fully (for the reclaimed-bytes count)
  and those are deleted right after.
- Symlink entries are unlinked (the link, never the target).
- Everything is best-effort: per-entry errors are collected, never raised.
"""

from __future__ import annotations

import os
import stat
import time
from contextlib import nullcontext
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path

from ._rmtree import rmtree_missing_ok
from ._context import idle_turn_eviction_guard, turn_scratch_eviction_guard

__all__ = [
    "DEFAULT_SCRATCH_TTL_DAYS",
    "DEFAULT_SCRATCH_ROOTS",
    "SweepResult",
    "resolve_scratch_roots",
    "resolve_scratch_ttl_days",
    "sweep_scratch_roots",
]

#: Default entry age (days, by newest contained mtime) before removal. The
#: recency check keeps any entry touched within the window, so a tight default
#: safely reclaims abandoned per-event clones (the 140 GB driver) while an
#: in-use checkout survives on its fresh mtimes.
DEFAULT_SCRATCH_TTL_DAYS = 1

#: Home-relative roots swept by default. Operators add agent-invented
#: variants (e.g. ``.review-scratch``) via ``MIMIR_SCRATCH_JANITOR_ROOTS``.
DEFAULT_SCRATCH_ROOTS: tuple[str, ...] = ("scratch", "attachments/fetch-cache")


def resolve_scratch_ttl_days(raw: str | None = None) -> int:
    """TTL in days from ``MIMIR_SCRATCH_TTL_DAYS`` (or ``raw``).

    Unset/blank/unparsable → :data:`DEFAULT_SCRATCH_TTL_DAYS`. Values
    ``<= 0`` mean "janitor disabled" and are returned as-is so callers
    can skip job registration.
    """
    if raw is None:
        raw = os.environ.get("MIMIR_SCRATCH_TTL_DAYS", "")
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_SCRATCH_TTL_DAYS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_SCRATCH_TTL_DAYS


def resolve_scratch_roots(raw: str | None = None) -> tuple[str, ...]:
    """Root names from ``MIMIR_SCRATCH_JANITOR_ROOTS`` (or ``raw``).

    Comma-separated **home-relative** paths (nesting allowed, e.g.
    ``state/worklink/transcripts``). Entries that are absolute or contain
    ``..`` components are dropped (fail-safe — a bad entry must never
    widen the sweep; :func:`_resolve_root` re-checks containment against
    the resolved home). Unset/blank → the default.
    """
    if raw is None:
        raw = os.environ.get("MIMIR_SCRATCH_JANITOR_ROOTS", "")
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_SCRATCH_ROOTS
    roots: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        # Absolute check BEFORE slash-stripping — stripping first would
        # turn "/etc" into the relative-looking "etc".
        if not name or os.path.isabs(name):
            continue
        name = name.strip("/")
        if not name or ".." in name.split("/"):
            continue
        if name not in roots:
            roots.append(name)
    return tuple(roots) or DEFAULT_SCRATCH_ROOTS


@dataclass(frozen=True)
class SweepResult:
    """Outcome of one :func:`sweep_scratch_roots` pass."""

    removed: tuple[str, ...] = ()
    kept: int = 0
    bytes_reclaimed: int = 0
    errors: tuple[str, ...] = ()
    #: Entries retained because a git repository depends on them via
    #: ``objects/info/alternates``. Reported separately from ``kept`` so a
    #: protected entry is visible rather than looking merely young.
    protected: tuple[str, ...] = ()


def _alternate_referenced_paths(home: Path) -> frozenset[Path]:
    """Object stores that a nearby git repository depends on.

    A git repository can borrow objects from elsewhere via
    ``objects/info/alternates``. If that target is inside a swept root, reclaiming
    it corrupts the borrowing repository: ``git fetch`` then fails with
    ``fatal: bad object <ref>`` for every ref that resolved only through it, and
    the failure looks like a broken repo rather than a reclaimed directory.

    That happened on 2026-07-28. ``<home>/scratch/pr1188-object-db`` was reclaimed
    at the 1-day TTL while ``/workspace/mimir`` referenced it, and six worklink
    attempts across three leaves failed and were auto-demoted before anyone traced
    it. So the janitor now declines to reclaim what a repository is standing on.

    Bounded on purpose: the candidate repositories are the configured running
    source checkout, the operator-configured external file-tool roots, and the
    home itself. This is a safety interlock, not a filesystem search — an unlisted
    repository is no worse off than before this existed.
    """
    candidates: list[Path] = []
    if source_dir := os.environ.get("MIMIR_SOURCE_DIR", "").strip():
        candidates.append(Path(source_dir))
    for item in os.environ.get("MIMIR_FILE_TOOL_ROOTS", "").split(","):
        # Entries may carry a ``:ro`` / ``:rw`` access suffix.
        raw = item.split(":")[0].strip()
        if raw:
            candidates.append(Path(raw))
    candidates.append(home)

    referenced: set[Path] = set()
    seen: set[Path] = set()
    for repo in candidates:
        try:
            repo = repo.resolve()
        except (OSError, RuntimeError):
            continue
        if repo in seen:
            continue
        seen.add(repo)
        for objects in (repo / ".git" / "objects", repo / "objects"):
            alternates = objects / "info" / "alternates"
            try:
                if not alternates.is_file():
                    continue
                lines = alternates.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                entry = line.strip()
                if not entry:
                    continue
                path = Path(entry)
                if not path.is_absolute():
                    path = objects / path
                try:
                    referenced.add(path.resolve())
                except (OSError, RuntimeError):
                    continue
    return frozenset(referenced)


def _entry_is_protected(entry: Path, protected_paths: frozenset[Path]) -> bool:
    """True when a borrowed object store lives at or beneath ``entry``."""
    if not protected_paths:
        return False
    try:
        resolved = entry.resolve()
    except (OSError, RuntimeError):
        # Unresolvable means we cannot prove the entry is safe to delete. Keep it;
        # a retained directory costs disk, a wrongly-deleted one costs a repository.
        return True
    for path in protected_paths:
        if path == resolved or resolved in path.parents:
            return True
    return False


def _tree_newest_mtime_and_size(
    path: Path, cutoff: float
) -> tuple[bool, int]:
    """(is_recent, size_bytes) for the tree rooted at ``path``.

    lstat-based (symlinks never followed). Early-exits with
    ``(True, 0)`` on the first path newer than ``cutoff`` — the size
    only matters for trees that are about to be deleted.
    """
    total = 0
    try:
        st = path.lstat()
    except OSError:
        return False, 0
    if st.st_mtime >= cutoff:
        return True, 0
    total += st.st_size
    if not path.is_dir() or path.is_symlink():
        return False, total
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_mtime >= cutoff:
                        return True, 0
                    total += st.st_size
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return False, total


def _resolve_root(home: Path, name: str) -> Path | None:
    """Validate ``name`` as a sweepable root under ``home`` or return None.

    ``home`` must already be resolved. Containment is checked on the
    *resolved* candidate, so a symlinked root that escapes the home is
    rejected regardless of how the name looks.
    """
    if not name or os.path.isabs(name) or ".." in name.split("/"):
        return None
    candidate = (home / name).resolve()
    if candidate == home or not candidate.is_relative_to(home):
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _directory_identities(home: Path, parent: Path) -> dict[Path, tuple[int, int]]:
    """Snapshot the real parent chain before enumerating retention candidates."""
    identities: dict[Path, tuple[int, int]] = {}
    parts = parent.relative_to(home).parts
    paths = [home, *(home.joinpath(*parts[:i]) for i in range(1, len(parts) + 1))]
    for path in paths:
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"unsafe scratch container: {path.name}")
        identities[path] = (st.st_dev, st.st_ino)
    return identities


def _quarantine_turn_entry(
    home: Path, entry: Path, identities: dict[Path, tuple[int, int]],
    expected: os.stat_result,
) -> tuple[str, int]:
    """Detach one candidate atomically; caller holds the admission lock.

    Walk the snapshotted container chain with O_NOFOLLOW. Rename using the
    opened parent fd, so replacing any pathname cannot redirect the operation.
    The returned scratch fd also anchors deletion after the lock is released.
    """
    fds: dict[Path, int] = {}
    try:
        for path, identity in identities.items():
            fd = os.open(
                path if path == home else path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                **({} if path == home else {"dir_fd": fds[path.parent]}),
            )
            fds[path] = fd
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != identity:
                raise OSError(f"scratch container changed: {path.name}")
        parent_fd = fds[entry.parent]
        current = os.stat(entry.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError(f"scratch entry changed: {entry.name}")
        scratch_fd = fds[home / "scratch"]
        trash = f".janitor-trash-{uuid4().hex}"
        os.rename(entry.name, trash, src_dir_fd=parent_fd, dst_dir_fd=scratch_fd)
        return trash, os.dup(scratch_fd)
    finally:
        for fd in fds.values():
            os.close(fd)


def _remove_quarantined_entry(name: str, scratch_fd: int) -> None:
    """Potentially slow recursive deletion, never under the lifecycle lock."""
    try:
        st = os.stat(name, dir_fd=scratch_fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            rmtree_missing_ok(name, dir_fd=scratch_fd)
        else:
            os.unlink(name, dir_fd=scratch_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(scratch_fd)


def sweep_scratch_roots(
    home: Path,
    *,
    ttl_days: int = DEFAULT_SCRATCH_TTL_DAYS,
    roots: tuple[str, ...] = DEFAULT_SCRATCH_ROOTS,
    now: float | None = None,
) -> SweepResult:
    """Delete top-level entries under each root older than ``ttl_days``.

    Synchronous by design (bounded file IO) — the scheduler job wraps it
    in ``asyncio.to_thread``. ``ttl_days <= 0`` is a no-op safeguard;
    callers should not have registered the job at all in that case.
    """
    if ttl_days <= 0:
        return SweepResult()
    home = home.resolve()
    cutoff = (now if now is not None else time.time()) - ttl_days * 86400
    removed: list[str] = []
    errors: list[str] = []
    protected_entries: list[str] = []
    kept = 0
    reclaimed = 0
    # Object stores a git repository is borrowing. Reclaiming one corrupts the
    # borrower, so these are never swept regardless of age.
    protected_paths = _alternate_referenced_paths(home)
    for name in roots:
        root = _resolve_root(home, name)
        if root is None:
            continue
        # Do not let resolution of a custom turns root hide a symlinked
        # container. Default-root enumeration also validates the chain below.
        turns_root = home / "scratch" / "turns"
        requested_root = home / name
        if (requested_root == turns_root or turns_root in requested_root.parents) and root != requested_root:
            errors.append(f"{name}: unsafe scratch container")
            continue
        try:
            root_identities = (
                _directory_identities(home, root)
                if root == home / "scratch" or turns_root == root or turns_root in root.parents
                else None
            )
            entries = sorted(root.iterdir())
        except OSError as exc:
            errors.append(f"{name}: {exc}")
            continue
        # The turns container is not one retention unit: a fresh turn must not
        # pin every older workspace. Keep the container and sweep each child
        # with the same newest-mtime/TTL rules as other top-level entries.
        candidates: list[tuple[Path, dict[Path, tuple[int, int]] | None]] = []
        for entry in entries:
            if entry == turns_root and not entry.is_symlink() and entry.is_dir():
                try:
                    identities = _directory_identities(home, entry)
                    candidates.extend((child, identities) for child in sorted(entry.iterdir()))
                except OSError as exc:
                    errors.append(f"scratch/turns: {exc}")
            else:
                candidates.append((entry, root_identities))
        for entry, identities in candidates:
            try:
                expected = entry.lstat()
                if _entry_is_protected(entry, protected_paths):
                    kept += 1
                    protected_entries.append(str(entry.relative_to(home)))
                    continue
                recent, size = _tree_newest_mtime_and_size(entry, cutoff)
                if recent:
                    kept += 1
                    continue
                cache_root = (home / "attachments" / "fetch-cache").resolve()
                resolved = entry.resolve()
                touches_cache = (
                    resolved == cache_root or cache_root in resolved.parents
                    or resolved in cache_root.parents
                )
                # Protect body, sidecar and extracted text through the entire
                # turn, including the gap between fetch_url and read_file.
                turns_root = home / "scratch" / "turns"
                touches_turns = (
                    entry == turns_root or turns_root in entry.parents
                    or entry in turns_root.parents
                )
                guard = (
                    idle_turn_eviction_guard() if touches_cache
                    else turn_scratch_eviction_guard(entry) if touches_turns
                    else nullcontext(True)
                )
                quarantine = None
                with guard as idle:
                    if not idle:
                        kept += 1
                        protected_entries.append(str(entry.relative_to(home)))
                        continue
                    if touches_turns:
                        if identities is None:
                            raise OSError("missing scratch container identity")
                        # Only validation + atomic rename holds the lock. A
                        # subsequent turn may safely recreate the original dir
                        # while the worker deletes this detached trash tree.
                        quarantine = _quarantine_turn_entry(home, entry, identities, expected)
                    elif entry.is_symlink() or not entry.is_dir():
                        entry.unlink(missing_ok=True)
                    else:
                        rmtree_missing_ok(entry)
                if quarantine is not None:
                    _remove_quarantined_entry(*quarantine)
                removed.append(str(entry.relative_to(home)))
                reclaimed += size
            except OSError as exc:
                errors.append(f"{entry.name}: {exc}")
    return SweepResult(
        removed=tuple(removed),
        kept=kept,
        bytes_reclaimed=reclaimed,
        errors=tuple(errors),
        protected=tuple(protected_entries),
    )
