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
  WORKLINK_MAX_CONCURRENT legacy override for total concurrent claims;
                          worklink.yaml defaults.max_concurrent is canonical.
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


def _active_lock_count(home: Path) -> int | None:
    """Active Chainlink lock count, or ``None`` if unreadable.

    Locks are Worklink's atomic reservation surface. Counting labels here is a
    TOCTOU bug: detached workers apply ``worklink:in-progress`` only after cold
    start, so a second autonomous dispatcher can over-admit before labels show.
    """
    try:
        proc = subprocess.run(
            [_chainlink_bin(), "locks", "list", "--json"],
            cwd=str(home), capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    locks = data.get("locks", data if isinstance(data, list) else {})
    if isinstance(locks, (dict, list)):
        return len(locks)
    return None


def _issue_ids_with_label(home: Path, label: str) -> list[int] | None:
    """Issue ids carrying ``label``, or ``None`` if the query failed.

    ``None`` (subprocess error / non-zero exit / invalid JSON) is distinct from
    ``[]`` (no matches): the caller treats a failed read as a reason to NOT
    dispatch, so the cap can't be undercounted into admitting extra workers.
    """
    try:
        proc = subprocess.run(
            [_chainlink_bin(), "issue", "list", "--label", label, "--status", "open", "--json"],
            cwd=str(home), capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    issues = data if isinstance(data, list) else data.get("issues", [])
    ids: list[int] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        raw = item.get("id")
        if raw is None:
            raw = item.get("number")
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids



def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _read_defaults_scalar(path: Path, key: str) -> str | None:
    in_defaults = False
    defaults_indent = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped == "defaults:":
            in_defaults = True
            defaults_indent = indent
            continue
        if in_defaults and indent <= defaults_indent:
            in_defaults = False
        if in_defaults and stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip().strip('"\'')
    return None


def _configured_cap(home: Path) -> int:
    """Autonomous concurrency cap: worklink.yaml is canonical.

    ``WORKLINK_MAX_CONCURRENT`` remains a legacy override for deployments that
    have not moved the knob into ``worklink.yaml`` yet. Malformed values fall
    back to the safe default rather than crashing the poller.
    """
    default = 2
    config = home / "worklink.yaml"
    if config.exists():
        try:
            configured = _read_defaults_scalar(config, "max_concurrent")
        except OSError:
            configured = None
        if configured is not None:
            return _positive_int(configured, default=default)
    legacy = os.environ.get("WORKLINK_MAX_CONCURRENT")
    if legacy is not None:
        return _positive_int(legacy, default=default)
    return default


def main() -> int:
    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        _emit({"signal": "worklink_poller_misconfigured", "reason": "MIMIR_HOME unset"})
        return 0
    home = Path(home_env)

    ready_ids = _issue_ids_with_label(home, READY_LABEL)
    active = _active_lock_count(home)
    # Fail closed: if we can't read either the ready queue or the active lock
    # count, do not dispatch (an undercounted cap could over-admit workers).
    if ready_ids is None or active is None:
        _emit({
            "signal": "worklink_poller_degraded",
            "reason": "chainlink ready/lock read failed; skipping dispatch this cycle",
        })
        return 0
    ready = sorted(set(ready_ids))
    cap = _configured_cap(home)
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
        argv = [*run_bin, "worklink", "run", str(issue_id),
                "--home", str(home), "--repo", repo, "--autonomous"]
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
