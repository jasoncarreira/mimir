"""Quiescent, attempt-scoped OpenCode event journal maintenance.

OpenCode resumes from its session/message/part tables. Its event journal also
contains a full part snapshot on every streaming update; only those redundant
``message.part.updated`` (and the observed versioned ``.1``) snapshots are
removed. Other event types are retained, including message/session updates.

This is intentionally lossy journal compaction, not an event-replay-preserving
archive: aggregate ``seq`` values and ``event_sequence`` references are not
renumbered, so deleted updates leave gaps. Current OpenCode resumes from the
materialized tables (covered by the pinned CLI child-task resume test). A future
migration/sync that replays the journal may require disabling or revisiting this
maintenance; retaining other event types does not guarantee future replay.
Versioned event names other than the observed ``.1`` are left untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import re
from urllib.parse import quote

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .factory_state import FactoryRunRecord


def _store_path(record: FactoryRunRecord) -> Path | None:
    sandbox = Path(record.sandbox)
    checkout = sandbox.parent.parent
    if checkout.name != "checkout":
        # Pre-worker factory runs have no sibling attempt-scoped OpenCode store.
        return None
    if (
        sandbox.name != record.run_id
        or sandbox.parent.name != ".factory-sandboxes"
        or checkout.parent.name != f"{record.issue_id}-{record.attempt}"
    ):
        raise RuntimeError("factory OpenCode store is outside the attempt boundary")
    return checkout.parent / ".factory-runtime" / "data" / "opencode" / "opencode.db"


def _prune_db(path: Path) -> dict[str, int] | None:
    """Called in the worker identity for executor checkouts; never create a DB."""
    for current in (path.parent.parent.parent, path.parent.parent, path.parent):
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("factory OpenCode store directory is unsafe")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("factory OpenCode store is not a regular file")
    # mode=rw prevents a missing DB from being silently created. Keep all other
    # event types: they may be needed by future OpenCode replay implementations.
    with sqlite3.connect(f"file:{quote(str(path))}?mode=rw", uri=True, timeout=1) as db:
        def checkpoint() -> None:
            # Detects active WAL transactions, NOT idle outside connections.
            # Quiescence relies on the executor acknowledgement (or explicit
            # operator recovery); SQLite locking alone is not a liveness proof.
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("factory OpenCode store still has a reader or writer")

        checkpoint()
        before = path.stat().st_size
        deleted = db.execute(
            "DELETE FROM event WHERE type IN (?, ?)",
            ("message.part.updated", "message.part.updated.1"),
        ).rowcount
        db.commit()
        checkpoint()
        db.execute("VACUUM")
        checkpoint()
        after = path.stat().st_size
    return {"bytes_before": before, "bytes_after": after, "rows_deleted": deleted}


def _clear_runtime(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("factory runtime is not a directory")
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def clear_prunable_runtime(attempt: Path) -> None:
    """Delete worker-owned contents at TTL, leaving the outer attempt to the reaper."""
    runtime = attempt / ".factory-runtime"
    if re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", attempt.name) is None:
        raise RuntimeError("factory runtime is outside a checkout attempt")
    from .worker_client import factory_checkout_for_path, run_factory_control

    checkout = attempt / "checkout"
    if not checkout.exists() and not checkout.is_symlink():
        # Interrupted outer deletion may already have removed checkout/. There
        # is then no safe worker cwd to reopen. Clear with existing controller
        # permissions only; permission failures retain the attempt for recovery.
        _clear_runtime(runtime)
    elif factory_checkout_for_path(checkout) is None:
        _clear_runtime(runtime)
    else:
        result = run_factory_control(
            checkout, [sys.executable, "-I", str(Path(__file__).resolve()),
                       "--clear-runtime", str(runtime)], env={}, timeout=3600, output_limit=4096,
        )
        if result.returncode:
            raise RuntimeError("factory runtime cleanup failed: " + result.stderr.decode(errors="replace")[:300])


def prune_factory_store(record: FactoryRunRecord) -> dict[str, int] | None:
    """Refuse a live/unverified driver before any DB access, including VACUUM.

    The caller holds the factory issue resource lock throughout finalization,
    excluding another controller's launch. The executor reaps specialist children
    before reporting the driver stopped. No other process may write this store
    while this function runs.
    """
    from mimir.event_logger import log_event_sync
    from .factory_state import factory_process_is_alive, factory_process_is_verified_dead

    path = _store_path(record)
    if path is None or (not path.exists() and not path.is_symlink()):
        return None
    if factory_process_is_alive(record) or not factory_process_is_verified_dead(record):
        raise RuntimeError("factory OpenCode prune refused: driver death is not verified")
    if not record.children_reaped:
        raise RuntimeError("factory OpenCode prune refused: specialist reaping is not verified")
    from .worker_client import factory_checkout_for_path, run_factory_control

    checkout = Path(record.sandbox).parent.parent
    if factory_checkout_for_path(checkout) is not None:
        result = run_factory_control(
            checkout, [sys.executable, "-I", str(Path(__file__).resolve()),
                       "--prune", str(path)], env={}, timeout=3600, output_limit=4096,
        )
        if result.returncode:
            raise RuntimeError("factory OpenCode store pruning failed: " + result.stderr.decode(errors="replace")[:300])
        metrics = json.loads(result.stdout)
    else:
        metrics = _prune_db(path)
    if metrics is not None:
        try:
            log_event_sync(
                "worklink_factory_opencode_pruned", issue_id=record.issue_id,
                attempt=record.attempt, run_id=record.run_id, **metrics,
            )
        except RuntimeError:
            pass  # Same best-effort event contract as the provider orchestrator.
    return metrics


if __name__ == "__main__":
    # -I script execution in the worklink worker; the controller chooses the
    # exact path from the trusted attempt layout, not from a builder prompt.
    import sys

    if len(sys.argv) != 3:
        raise SystemExit(2)
    if sys.argv[1] == "--prune":
        print(json.dumps(_prune_db(Path(sys.argv[2]))))
    elif sys.argv[1] == "--clear-runtime":
        _clear_runtime(Path(sys.argv[2]))
    else:
        raise SystemExit(2)
