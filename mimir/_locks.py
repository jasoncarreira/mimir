"""Per-file ``flock(LOCK_EX)`` helpers (SPEC §4.7).

The pattern: each content path gets a sidecar lock file in
``<home>/.mimir/locks/<sha8>.lock``. Holders ``flock`` the sidecar, do their
read-modify-write or atomic replace on the content path, then close the
sidecar (releasing the lock).

Why a sidecar instead of locking the content file directly:
- Locking the content file requires opening it; ``open("w")`` truncates BEFORE
  the lock is acquired → torn-write window.
- Sidecar is simple: open-or-create the lock file, ``flock`` it, do work,
  close. No mode juggling.

All blocking syscalls run in a thread via ``asyncio.to_thread`` so the event
loop is never blocked.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def _lock_path_for(home: Path, target: Path) -> Path:
    """Stable lock-file path keyed by target's resolved bytes."""
    digest = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:16]
    return home / ".mimir" / "locks" / f"{digest}.lock"


@contextmanager
def flock_path(home: Path, target: Path):
    """Acquire an exclusive lock for ``target``. Blocking — call from a thread."""
    lock_path = _lock_path_for(home, target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # ``a+`` opens for read/write, creates if missing, never truncates.
    fd = open(lock_path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


async def with_path_lock(home: Path, target: Path, fn: Callable[[], T]) -> T:
    """Run ``fn`` in a worker thread while holding the path's exclusive lock."""

    def _run() -> T:
        with flock_path(home, target):
            return fn()

    return await asyncio.to_thread(_run)
