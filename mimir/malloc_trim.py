"""Best-effort glibc heap trimming for long-running Linux processes."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class TrimResult:
    rss_before_bytes: int
    rss_after_bytes: int
    released: int  # malloc_trim's C return value (1 if pages were released)


def _malloc_trim(libc: object | None = None) -> object | None:
    """Resolve the symbol lazily; alternative allocators bypass glibc's heap."""
    if sys.platform != "linux":
        return None
    preload = os.environ.get("LD_PRELOAD", "").lower()
    if "jemalloc" in preload or "tcmalloc" in preload:
        return None
    if libc is None:
        libc = ctypes.CDLL(None)
    return getattr(libc, "malloc_trim", None)


def trim_supported(*, libc: object | None = None) -> bool:
    """Whether this process has a usable glibc trim symbol."""
    try:
        return _malloc_trim(libc) is not None
    except Exception:  # noqa: BLE001 — allocator probing must never affect startup
        return False


def _rss_bytes() -> int:
    resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def trim_heap(
    *, libc: object | None = None, rss_reader: Callable[[], int] | None = None,
) -> TrimResult | None:
    """Trim freed glibc pages and return the RSS delta, or None if unavailable."""
    try:
        trim = _malloc_trim(libc)
        if trim is None:
            return None
        read_rss = rss_reader or _rss_bytes
        before = read_rss()
        released = trim(0)
        after = read_rss()
        return TrimResult(before, after, released)
    except Exception:  # noqa: BLE001 — best-effort maintenance must never fail a job
        return None
