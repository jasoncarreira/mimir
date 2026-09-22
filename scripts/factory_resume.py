#!/usr/bin/env python3
"""Check a parked factory run is recoverable, then dispatch it through mimir.

This script deliberately does **not** call ``factory resume``.

mimir's recovery path does the whole transition as one operation: it refuses a
status that is not ``running`` or ``needs-human``, requires the retained process
to be verifiably dead, steals or claims the session lock as needed, resumes,
checks the returned status is owned and running, validates the recovery binding,
launches a driver, saves the new handle and enters supervision. Unparking from
outside spends the ``needs-human`` transition that path is gated on, and then the
supported path refuses the run with ``factory resume requires current status
needs-human``. That is not hypothetical: it is how an attempt was lost.

Two details follow from reading that path rather than guessing at it:

**The session is not the operator's to choose.** Recovery steals the lock using
the *recorded* session and then resumes with it. A new session id produces a lock
whose owner does not match what resume expects.

**A stale lock is not the operator's to steal.** Recovery already steals when
``lock`` is ``stale`` or ``dead_lock`` is set, in the same sequence that then
verifies ownership. A manual steal beforehand only moves the lock out from under
those checks.

So the useful work here is the preflight: report exactly which of mimir's own
recovery preconditions hold, and refuse with the specific one that does not,
before spending a dispatch on a run that will be rejected.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir.worklink.factory_state import (  # noqa: E402
    factory_process_is_alive,
    factory_process_is_verified_dead,
    load_factory_record,
)
from mimir.worklink.orchestrator import _RECOVERABLE_FACTORY_PHASES  # noqa: E402

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


def preflight(record, status: dict, sandbox: Path) -> list[str]:
    """Return the recovery preconditions that do not hold, most specific first.

    Each check mirrors one in ``_verify_factory_recovery_target`` or the resume
    block that follows it. ``_RECOVERABLE_FACTORY_PHASES`` is imported rather
    than restated so this cannot drift from the gate it is reporting on.
    """
    problems: list[str] = []

    reported = status.get("status")
    if reported not in {"running", PARKED}:
        problems.append(
            f"factory status is {reported!r}; recovery resumes only 'running' or 'needs-human'"
        )

    if record.controller_phase not in _RECOVERABLE_FACTORY_PHASES:
        problems.append(
            f"retained controller_phase is {record.controller_phase!r}; recoverable phases are "
            f"{sorted(_RECOVERABLE_FACTORY_PHASES)}"
        )

    if not record.session:
        problems.append("retained session is missing; recovery resumes with the recorded session")

    if (
        record.status is not None
        and not record.status.is_terminal
        and record.status.status != PARKED
    ):
        problems.append(
            f"retained status is {record.status.status!r}; recovery requires 'needs-human'. "
            "An out-of-band 'factory resume' is the usual cause."
        )

    if factory_process_is_alive(record):
        problems.append("the retained factory process is still alive; recovery refuses a live run")
    elif not factory_process_is_verified_dead(record):
        problems.append(
            "the retained process cannot be verified dead (no recorded birth marker, or it is a "
            "zombie); recovery requires verified death to rule out pid reuse"
        )

    if not sandbox.is_absolute() or not sandbox.is_dir() or sandbox.is_symlink():
        problems.append(f"sandbox is unavailable as an absolute real directory: {sandbox}")

    if not status.get("park_snapshot") and reported == PARKED:
        problems.append(
            "park_snapshot is null: the park was never acknowledged, so its control plane was "
            "not published and the run is evidence only"
        )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sandbox", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--repo", type=Path, help="controller repo checkout to dispatch from")
    parser.add_argument(
        "--dispatch", action="store_true",
        help="after a clean preflight, dispatch through 'mimir worklink run-epic --autonomous'",
    )
    args = parser.parse_args(argv)

    sandbox = args.sandbox.resolve()
    print(f"run     : {args.run_id}")
    print(f"sandbox : {sandbox}")

    if not sandbox.is_dir():
        raise ResumeError(
            f"sandbox missing: {sandbox}. A parked run whose checkout was swept is not "
            "resumable; its snapshot is evidence only, and the factory has no restore command."
        )

    record = load_factory_record(args.home, args.run_id)
    if record is None:
        raise ResumeError(f"no retained record for {args.run_id} under {args.home}")

    status = factory_status(args.launcher, args.run_id, sandbox)
    print(
        f"factory : status={status.get('status')} lock={status.get('lock')} "
        f"dead_lock={status.get('dead_lock')} park_snapshot={bool(status.get('park_snapshot'))}"
    )
    print(
        f"record  : phase={record.controller_phase} "
        f"status={None if record.status is None else record.status.status} "
        f"session={'set' if record.session else 'MISSING'} issue={record.issue_id}"
    )

    problems = preflight(record, status, sandbox)
    if problems:
        print()
        print("not recoverable:")
        for problem in problems:
            print(f"  - {problem}")
        raise ResumeError(f"{len(problems)} recovery precondition(s) do not hold")
    print("preflight: every recovery precondition holds")

    dispatch = [
        "mimir", "worklink", "run-epic", str(record.issue_id),
        "--home", str(args.home), "--repo", str(args.repo or record.sandbox), "--autonomous",
    ]
    print()
    if not args.dispatch:
        print("dispatch with (or re-run with --dispatch):")
        print("  " + " ".join(dispatch))
        print()
        print(
            "that path resumes, relaunches and enters supervision as one operation. Do not run\n"
            "'factory resume' first: it spends the needs-human transition the path is gated on."
        )
        return 0

    if args.repo is None:
        raise ResumeError("--dispatch needs --repo: the controller checkout, not the sandbox")
    print("dispatching: " + " ".join(dispatch))
    result = _run(dispatch, timeout=600)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise ResumeError(f"dispatch exited {result.returncode}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ResumeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
