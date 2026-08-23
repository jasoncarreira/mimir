#!/usr/bin/env python3
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
    exe = Path(sys.executable).resolve()
    venv_root = exe.parent.parent
    script_path = globals().get("__file__")
    candidates = [Path(script_path).resolve().parents[4]] if script_path else []
    if source_dir := os.environ.get("MIMIR_SOURCE_DIR"):
        candidates.append(Path(source_dir))
    if venv_root.name in {".venv", "venv"}:
        candidates.append(venv_root.parent)
    candidates.append(Path("/workspace/mimir"))
    for candidate in candidates:
        if not (candidate / "mimir" / "__init__.py").is_file():
            continue
        path = str(candidate)
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
        venv = candidate / ".venv"
        if venv.is_dir():
            for site in sorted((venv / "lib").glob("python*/site-packages")):
                site_path = str(site)
                if site_path not in sys.path:
                    sys.path.append(site_path)
        return


_ensure_mimir_import_path()

from mimir.coding import coding_enabled
from mimir.worklink.autonomy import factory_max_concurrent
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.continuation import consume_worklink_budget_continuations
from mimir.worklink.dispatch_failures import (
    POLLER_NAME,
    delivery_receipt_exists,
    dispatch_failure_state_dir,
    mark_failure_notified,
    pending_failure_alerts,
)


READY_LABEL = "worklink:ready"
EPIC_LABEL = "worklink:epic"
BLOCKED_LABEL = "worklink:blocked"
_CHAINLINK_READ_TIMEOUT_SECONDS = 5


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
        return "run-epic" if self.mode == "epic" else "run"


def _emit(record: dict) -> None:
    record.setdefault("poller", POLLER_NAME)
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def _chainlink_bin() -> str:
    return os.environ.get("CHAINLINK_BIN") or "chainlink"


