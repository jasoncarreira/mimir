#!/usr/bin/env python3
"""Resume a parked factory run, restoring it to a supervised running state.

Resume grants a fresh full budget: ``_supervise_factory_070`` recomputes
``deadline = loop.time() + run_timeout`` on entry and nothing persists elapsed
time, so a run parked at 11h50m comes back with the whole budget again.

``needs-human`` is explicitly not final — only ``completed``, ``partial`` and
``blocked`` are — so a parked run is resumable by design.

Two things this script checks that are easy to skip:

**The lock reads ``fresh`` immediately after the holder dies.** It has to age
into staleness before ``dead_lock`` flips, so a resume attempted straight after
a park will see a live-looking lock owned by a dead session. Stealing is
legitimate only once status proves the holder is gone, so this refuses to steal
on a fresh lock rather than forcing it.

**mimir's retained record has to come back too.** Parking sets
``controller_phase=parked``; leaving it there after the factory is running again
leaves the two views disagreeing, which is the same class of bug that let a
parked run be pruned.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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

PARKED = "needs-human"


class ResumeError(RuntimeError):
    """A resume refused before changing anything."""


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )


def factory_status(launcher: Path, run_id: str, sandbox: Path) -> dict:
    result = _run(["node", str(launcher), "status", run_id, "--repo", str(sandbox), "--json"])
    if result.returncode != 0:
        raise ResumeError(f"factory status failed: {(result.stderr or result.stdout).strip()[:300]}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ResumeError(f"factory status returned unparseable JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sandbox", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--session", required=True, help="the new session id taking the run")
    parser.add_argument(
        "--steal-stale-lock", action="store_true",
        help="take the lock when status proves the holder is gone (dead_lock true)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    sandbox = args.sandbox.resolve()
    print(f"run     : {args.run_id}")
    print(f"sandbox : {sandbox}")

    if not sandbox.is_dir():
        raise ResumeError(
            f"sandbox missing: {sandbox}. A parked run whose checkout was swept is not "
            "resumable; its snapshot is evidence only, and the factory has no restore command."
        )

    status = factory_status(args.launcher, args.run_id, sandbox)
    print(f"status  : {status.get('status')}  lock={status.get('lock')} dead_lock={status.get('dead_lock')}")
    if status.get("status") != PARKED:
        raise ResumeError(f"run is not parked (status={status.get('status')}); nothing to resume")
    if not status.get("park_snapshot"):
        print("warning : park_snapshot is null — the park was never fully published")

    record = load_factory_record(args.home, args.run_id)
    if record is None:
        raise ResumeError(f"no retained record for {args.run_id} under {args.home}")

    if args.dry_run:
        print("dry-run : would steal a stale lock if needed, resume, then reconcile the record")
        return 0

    if status.get("dead_lock"):
        if not args.steal_stale_lock:
            raise ResumeError(
                "the lock is stale (dead_lock true). Re-run with --steal-stale-lock to take it; "
                "status proving the holder gone is what makes a steal legitimate."
            )
        steal = _run([
            "node", str(args.launcher), "lock", args.run_id, "steal",
            "--session", args.session, "--repo", str(sandbox), "--json",
        ])
        if steal.returncode != 0:
            raise ResumeError(f"lock steal failed: {(steal.stderr or steal.stdout).strip()[:300]}")
        held = factory_status(args.launcher, args.run_id, sandbox)
        if held.get("lock") != "fresh" or held.get("dead_lock"):
            raise ResumeError("lock is not held fresh after the steal; refusing to resume")
        print("lock    : stolen and held fresh")
    elif status.get("lock") == "fresh":
        print(
            "note    : the lock still reads fresh. Immediately after a park this is expected — "
            "the dead holder's lock has not aged into staleness yet. Resume may refuse until it does."
        )

    resumed = _run([
        "node", str(args.launcher), "resume", args.run_id,
        "--session", args.session, "--repo", str(sandbox), "--json",
    ])
    if resumed.returncode != 0:
        raise ResumeError(f"factory resume failed: {(resumed.stderr or resumed.stdout).strip()[:300]}")

    after = factory_status(args.launcher, args.run_id, sandbox)
    if after.get("status") == PARKED:
        raise ResumeError("factory still reports needs-human after resume; the run did not unpark")
    print(f"resumed : status={after.get('status')} next={after.get('next')}")

    with factory_checkout_interlock(args.home) as acquired:
        if not acquired:
            raise ResumeError(
                "could not acquire the factory checkout interlock; the factory is running again but "
                "mimir's record still reads parked. Re-run to reconcile."
            )
        current = load_factory_record(args.home, args.run_id)
        if current is None:
            raise ResumeError("retained record vanished while resuming")
        reconciled = replace(
            current.observed(parse_factory_status(after), datetime.now(UTC).isoformat()),
            controller_phase="running",
        )
        save_factory_record(args.home, reconciled)
    print("reconciled: controller_phase=running")

    print()
    print("the run is unparked but has no driver: dispatch it through mimir so it is supervised,")
    print("rather than launching the factory CLI by hand.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ResumeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
