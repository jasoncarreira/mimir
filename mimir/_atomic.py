"""Single canonical atomic-write helper (chainlink #239).

Three pre-existing implementations diverged in their durability
guarantees:

- ``mimir/oauth_usage_poller._atomic_write_json``: fsync file + parent
  dir (the CR#7 invariant after the refresh-token loss incident).
- ``mimir/rate_limits._write_json_atomic``: no fsync at all — a crash
  between ``rename`` commit and writeback could revert to pre-rename.
- ``mimir/quota_pause`` inline: fsync file, no parent-dir fsync.

This module unifies them on the strongest contract (CR#7). Callers
that need to swallow OSError (e.g. a non-critical state file like
quota_pause.json where a crash-skipped write self-heals next save)
wrap the call in their own try/except rather than encoding "swallow"
into the helper — the helper's contract is "write durably or raise."
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    mode: int = 0o600,
    indent: int | None = 2,
) -> None:
    """Atomically write *payload* as JSON to *path* with durability.

    Sequence (CR#7 invariant):

    1. Create a temp file in the same directory as *path* (so the
       eventual ``rename`` is intra-filesystem and POSIX-atomic).
    2. Write *payload* as JSON.
    3. ``fsync`` the temp file so the bytes are on disk.
    4. ``os.replace(tmp, path)`` — atomic on POSIX, same-FS.
    5. ``fsync`` the parent directory so the rename itself is durable
       (best-effort; some platforms / exotic filesystems reject
       ``O_RDONLY`` on a directory and the rename remains atomic from
       userspace's point of view).

    On any failure after the temp file is created, the temp is removed
    and the exception propagates — the caller decides whether the
    failure is fatal. The destination *path* is never left in a
    half-written state: either the old file is intact or the new file
    is fully written and synced.

    Args:
        path: Destination path.
        payload: JSON-serializable payload. ``default=str`` is set so
            datetimes / Paths in caller-side dicts serialize without a
            wrapping pass.
        mode: File permission mode applied to the temp file (and
            therefore the destination after rename). Default ``0o600``.
        indent: ``json.dumps`` indent. Default 2 for human-readable
            sidecars; pass ``None`` for compact output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=indent, default=str).encode("utf-8")

    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        # tempfile.mkstemp creates with 0o600 by default; force the
        # explicit mode anyway so a future caller passing 0o644 works.
        os.fchmod(fd, mode)
        # Use os.write rather than fdopen + with-block so we own the
        # fsync ordering before close — fdopen's __exit__ closes the fd
        # without fsync, which would defeat the durability contract on
        # systems where the kernel buffers writeback.
        os.write(fd, body)
        os.fsync(fd)
    except BaseException:
        # Best-effort cleanup before propagating.
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)

    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # Parent-dir fsync is the difference between "the rename will
    # eventually be visible" and "the rename is committed even across
    # a crash now." Best-effort: Windows + some network FS reject
    # O_RDONLY on directories.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
