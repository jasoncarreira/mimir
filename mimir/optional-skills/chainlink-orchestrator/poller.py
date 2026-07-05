#!/usr/bin/env python3
"""Worklink ready-queue poller (chainlink #444).

Discovers actionable ``worklink:ready`` leaves and dispatches up to the
concurrent-claim cap by invoking ``mimir worklink run <id>`` as a
**detached** subprocess. ``worklink:epic`` issues are recognized only to be
EXCLUDED from leaf dispatch (an epic is built by the opencode feature-factory,
not as a single leaf; #830) — the poller never dispatches them. It deliberately does
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
from dataclasses import dataclass
from pathlib import Path


def _ensure_mimir_import_path() -> None:
    """Let an installed optional-skill poller import the source checkout.

    Optional poller commands run as subprocesses from the installed skill dir, so
    ``sys.path[0]`` is the skill directory rather than the mimir source checkout.
    Prefer an explicit ``MIMIR_SOURCE_DIR`` supplied by the runtime; fall back to
    the common editable-source layout where the interpreter lives in
    ``<source>/.venv/bin``; finally accept mimirbot's container source path.
    Pip-installed deployments do not need this repair because ``mimir`` is already
    in site-packages.
    """

    exe = Path(sys.executable).resolve()
    venv_root = exe.parent.parent
    candidates = []
    if source_dir := os.environ.get("MIMIR_SOURCE_DIR"):
        candidates.append(Path(source_dir))
    if venv_root.name in {".venv", "venv"}:
        candidates.append(venv_root.parent)
    # Mimirbot-specific editable-source fallback: the deployed optional skill is
    # under /mimir-home/skills, while the source checkout is mounted here.
    candidates.append(Path("/workspace/mimir"))

    for candidate in candidates:
        if (candidate / "mimir" / "__init__.py").is_file():
            # Source checkout first, so ``import mimir`` resolves to the checked-out
            # code even when the poller is installed under <home>/skills.
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)

            # Production poller commands may run under system ``python3`` rather
            # than the mimir venv interpreter. In editable-source deployments the
            # checked-out repo's venv holds runtime deps such as PyYAML, so add
            # its site-packages too. This is a best-effort repair: pip-installed
            # deployments already have dependencies on sys.path, and missing venvs
            # simply fall through to the normal ImportError if deps are absent.
            venv = candidate / ".venv"
            if venv.is_dir():
                for site in sorted((venv / "lib").glob("python*/site-packages")):
                    site_path = str(site)
                    if site_path not in sys.path:
                        sys.path.append(site_path)
            return


_ensure_mimir_import_path()

from mimir.worklink.backends.registry import WorklinkConfig, WorklinkDefaults

POLLER_NAME = os.environ.get("POLLER_NAME", "worklink-ready-queue")
READY_LABEL = "worklink:ready"
EPIC_LABEL = "worklink:epic"


def _factory_epics_enabled() -> bool:
    """chainlink #834: opt-in routing of ``worklink:epic`` issues to the
    feature-factory driver. Default OFF — until set, epics are only excluded
    from leaf dispatch (the pre-#834 behavior)."""
    return os.environ.get("MIMIR_FACTORY_EPICS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
BLOCKED_LABEL = "worklink:blocked"
REVIEW_LABEL = "worklink:review"


@dataclass(frozen=True)
class IssueRecord:
    issue_id: int
    parent_id: int | None = None


@dataclass(frozen=True)
class DispatchItem:
    issue_id: int
    mode: str

    @property
    def command(self) -> str:
        # chainlink #834: epics route to the feature-factory driver
        # (``mimir worklink factory <id>``); leaves run as before.
        return "factory" if self.mode == "epic" else "run"


def _emit(record: dict) -> None:
    record.setdefault("poller", POLLER_NAME)
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def _chainlink_bin() -> str:
    return os.environ.get("CHAINLINK_BIN") or "chainlink"


def _active_lock_issue_ids(home: Path) -> set[int] | None:
    """Active Chainlink lock issue ids, or ``None`` if unreadable.

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
    ids: set[int] = set()
    if isinstance(locks, dict):
        iterable = locks.items()
    elif isinstance(locks, list):
        iterable = enumerate(locks)
    else:
        return None
    for key, value in iterable:
        raw = value.get("issue_id") if isinstance(value, dict) else None
        if raw is None:
            raw = key
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _issue_records_with_label(home: Path, label: str) -> list[IssueRecord] | None:
    """Open issues carrying ``label``, or ``None`` if the query failed.

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
    records: list[IssueRecord] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        raw = _issue_raw_id(item)
        try:
            issue_id = int(raw)
        except (TypeError, ValueError):
            continue
        parent_id = None
        if item.get("parent_id") is not None:
            try:
                parent_id = int(item["parent_id"])
            except (TypeError, ValueError):
                parent_id = None
        records.append(IssueRecord(issue_id=issue_id, parent_id=parent_id))
    return records


def _issue_raw_id(item: dict) -> object:
    raw = item.get("id")
    if raw is None:
        raw = item.get("number")
    return raw


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
        raw = _issue_raw_id(item)
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


def _worklink_dispatch_plan(
    home: Path, *, active_lock_ids: set[int]
) -> tuple[list[DispatchItem], int, int, int] | None:
    """Return dispatchable Worklink leaves plus ready/blocked/epic counts.

    Leaves require ``worklink:ready`` and Chainlink actionability. ``worklink:
    epic`` issues are recognized ONLY to EXCLUDE them (and their child leaves)
    from per-leaf dispatch — the in-mimir epic runner was removed (#830); epics
    are built by the opencode feature-factory, so the poller never dispatches
    them. The epic count is reported for observability only.
    """
    ready_records = _issue_records_with_label(home, READY_LABEL)
    epic_records = _issue_records_with_label(home, EPIC_LABEL)
    actionable_ids = _actionable_issue_ids(home)
    if (
        ready_records is None
        or epic_records is None
        or actionable_ids is None
    ):
        return None
    labeled = {record.issue_id for record in ready_records}
    # ``worklink:epic`` issues are NOT dispatched by the poller (#830 — an epic
    # is built by the opencode feature-factory, not as a leaf). They are
    # tracked here only to EXCLUDE them (and any child leaves) from per-leaf
    # dispatch, so a worklink:epic issue is never run as a single leaf.
    epics = {record.issue_id for record in epic_records}
    actionable = set(actionable_ids)
    dispatchable_leaves = sorted(
        record.issue_id
        for record in ready_records
        if record.issue_id in actionable
        and record.issue_id not in epics
        and record.parent_id not in epics
    )
    plan = [DispatchItem(issue_id=issue_id, mode="leaf") for issue_id in dispatchable_leaves]
    # chainlink #834: when opted in, an actionable worklink:epic (labeled both
    # worklink:ready and worklink:epic) is dispatched to the feature-factory
    # driver instead of merely excluded. Its child leaves stay excluded above —
    # the factory builds them internally.
    if _factory_epics_enabled():
        ready_ids = {record.issue_id for record in ready_records}
        dispatchable_epics = sorted(
            issue_id for issue_id in epics if issue_id in actionable and issue_id in ready_ids
        )
        plan += [DispatchItem(issue_id=issue_id, mode="epic") for issue_id in dispatchable_epics]
    blocked_count = len(labeled - actionable)
    return plan, len(labeled), blocked_count, len(epics)


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

    active_lock_ids = _active_lock_issue_ids(home)
    # Fail closed: if we can't read either the ready queue, Chainlink's
    # actionable set, or the active lock count, do not dispatch (an undercounted
    # cap or blocked issue could over-admit workers).
    ready_result = (
        None
        if active_lock_ids is None
        else _worklink_dispatch_plan(home, active_lock_ids=active_lock_ids)
    )
    if ready_result is None or active_lock_ids is None:
        _emit({
            "signal": "worklink_poller_degraded",
            "reason": "chainlink ready/actionable/lock read failed; skipping dispatch this cycle",
        })
        return 0
    ready, labeled_ready_count, blocked_ready_count, actionable_epic_count = ready_result
    cap = _configured_cap(home)
    active = len(active_lock_ids)
    slots = max(0, cap - active)

    if not ready or slots == 0:
        _emit({
            "signal": "worklink_ready_scan",
            "ready_count": len(ready),
            "labeled_ready_count": labeled_ready_count,
            "blocked_ready_count": blocked_ready_count,
            "actionable_epic_count": actionable_epic_count,
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
            "actionable_epic_count": actionable_epic_count,
        })
        return 0

    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    state_dir = Path(os.environ.get("STATE_DIR") or (home / "state" / "pollers" / POLLER_NAME))
    state_dir.mkdir(parents=True, exist_ok=True)

    dispatched = 0
    for item in ready[:slots]:
        issue_id = item.issue_id
        argv = [*run_bin, "worklink", item.command, str(issue_id),
                "--home", str(home), "--repo", repo, "--autonomous"]
        log_path = state_dir / f"{item.command}-{issue_id}.log"
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
            "mode": item.mode,
            "log": str(log_path),
            "active_before": active,
            "cap": cap,
        })

    _emit({
        "signal": "worklink_ready_scan",
        "ready_count": len(ready),
        "labeled_ready_count": labeled_ready_count,
        "blocked_ready_count": blocked_ready_count,
        "actionable_epic_count": actionable_epic_count,
        "active": active,
        "cap": cap,
        "dispatched": dispatched,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
