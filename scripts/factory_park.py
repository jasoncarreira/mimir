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

**The controller has to be stopped first, before anything it owns.** The
controller's own budget park (``park_for_budget`` in ``orchestrator.py``) is
race-free precisely because it cancels a handle it owns and therefore never
observes an unexpected exit. A script that kills the compute processes under a
live controller gives it exactly that unexpected exit: it runs its failure path,
writes ``failed`` to the retained record, and races this script's later
terminalize and reconcile — and whichever write lands second wins. There is no
ordering of the compute processes that avoids this, so the controller is stopped
first and its death is verified before any other process is signalled.

Stopping the controller leaves its Chainlink claim behind, and that claim is
what refuses the next dispatch, so the park releases it as its last step. The
refusal is indirect enough to be worth stating: the chainlink CLI treats a
same-agent re-claim as idempotent success and says "You already hold the lock",
and ``claim_issue`` uses exactly that string to decide whether to run its
duplicate-liveness guard -- which then finds the stopped controller's own
heartbeat comment still fresh and returns ``duplicate_run_live`` for the whole
``duplicate_freshness_s`` window. A released lock is claimed outright instead,
so the guard is never reached.

Which process to stop is not guessed from ``ps``. The retained record's
``handle`` names the driver, and ``factory_process_is_verified_dead`` is the same
predicate mimir's own recovery path applies before it will resume, so this script
asserts the state resume actually requires rather than a proxy for it.
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
    factory_process_is_alive,
    factory_process_is_verified_dead,
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


def process_argv(pid: int) -> str | None:
    """Return a live process's argv, or None if it is gone.

    ``ps`` rather than ``/proc`` so the same check works on the macOS host where
    these scripts are tested and in the Linux container where they are run.
    """
    result = _run(["ps", "-o", "args=", "-p", str(pid)])
    if result.returncode != 0:
        return None
    argv = result.stdout.strip()
    return argv or None


def verify_controller(pid: int, issue_id: int) -> str:
    """Confirm a pid really is the controller for this issue before signalling it.

    The tokens are derived from the retained record, not supplied by the
    operator, so a mistyped pid is refused rather than acted on.
    """
    argv = process_argv(pid)
    if argv is None:
        raise ParkError(
            f"--controller-pid {pid} is not running. If the controller is already gone, "
            "pass --controller-pid none, which verifies that rather than assuming it."
        )
    required = ("worklink", "run-epic", str(issue_id))
    missing = [token for token in required if token not in argv]
    if missing:
        raise ParkError(
            f"--controller-pid {pid} does not look like this run's controller "
            f"(argv lacks {missing}): {argv[:200]}"
        )
    return argv


def find_controllers(issue_id: int) -> list[int]:
    """Report controller candidates so a refusal can name them.

    This never selects one. Picking a process to kill by pattern match is how an
    operator kills the wrong run; the operator names the pid and this verifies it.
    """
    listing = _run(["ps", "-eo", "pid=,args="])
    self_pid = os.getpid()
    found: list[int] = []
    for line in listing.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, argv = line.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        # This script's own argv carries every token it searches for.
        if "factory_park.py" in argv:
            continue
        if all(token in argv for token in ("worklink", "run-epic", str(issue_id))):
            found.append(pid)
    return found