def _factory_epics_enabled() -> bool:
    return os.environ.get("MIMIR_FACTORY_EPICS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _active_lock_issue_ids(home: Path) -> set[int] | None:
    try:
        proc = subprocess.run(
            [_chainlink_bin(), "locks", "list", "--json"],
            cwd=str(home),
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHAINLINK_READ_TIMEOUT_SECONDS,
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
    if isinstance(locks, dict):
        iterable = locks.items()
    elif isinstance(locks, list):
        iterable = enumerate(locks)
    else:
        return None
    ids: set[int] = set()
    for key, value in iterable:
        raw = value.get("issue_id") if isinstance(value, dict) else None
        if raw is None:
            raw = key
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            return None
    return ids


def _issue_raw_id(item: dict) -> object:
    return item.get("id", item.get("number"))


def _issue_records_with_label(home: Path, label: str) -> list[IssueRecord] | None:
    try:
        proc = subprocess.run(
            [
                _chainlink_bin(),
                "issue",
                "list",
                "--label",
                label,
                "--status",
                "open",
                "--json",
            ],
            cwd=str(home),
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHAINLINK_READ_TIMEOUT_SECONDS,
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
    if not isinstance(issues, list):
        return None
    records: list[IssueRecord] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        try:
            issue_id = int(_issue_raw_id(item))
        except (TypeError, ValueError):
            continue
        try:
            parent_id = int(item["parent_id"]) if item.get("parent_id") is not None else None
        except (TypeError, ValueError):
            parent_id = None
        records.append(IssueRecord(issue_id, parent_id))
    return records


def _issue_ids_from_records(data: object) -> list[int]:
    issues = data if isinstance(data, list) else data.get("issues", []) if isinstance(data, dict) else []
    ids: list[int] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        try:
            ids.append(int(_issue_raw_id(item)))
        except (TypeError, ValueError):
            continue
    return ids


def _issue_ids_from_ready_text(text: str) -> list[int]:
    ids: list[int] = []
    for line in text.splitlines():
        match = re.match(r"^\s*#(\d+)\b", line)
        if match:
            ids.append(int(match.group(1)))
    return ids


def _actionable_issue_ids(home: Path) -> list[int] | None:
    try:
        proc = subprocess.run(
            [_chainlink_bin(), "issue", "ready", "--json"],
            cwd=str(home),
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHAINLINK_READ_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return _issue_ids_from_records(json.loads(proc.stdout or "[]"))
    except json.JSONDecodeError:
        parsed = _issue_ids_from_ready_text(proc.stdout or "")
        return parsed or None


def _worklink_dispatch_plan(
    home: Path, *, active_lock_ids: set[int]
) -> tuple[list[DispatchItem], int, int, int, set[int]] | None:
    ready_records = _issue_records_with_label(home, READY_LABEL)
    epic_records = _issue_records_with_label(home, EPIC_LABEL)
    blocked_records = _issue_records_with_label(home, BLOCKED_LABEL)
    actionable_ids = _actionable_issue_ids(home)
    if (
        ready_records is None
        or epic_records is None
        or blocked_records is None
        or actionable_ids is None
    ):
        return None
    labeled = {record.issue_id for record in ready_records}
    epics = {record.issue_id for record in epic_records}
    blocked = {record.issue_id for record in blocked_records}
    actionable = set(actionable_ids)
    # An issue holding an active lock is not a dispatch candidate. Slots are reduced by the
    # active count, so leaving it in the sorted candidates lets a low id consume the slot its
    # own worker already holds: the new worker loses the atomic claim and exits, and an
    # unlocked issue waits another cycle. Ready and actionable both stay true across claim
    # label transitions, so this is reachable rather than theoretical.
    leaves = sorted(
        record.issue_id
        for record in ready_records
        if record.issue_id in actionable
        and record.issue_id not in epics
        and record.parent_id not in epics
        and record.issue_id not in blocked
        and record.issue_id not in active_lock_ids
    )
    factory_epics = sorted(
        record.issue_id
        for record in epic_records
        if record.issue_id in labeled
        and record.issue_id in actionable
        and record.issue_id not in blocked
        and record.issue_id not in active_lock_ids
    )
    plan = [DispatchItem(issue_id, "leaf") for issue_id in leaves]
    if _factory_epics_enabled():
        plan.extend(DispatchItem(issue_id, "epic") for issue_id in factory_epics)
    return plan, len(labeled), len(labeled - actionable), len(labeled & blocked), epics


def _configured_cap(home: Path) -> int:
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


def _dispatch(
    *,
    item: DispatchItem,
    home: Path,
    repo: str,
    state_dir: Path,
    run_bin: list[str],
    active: int,
    leaf_cap: int,
    factory_cap: int,
) -> bool:
    effective_coding_enabled = coding_enabled()
    argv = [
        *run_bin,
        "worklink",
        item.command,
        str(item.issue_id),
        "--home",
        str(home),
        "--repo",
        repo,
        "--autonomous",
    ]
    log_path = state_dir / f"{item.command}-{item.issue_id}.log"
    try:
        log_fh = log_path.open("ab")
    except OSError:
        log_fh = subprocess.DEVNULL
    try:
        subprocess.Popen(
            argv,
            cwd=repo,
            env={
                **os.environ,
                "STATE_DIR": str(state_dir),
                "WORKLINK_RUN_LOG": str(log_path),
            },
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _emit(
            {
                "signal": "worklink_dispatch_failed",
                "issue_id": item.issue_id,
                "reason": str(exc),
                "coding_enabled": effective_coding_enabled,
            }
        )
        return False
    finally:
        if log_fh not in (subprocess.DEVNULL, None):
            try:
                log_fh.close()
            except OSError:
                pass
    _emit(
        {
            "signal": "worklink_dispatched",
            "issue_id": item.issue_id,
            "mode": item.mode,
            "log": str(log_path),
            "active_before": active,
            "cap": leaf_cap,
            "factory_cap": factory_cap,
            "coding_enabled": effective_coding_enabled,
        }
    )
    return True


def main() -> int:
    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        _emit({"signal": "worklink_poller_misconfigured", "reason": "MIMIR_HOME unset"})
        return 0
    home = Path(home_env)
    config_path = home / "worklink.yaml"
    try:
        BackendRegistry(WorklinkConfig.load(config_path))
    except Exception as exc:
        _emit(
            {
                "signal": "worklink_poller_misconfigured",
                "reason": f"invalid Worklink config {config_path}: {exc}",
            }
        )
        return 0
    repo = os.environ.get("WORKLINK_REPO")
    # Detached workers and this reader must resolve the ledger from the same trusted home.
    state_dir = dispatch_failure_state_dir(home)
    state_dir.mkdir(parents=True, exist_ok=True)
    emitted_delivery = False
    try:
        backed_off_ids, alerts = pending_failure_alerts(state_dir)
        for alert in alerts:
            delivery_key = (
                f"worklink-run-failure:{alert['issue_id']}:"
                f"{alert['error_signature']}:{alert['failure_occurrence_id']}"
            )
            if delivery_receipt_exists(state_dir, delivery_key):
                mark_failure_notified(
                    state_dir,
                    int(alert["issue_id"]),
                    str(alert["error_signature"]),
                    str(alert["failure_occurrence_id"]),
                )
                continue
            alert["delivery_key"] = delivery_key
            _emit(alert)
            emitted_delivery = True
    except OSError as exc:
        backed_off_ids = set()
        _emit({"signal": "worklink_dispatch_failure_state_error", "reason": str(exc)})
    if emitted_delivery:
        return 0
    try:
        continuation_actions = consume_worklink_budget_continuations(
            home,
            delivery_receipt_exists=lambda key: delivery_receipt_exists(state_dir, key),
        )
        for action in continuation_actions:
            _emit(
                {
                    "signal": "worklink_continuation",
                    "delivery_key": action.delivery_key,
                    "source_id": f"continuation:{action.idempotency_key}",
                    "issue_id": action.issue_id,
                    "pr_url": action.pr_url,
                    "occurrences": action.occurrences,
                    "sidecar": str(action.sidecar_path),
                    "routing_instructions": (
                        "Resume the unfinished Worklink item from its durable sidecar. "
                        "Check current issue, PR, and evidence state before taking action."
                    ),
                }
            )
        if continuation_actions:
            return 0
    except Exception as exc:
        _emit({"signal": "worklink_continuation_consumer_error", "reason": str(exc)})
    active_lock_ids = _active_lock_issue_ids(home)
    ready_result = (
        None
        if active_lock_ids is None
        else _worklink_dispatch_plan(home, active_lock_ids=active_lock_ids)
    )
    if ready_result is None or active_lock_ids is None:
        _emit(
            {
                "signal": "worklink_poller_degraded",
                "reason": "chainlink ready/actionable/lock read failed; skipping dispatch this cycle",
            }
        )
        return 0
    (
        ready,
        labeled_ready_count,
        blocked_ready_count,
        label_blocked_ready_count,
        epic_ids,
    ) = ready_result
    actionable_epic_count = len(epic_ids)
    dispatch_ready = [item for item in ready if item.issue_id not in backed_off_ids]
    leaf_cap = _configured_cap(home)
    factory_cap = factory_max_concurrent()
    active = len(active_lock_ids - epic_ids)
    factory_active = len(active_lock_ids & epic_ids)
    leaf_slots = max(0, leaf_cap - active)
    factory_slots = max(0, factory_cap - factory_active)
    leaves = [item for item in dispatch_ready if item.mode == "leaf"][:leaf_slots]
    epics = [item for item in dispatch_ready if item.mode == "epic"][:factory_slots]
    selected = [*leaves, *epics]
    if not selected or not repo:
        _emit(
            {
                "signal": "worklink_ready_scan" if repo else "worklink_poller_misconfigured",
                "reason": None if repo else "WORKLINK_REPO unset; cannot dispatch",
                "ready_count": len(ready),
                "labeled_ready_count": labeled_ready_count,
                "blocked_ready_count": blocked_ready_count,
                "label_blocked_ready_count": label_blocked_ready_count,
                "actionable_epic_count": actionable_epic_count,
                "active": active,
                "cap": leaf_cap,
                "slots": leaf_slots,
                "factory_active": factory_active,
                "factory_cap": factory_cap,
                "factory_slots": factory_slots,
                "backed_off": len(ready) - len(dispatch_ready),
            }
        )
        return 0
    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    dispatched = sum(
        _dispatch(
            item=item,
            home=home,
            repo=repo,
            state_dir=state_dir,
            run_bin=run_bin,
            active=active,
            leaf_cap=leaf_cap,
            factory_cap=factory_cap,
        )
        for item in selected
    )
    _emit(
        {
            "signal": "worklink_ready_scan",
            "ready_count": len(ready),
            "labeled_ready_count": labeled_ready_count,
            "blocked_ready_count": blocked_ready_count,
            "label_blocked_ready_count": label_blocked_ready_count,
            "actionable_epic_count": actionable_epic_count,
            "active": active,
            "cap": leaf_cap,
            "factory_active": factory_active,
            "factory_cap": factory_cap,
            "dispatched": dispatched,
            "backed_off": len(ready) - len(dispatch_ready),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
