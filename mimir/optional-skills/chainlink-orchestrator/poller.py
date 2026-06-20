#!/usr/bin/env python3
"""Worklink ready-queue poller (chainlink #444).

Discovers ``worklink:ready`` leaf issues that are also unblocked in
Chainlink, then dispatches up to the concurrent-claim cap by invoking
``mimir worklink run <id>`` as a **detached** subprocess. It deliberately does
NOT reimplement claim/evidence/transition — all of that lives in the
deterministic core executor behind the CLI.

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

Imports the Worklink config loader from mimir so the autonomous cap uses the same
parser/defaulting as the CLI/tool path. The poller runs under the mimir venv via
``sys.executable``.

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
import re
import shlex
import subprocess
import sys
from pathlib import Path


def _ensure_mimir_import_path() -> None:
    """Let an installed optional-skill poller import the source checkout.

    Optional poller commands run as subprocesses from the installed skill dir. In
    a production container that interpreter is normally the project venv's
    ``python3``, but its default import path contains the skill dir rather than
    the source checkout. Add the checkout root from the colocated ``mimir`` CLI
    shim when available so ``import mimir`` works without requiring operators to
    hand-set PYTHONPATH.
    """

    exe = Path(sys.executable).resolve()
    candidates = [
        exe.parent.parent,
        Path(os.environ.get("MIMIR_SOURCE_DIR", "")) if os.environ.get("MIMIR_SOURCE_DIR") else None,
        Path("/workspace/mimir"),
    ]
    for candidate in candidates:
        if candidate and (candidate / "mimir" / "__init__.py").is_file():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return


_ensure_mimir_import_path()

from mimir.worklink.backends.registry import WorklinkConfig, WorklinkDefaults

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


def _issue_ids_from_records(data: object) -> list[int]:
    if isinstance(data, list):
        issues = data
    elif isinstance(data, dict):
        issues = data.get("issues", [])
    else:
        issues = []
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


def _issue_ids_from_ready_text(text: str) -> list[int]:
    """Parse ``chainlink issue ready`` text output when ``--json`` is ignored.

    chainlink 0.2.0 advertises ``--json`` for ``issue ready`` but still prints
    the human format in some deployed builds. The text is already Chainlink's
    filtered ready set, so parsing only leading ``#<id>`` rows preserves the
    blocker semantics without reimplementing edges in the poller.
    """
    ids: list[int] = []
    for line in text.splitlines():
        match = re.match(r"^\s*#(\d+)\b", line)
        if match:
            ids.append(int(match.group(1)))
    return ids


def _actionable_issue_ids(home: Path) -> list[int] | None:
    """Open issue ids that Chainlink considers ready/actionable.

    This intentionally delegates dependency semantics to ``chainlink issue ready``
    instead of reimplementing blocker-edge logic in the poller. Chainlink's
    ready command filters out issues blocked by open blockers while allowing
    issues whose blockers have since closed. If the CLI cannot provide
    parseable JSON or recognizable ready-list text, fail closed (``None``) rather
    than risk dispatching a blocked leaf.
    """
    try:
        proc = subprocess.run(
            [_chainlink_bin(), "issue", "ready", "--json"],
            cwd=str(home), capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    output = proc.stdout or ""
    try:
        return _issue_ids_from_records(json.loads(output or "[]"))
    except json.JSONDecodeError:
        return _issue_ids_from_ready_text(output)


def _worklink_ready_actionable_ids(home: Path) -> tuple[list[int], int, int] | None:
    """Return dispatchable ``worklink:ready`` ids plus raw/blocked counts.

    ``worklink:ready`` remains the human/planner intent signal, but it is no
    longer sufficient: the issue must also appear in Chainlink's ready set.
    """
    ready_ids = _issue_ids_with_label(home, READY_LABEL)
    actionable_ids = _actionable_issue_ids(home)
    if ready_ids is None or actionable_ids is None:
        return None
    labeled = set(ready_ids)
    actionable = set(actionable_ids)
    dispatchable = sorted(labeled & actionable)
    blocked_count = len(labeled - actionable)
    return dispatchable, len(labeled), blocked_count


def _configured_cap(home: Path) -> int:
    """Autonomous concurrency cap: ``worklink.yaml`` is canonical.

    Read through ``WorklinkConfig`` instead of a poller-local YAML subset parser
    so the detached ready-queue poller honors the same syntax, defaults, and
    malformed-value fallback as the in-turn ``worklink_run`` path.

    ``WORKLINK_MAX_CONCURRENT`` remains a legacy override only when no
    ``worklink.yaml`` is present.
    """
    config = home / "worklink.yaml"
    if config.exists():
        try:
            return WorklinkConfig.load(config).defaults.max_concurrent
        except (OSError, ValueError):
            return WorklinkDefaults.max_concurrent
    legacy = os.environ.get("WORKLINK_MAX_CONCURRENT")
    if legacy is not None:
        try:
            parsed = int(legacy)
        except ValueError:
            return WorklinkDefaults.max_concurrent
        return parsed if parsed > 0 else WorklinkDefaults.max_concurrent
    return WorklinkDefaults.max_concurrent


def main() -> int:
    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        _emit({"signal": "worklink_poller_misconfigured", "reason": "MIMIR_HOME unset"})
        return 0
    home = Path(home_env)

    ready_result = _worklink_ready_actionable_ids(home)
    active = _active_lock_count(home)
    # Fail closed: if we can't read either the ready queue, Chainlink's
    # actionable set, or the active lock count, do not dispatch (an undercounted
    # cap or blocked issue could over-admit workers).
    if ready_result is None or active is None:
        _emit({
            "signal": "worklink_poller_degraded",
            "reason": "chainlink ready/actionable/lock read failed; skipping dispatch this cycle",
        })
        return 0
    ready, labeled_ready_count, blocked_ready_count = ready_result
    cap = _configured_cap(home)
    slots = max(0, cap - active)

    if not ready or slots == 0:
        _emit({
            "signal": "worklink_ready_scan",
            "ready_count": len(ready),
            "labeled_ready_count": labeled_ready_count,
            "blocked_ready_count": blocked_ready_count,
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
            "labeled_ready_count": labeled_ready_count,
            "blocked_ready_count": blocked_ready_count,
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
        "labeled_ready_count": labeled_ready_count,
        "blocked_ready_count": blocked_ready_count,
        "active": active,
        "cap": cap,
        "dispatched": dispatched,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