def stop_pid(pid: int, label: str, *, timeout: float = 20.0) -> None:
    """SIGTERM, then SIGKILL, then verify the pid is actually gone."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ParkError(
                f"not permitted to signal {label} (pid {pid}): {exc}. Run as the uid that owns it."
            ) from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.5)
    if _pid_alive(pid):
        raise ParkError(f"{label} (pid {pid}) survived SIGKILL; refusing to continue the park")


def stop_residual_compute(run_id: str, *, timeout: float = 20.0) -> list[int]:
    """Stop any supervisor or driver left for this run, supervisor first.

    The record's handle is the authoritative driver and is stopped by the caller
    before this runs. This is a sweep for the rest of the tree, and the order is
    the opposite of intuition: a driver that dies under a live supervisor is
    recorded as a terminal failure, which marks the checkout prunable before the
    park can complete.
    """
    stopped: list[int] = []
    self_pid = os.getpid()
    for pattern in ("factory_supervisor", "opencode run"):
        listing = _run(["ps", "-eo", "pid=,args="])
        for line in listing.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            head, _, argv = line.partition(" ")
            try:
                pid = int(head)
            except ValueError:
                continue
            if pid == self_pid or "factory_park.py" in argv:
                continue
            if pattern not in argv or run_id not in argv:
                continue
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
    parser.add_argument(
        "--controller-pid", required=True,
        help=(
            "pid of the 'mimir worklink run-epic <issue>' controller that owns this run, "
            "or 'none' to assert it is already gone. The controller is stopped first: killing "
            "the compute processes underneath a live one makes it race this park's own writes."
        ),
    )
    parser.add_argument(
        "--chainlink-bin", default="chainlink",
        help="chainlink CLI used to release the claim the stopped controller held",
    )
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

    # Identify the controller before anything is signalled, so a bad pid is a
    # refusal rather than a half-completed park.
    controller_pid: int | None = None
    if args.controller_pid.strip().lower() == "none":
        live = find_controllers(record.issue_id)
        if live:
            raise ParkError(
                f"--controller-pid none asserts the controller is gone, but these processes "
                f"still look like this run's controller: {live}. Pass the right pid."
            )
        print("controller : already gone (verified: no matching process)")
    else:
        try:
            controller_pid = int(args.controller_pid)
        except ValueError as exc:
            raise ParkError("--controller-pid must be a pid or the word 'none'") from exc
        argv = verify_controller(controller_pid, record.issue_id)
        print(f"controller : pid {controller_pid} verified -> {argv[:120]}")

    if args.dry_run:
        print(
            "dry-run: would stop the controller, then the recorded driver, then any residual "
            "supervisor/driver, then terminalize, publish and reconcile"
        )
        return 0

    # The controller goes first and its death is verified. Until it is gone, any
    # write this script makes to the retained record can be overwritten by the
    # controller's failure path reacting to the driver's exit.
    if controller_pid is not None:
        stop_pid(controller_pid, "controller")
        print(f"stopped    : controller pid {controller_pid}")

    # The record's handle is the authoritative driver, not a ps pattern match.
    if factory_process_is_alive(record):
        handle_pid = record.handle.shim_pid if record.handle else None
        if handle_pid is None and record.handle is not None:
            try:
                handle_pid = int(record.handle.identifier)
            except ValueError:
                handle_pid = None
        if handle_pid is None:
            raise ParkError("retained handle reports alive but names no pid; refusing to guess")
        stop_pid(handle_pid, "recorded driver")
        print(f"stopped    : recorded driver pid {handle_pid}")

    residual = stop_residual_compute(args.run_id)
    print(f"stopped    : residual {residual or 'none found'}")

    # This is the predicate mimir's recovery path applies before it will resume.
    # Asserting it here means a park that publishes is a park that can be resumed.
    if not factory_process_is_verified_dead(record):
        raise ParkError(
            "cannot verify the recorded factory process is dead. mimir's recovery path requires "
            "factory_process_is_verified_dead before it will resume, so publishing a park now "
            "would produce a run that cannot be resumed through the supported path."
        )
    print("verified   : recorded process is dead (the predicate resume will re-check)")

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
    # Re-read rather than trust the write: if anything still held this record,
    # the operator needs to know now and not at resume time.
    settled = load_factory_record(args.home, args.run_id)
    if settled is None:
        raise ParkError("retained record vanished immediately after reconcile")
    if settled.controller_phase != "parked":
        raise ParkError(
            f"retained record reads controller_phase={settled.controller_phase!r} immediately "
            "after reconcile: something else is still writing it. The park is not safe to rely on."
        )
    if settled.status is None or settled.status.status != TERMINAL_PARKED:
        raise ParkError(
            "retained record does not read needs-human after reconcile; resume would refuse it"
        )
    print("settled    : record re-read and still parked")

    # Release the claim the stopped controller held. Without this an immediate
    # resume dispatch is refused: the chainlink CLI answers a same-agent
    # re-claim with "You already hold the lock" and rc=0, and `claim_issue`
    # reads that string as the trigger for its duplicate-liveness guard, which
    # then finds the stopped controller's own heartbeat comment still fresh and
    # returns `duplicate_run_live`. Releasing means the next claim acquires the
    # lock outright, so that branch is never entered.
    released = _run([args.chainlink_bin, "locks", "release", str(record.issue_id)])
    unlabelled = _run([
        args.chainlink_bin, "issue", "unlabel", str(record.issue_id), "worklink:in-progress",
    ])
    if released.returncode == 0:
        print(f"released   : claim on issue {record.issue_id}")
    if unlabelled.returncode != 0:
        print(
            f"warning    : could not clear worklink:in-progress on {record.issue_id}: "
            f"{(unlabelled.stderr or unlabelled.stdout).strip()[:200]}"
        )
    if released.returncode != 0:
        # The park itself is published and reconciled, so this is not a refusal
        # -- but the run is not dispatchable until the claim is released, which
        # is the one thing an operator must not have to discover at resume time.
        print()
        print(
            f"PARKED, BUT THE CLAIM IS STILL HELD: "
            f"{(released.stderr or released.stdout).strip()[:200]}",
            file=sys.stderr,
        )
        print(
            f"release it before resuming:  {args.chainlink_bin} locks release {record.issue_id}",
            file=sys.stderr,
        )
        return 3

    print()
    print("resume with:")
    print(f"  scripts/factory_resume.py --run-id {args.run_id} --sandbox {sandbox} \\")
    print(f"      --home {args.home} --launcher {args.launcher} --repo <CONTROLLER_REPO>")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ParkError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
