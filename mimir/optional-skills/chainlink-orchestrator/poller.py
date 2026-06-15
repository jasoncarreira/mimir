#!/usr/bin/env python3
"""Worklink ready-queue poller (chainlink #444).

Discovers ``worklink:ready`` leaf issues and dispatches up to the
concurrent-claim cap by invoking ``mimir worklink run <id>`` as a **detached**
subprocess. It deliberately does NOT reimplement claim/evidence/transition —
all of that lives in the deterministic core executor behind the CLI.

Why detached: a leaf run can take minutes, but the poller framework kills a
poller subprocess after ~60s. So we launch each run in a new session
(``start_new_session=True``) so it survives the poller's exit, log its output
under the poller STATE_DIR, and return immediately after emitting one
``worklink_dispatched`` signal per launch. Crashed/abandoned runs are recovered
by the TTL reaper; per-issue exclusivity is guaranteed by ``chainlink locks
claim`` inside the run (a second launch for the same id simply fails to claim
and exits), so the cap here is only a soft *total* concurrency bound.

Arbiter shedding under resource pressure is handled by the scheduler BEFORE
this poller fires (it carries ``priority`` in pollers.json), so the body here
stays pure discovery + dispatch.

Standalone: stdlib only, no mimir imports (runs in a scrubbed subprocess).

Env (passed via pollers.json):
  MIMIR_HOME              Chainlink repo + agent home (required).
  CHAINLINK_BIN           chainlink binary (default: chainlink).
  WORKLINK_RUN_BIN        command to invoke the executor, shlex-split
                          (default: "mimir"); e.g. "uv run mimir" or an
                          absolute venv path if bare ``mimir`` isn't on PATH.
  WORKLINK_REPO           git repo the backend works in (required to dispatch).
  WORKLINK_MAX_CONCURRENT total concurrent claims allowed (default: 2; keep in
                          sync with defaults.max_concurrent in worklink.yaml).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

POLLER_NAME = os.environ.get("POLLER_NAME", "worklink-ready-queue")
READY_LABEL = "worklink:ready"
IN_PROGRESS_LABEL = "worklink:in-progress"


def _emit(record: dict) -> None:
    record.setdefault("poller", POLLER_NAME)
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def _chainlink_bin() -> str:
    return os.environ.get("CHAINLINK_BIN") or "chainlink"


def _issue_ids_with_label(home: Path, label: str) -> list[int]:
    try:
        proc = subprocess.run(
            [_chainlink_bin(), "issue", "list", "--label", label, "--status", "open", "--json"],
            cwd=str(home), capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    issues = data if isinstance(data, list) else data.get("issues", [])
    ids: list[int] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        try:
            ids.append(int(item.get("id", item.get("number"))))
        except (TypeError, ValueError):
            continue
    return ids


def main() -> int:
    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        _emit({"signal": "worklink_poller_misconfigured", "reason": "MIMIR_HOME unset"})
        return 0
    home = Path(home_env)

    ready = sorted(set(_issue_ids_with_label(home, READY_LABEL)))
    active = len(_issue_ids_with_label(home, IN_PROGRESS_LABEL))
    try:
        cap = int(os.environ.get("WORKLINK_MAX_CONCURRENT", "2"))
    except ValueError:
        cap = 2
    slots = max(0, cap - active)

    if not ready or slots == 0:
        _emit({
            "signal": "worklink_ready_scan",
            "ready_count": len(ready),
            "active": active,
            "cap": cap,
            "slots": slots,
        })
        return 0

    repo = os.environ.get("WORKLINK_REPO")
    if not repo:
        _emit({
            "signal": "worklink_poller_misconfigured",
            "reason": "WORKLINK_REPO unset; cannot dispatch",
            "ready_count": len(ready),
        })
        return 0

    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    state_dir = Path(os.environ.get("STATE_DIR") or (home / "state" / "pollers" / POLLER_NAME))
    state_dir.mkdir(parents=True, exist_ok=True)

    dispatched = 0
    for issue_id in ready[:slots]:
        argv = [*run_bin, "worklink", "run", str(issue_id), "--home", str(home), "--repo", repo]
        log_path = state_dir / f"run-{issue_id}.log"
        try:
            log_fh = log_path.open("ab")
        except OSError:
            log_fh = subprocess.DEVNULL
        try:
            subprocess.Popen(
                argv,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,  # detach: survive poller exit + 60s timeout
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _emit({"signal": "worklink_dispatch_failed", "issue_id": issue_id, "reason": str(exc)})
            continue
        finally:
            if log_fh not in (subprocess.DEVNULL, None):
                try:
                    log_fh.close()
                except OSError:
                    pass
        dispatched += 1
        _emit({
            "signal": "worklink_dispatched",
            "issue_id": issue_id,
            "log": str(log_path),
            "active_before": active,
            "cap": cap,
        })

    _emit({
        "signal": "worklink_ready_scan",
        "ready_count": len(ready),
        "active": active,
        "cap": cap,
        "dispatched": dispatched,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
