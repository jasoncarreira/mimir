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
- Only *top-level* entries of a root are deletion candidates; a directory
  is removed as a unit or kept as a unit.
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
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_SCRATCH_TTL_DAYS",
    "DEFAULT_SCRATCH_ROOTS",
    "SweepResult",
    "resolve_scratch_roots",
    "resolve_scratch_ttl_days",
    "sweep_scratch_roots",
]

#: Default entry age (days, by newest contained mtime) before removal.
DEFAULT_SCRATCH_TTL_DAYS = 7

#: Home-relative roots swept by default. Operators add agent-invented
#: variants (e.g. ``.review-scratch``) via ``MIMIR_SCRATCH_JANITOR_ROOTS``.
DEFAULT_SCRATCH_ROOTS: tuple[str, ...] = ("scratch",)


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
    kept = 0
    reclaimed = 0
    for name in roots:
        root = _resolve_root(home, name)
        if root is None:
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            errors.append(f"{name}: {exc}")
            continue
        for entry in entries:
            try:
                recent, size = _tree_newest_mtime_and_size(entry, cutoff)
                if recent:
                    kept += 1
                    continue
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry)
                removed.append(str(entry.relative_to(home)))
                reclaimed += size
            except OSError as exc:
                errors.append(f"{entry.name}: {exc}")
    return SweepResult(
        removed=tuple(removed),
        kept=kept,
        bytes_reclaimed=reclaimed,
        errors=tuple(errors),
    )
