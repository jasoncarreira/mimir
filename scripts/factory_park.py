#!/usr/bin/env python3
"""Park a live factory run by hand, so its control plane survives to be resumed.

A run that exhausts its budget is killed and swept, losing an approved spec, an
approved decomposition and every slice review. Parking preserves them, and
resume grants a fresh full budget because ``_supervise_factory_070`` recomputes
its deadline from the monotonic clock on entry.

Two facts drove this script's shape; both cost a real run to learn.

**The snapshot belongs to the checkout, not the sandbox.** ``observedParkSnapshot``
in the factory CLI computes ``operatorRoot = dirname(dirname(repo))``, so the
published path is ``<checkout>/.factory/.parked/<run>``. The sandbox has its own
``.factory`` holding the live plane, and publishing there produces a byte-perfect
snapshot the factory will never acknowledge.

**mimir's retained record must be reconciled, or the parked run is pruned.** The
retention guard reads mimir's last *observed* status, not the factory's plane
(``_attempt_is_active`` in ``mimir/worklink/autonomy.py``). A park that leaves
that record saying ``running`` is deleted by the next cleanup pass even though
the factory itself reports ``needs-human``. That is why the supervisor is stopped
before the driver here: if the driver dies under a live supervisor, the
supervisor records a terminal failure first and marks the tree prunable before
the park can land.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir.worklink.backends.feature_factory import parse_factory_status  # noqa: E402
from mimir.worklink.factory_state import (  # noqa: E402
    factory_checkout_interlock,
    load_factory_record,
    save_factory_record,
)

TERMINAL_PARKED = "needs-human"


class ParkError(RuntimeError):
    """A park refused before it changed anything the operator cannot undo."""


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )


def operator_root(sandbox: Path) -> Path:
    """Return the directory the factory publishes parked snapshots under.

    Mirrors ``observedParkSnapshot``: the sandbox must be
    ``<operatorRoot>/.factory-sandboxes/<runId>``, and the snapshot lives at
    ``<operatorRoot>/.factory/.parked/<runId>``. Refusing a sandbox of another
    shape is deliberate — a wrong root publishes a snapshot nothing reads.
    """
    sandbox = sandbox.resolve()
    container = sandbox.parent
    if container.name != ".factory-sandboxes":
        raise ParkError(
            f"sandbox is not under .factory-sandboxes: {sandbox}; refusing to guess a snapshot root"
        )
    return container.parent


def factory_status(launcher: Path, run_id: str, sandbox: Path) -> dict:
    result = _run(["node", str(launcher), "status", run_id, "--repo", str(sandbox), "--json"])
    if result.returncode != 0:
        raise ParkError(f"factory status failed: {(result.stderr or result.stdout).strip()[:300]}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ParkError(f"factory status returned unparseable JSON: {exc}") from exc


def plane_inventory(root: Path) -> list[tuple[str, str, int, str]]:
    """Relative path, type, mode, and content digest or link target, sorted.

    The plane-root ``factory.lock`` is excluded by the caller, not here: it is
    session liveness on a timer, so a heartbeat landing between the two reads
    would fail an otherwise byte-correct comparison. Any ``factory.lock`` below
    the plane root is run state and must match.
    """
    items: list[tuple[str, str, int, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                items.append((rel, "link", mode, os.readlink(path)))
            elif stat.S_ISDIR(st.st_mode):
                items.append((rel, "dir", mode, ""))
            elif stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                items.append((rel, "file", mode, digest.hexdigest()))
            else:
                items.append((rel, "other", mode, ""))
    return sorted(items)


def publish_snapshot(plane: Path, parked_dir: Path, run_id: str) -> Path:
    """Stage, verify, then commit by rename. The rename is the only commit point."""
    canonical = parked_dir / run_id
    staging = parked_dir / f".staging-{run_id}"
    prior = parked_dir / f".prior-{run_id}"

    if staging.exists() or staging.is_symlink():
        raise ParkError(f"residual staging tree present: {staging}")
    if prior.exists() or prior.is_symlink():
        shutil.rmtree(prior)

    # The operator root must already exist: it is the run's checkout. If it does
    # not, the sandbox path was wrong and creating it would publish a snapshot
    # nowhere the factory looks. Only .factory and .parked are ours to create,
    # one directory at a time, never through a symlink.
    root = parked_dir.parent.parent
    if not root.is_dir() or root.is_symlink():
        raise ParkError(
            f"operator root is not an existing directory: {root}; the sandbox path is wrong"
        )
    for parent in (parked_dir.parent, parked_dir):
        if parent.is_symlink():
            raise ParkError(f"refusing to write through a symlinked parent: {parent}")
        if not parent.exists():
            parent.mkdir(mode=0o2775)
        elif not parent.is_dir():
            raise ParkError(f"snapshot parent is not a directory: {parent}")

    shutil.copytree(plane, staging, symlinks=True)

    source = [item for item in plane_inventory(plane) if item[0] != "factory.lock"]
    copied = [item for item in plane_inventory(staging) if item[0] != "factory.lock"]
    if source != copied:
        shutil.rmtree(staging, ignore_errors=True)
        raise ParkError("snapshot inventory mismatch; staging discarded, nothing published")

    moved_prior = False
    try:
        if canonical.exists() or canonical.is_symlink():
            os.rename(canonical, prior)
            moved_prior = True
        os.rename(staging, canonical)
    except OSError as exc:
        if moved_prior:
            os.rename(prior, canonical)
            raise ParkError(f"commit failed ({exc}); prior snapshot restored, nothing published") from exc
        shutil.rmtree(staging, ignore_errors=True)
        raise ParkError(f"commit failed ({exc}); staging discarded, nothing published") from exc

    if moved_prior:
        shutil.rmtree(prior, ignore_errors=True)
    return canonical


def stop_processes(run_id: str, *, timeout: float = 20.0) -> list[int]:
    """Stop the supervisor before the driver, and report what was signalled.

    Order matters and is the opposite of intuition: a driver that dies under a
    live supervisor is recorded as a terminal failure, which marks the checkout
    prunable before the park can complete.
    """
    stopped: list[int] = []
    for pattern in ("factory_supervisor", "opencode run"):
        listing = _run(["ps", "-eo", "pid=,args="])
        for line in listing.stdout.splitlines():
            line = line.strip()
            if not line or pattern not in line or run_id not in line:
                continue
            # Never match this script's own argv, which carries both strings.
            if str(os.getpid()) == line.split(maxsplit=1)[0]:
                continue
            pid = int(line.split(maxsplit=1)[0])
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
            except (OSError, ProcessLookupError):
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(_pid_alive(pid) for pid in stopped):
                break
            time.sleep(0.5)
    for pid in stopped:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    return stopped


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sandbox", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path, help="MIMIR_HOME holding the retained record")
    parser.add_argument("--launcher", required=True, type=Path, help="path to the factory CLI entrypoint")
    parser.add_argument("--reason", required=True, help="diagnosis that makes resume a one-liner")
    parser.add_argument("--dry-run", action="store_true", help="report the plan and refuse to change anything")
    args = parser.parse_args(argv)

    sandbox = args.sandbox.resolve()
    root = operator_root(sandbox)
    plane = sandbox / ".factory" / args.run_id
    parked_dir = root / ".factory" / ".parked"

    print(f"run        : {args.run_id}")
    print(f"sandbox    : {sandbox}")
    print(f"live plane : {plane}")
    print(f"snapshot   : {parked_dir / args.run_id}")

    if not plane.is_dir():
        raise ParkError(f"live plane missing: {plane}")

    before = factory_status(args.launcher, args.run_id, sandbox)
    status = before.get("status")
    print(f"status     : {status}")
    if status == TERMINAL_PARKED:
        raise ParkError("run is already parked; nothing to do")
    if status in {"completed", "partial", "blocked"}:
        raise ParkError(f"run is terminal ({status}); it cannot be parked")

    record = load_factory_record(args.home, args.run_id)
    if record is None:
        raise ParkError(f"no retained record for {args.run_id} under {args.home}")

    if args.dry_run:
        print("dry-run: would stop the supervisor then the driver, terminalize, publish, reconcile")
        return 0

    stopped = stop_processes(args.run_id)
    print(f"stopped    : {stopped or 'none found'}")

    terminal = _run([
        "node", str(args.launcher), "terminal", args.run_id, TERMINAL_PARKED,
        "--reason", args.reason, "--repo", str(sandbox), "--json",
    ])
    if terminal.returncode != 0:
        raise ParkError(f"factory terminal failed: {(terminal.stderr or terminal.stdout).strip()[:300]}")
    print("terminalized: needs-human")

    published = publish_snapshot(plane, parked_dir, args.run_id)
    print(f"published  : {published}")

    after = factory_status(args.launcher, args.run_id, sandbox)
    if not after.get("park_snapshot"):
        raise ParkError(
            "factory status reports park_snapshot null: the snapshot is not acknowledged. "
            "The park is not complete; do not treat the run as resumable."
        )
    print(f"verified   : park_snapshot = {after['park_snapshot']}")

    # Reconcile mimir's view last, under the interlock the pruner also takes, so
    # cleanup cannot observe a stale 'running' record and delete a parked run.
    with factory_checkout_interlock(args.home) as acquired:
        if not acquired:
            raise ParkError(
                "could not acquire the factory checkout interlock; the park is published but "
                "mimir's record still reads stale. Re-run to reconcile before cleanup runs."
            )
        current = load_factory_record(args.home, args.run_id)
        if current is None:
            raise ParkError("retained record vanished while parking")
        reconciled = replace(
            current.observed(parse_factory_status(after), datetime.now(UTC).isoformat()),
            controller_phase="parked",
        )
        save_factory_record(args.home, reconciled)
    print("reconciled : controller_phase=parked, observed status=needs-human")

    print()
    print("resume with:")
    print(f"  scripts/factory_resume.py --run-id {args.run_id} --sandbox {sandbox} \\")
    print(f"      --home {args.home} --launcher {args.launcher} --session <NEW_SESSION_ID>")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ParkError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
