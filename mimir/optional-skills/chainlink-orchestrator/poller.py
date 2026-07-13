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

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

from mimir._atomic import atomic_write_json
from mimir.worklink.backends import feature_factory as ff
from mimir.worklink.backends.feature_factory import (
    FactoryRunState,
    FactoryTerminalResult,
    epic_run_id,
    factory_run_dir,
    read_factory_run_state,
)
from mimir.worklink.backends.registry import WorklinkConfig, WorklinkDefaults

POLLER_NAME = os.environ.get("POLLER_NAME", "worklink-ready-queue")
LIFECYCLE_RECONCILIATION_NAME = "worklink-lifecycle-reconciliation"
READY_LABEL = "worklink:ready"
EPIC_LABEL = "worklink:epic"

WORKLINK_DIR = ".worklink"
FACTORY_DIR = ".opencode/factory"
RUN_JSON = "run.json"

CURSOR_FILE = "lifecycle_cursor.json"
CURSOR_VERSION = 1

LIVENESS_CLASSES = frozenset({"healthy", "stale", "unknown"})
VALIDITY_CLASSES = frozenset({"valid", "invalid", "unreadable"})
ACTIONABLE_STATUSES = frozenset({"blocked", "partial", "needs-human", "invalid", "stale"})


def _factory_epics_enabled() -> bool:
    """Opt-in routing of ``worklink:epic`` issues to the feature-factory backend
    (``mimir worklink run-epic``, chainlink #833). Default OFF: until
    ``MIMIR_FACTORY_EPICS_ENABLED`` is set, epics are only excluded from leaf
    dispatch and are never dispatched — so a deployment that hasn't opted in
    doesn't dispatch-then-refuse (``run-epic`` refuses non-autonomous-safe
    compute) every cycle. The capability-based ``autonomous_compute_allowed``
    policy is the second, hard safety layer once this is on."""
    return os.environ.get("MIMIR_FACTORY_EPICS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


BLOCKED_LABEL = "worklink:blocked"
REVIEW_LABEL = "worklink:review"

CLEANUP_RETENTION_DAYS = 30


@dataclass(frozen=True)
class FactoryRunObservation:
    run_id: str
    issue_id: int
    attempt: int | None
    physical_path: Path
    status: str
    pr_url: str | None
    reason: str | None
    summary: str | None
    pending_gate: str | None
    is_terminal: bool
    is_stale: bool
    validator_verdict: str | None
    security_verdict: str | None
    terminal_result: FactoryTerminalResult | None
    fingerprint: str = ""
    liveness_class: str = "unknown"
    validity_class: str = "unknown"

    def __post_init__(self) -> None:
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", self._compute_fingerprint())

    def _compute_fingerprint(self) -> str:
        parts = [
            self.run_id,
            self.status,
            self.reason or "",
            self.summary or "",
            self.pr_url or "",
            self.pending_gate or "",
            self.liveness_class,
            self.validity_class,
            self.validator_verdict or "",
            self.security_verdict or "",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class CursorEntry:
    run_id: str
    issue_id: int
    attempt: int | None
    physical_path: str
    fingerprint: str
    last_observed: str
    alerted: bool
    alerted_at: str | None
    cleaned: bool = False
    cleaned_at: str | None = None
    tombstone: bool = False


@dataclass
class LifecycleCursor:
    version: int = CURSOR_VERSION
    entries: dict[str, CursorEntry] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class LifecycleAlert:
    source_id: str
    signal: str
    run_id: str
    issue_id: int
    attempt: int | None
    physical_path: str
    prior_fingerprint: str | None
    current_fingerprint: str
    status: str
    prior_status: str | None
    reason: str | None
    pr_url: str | None
    pending_gate: str | None
    liveness_class: str
    validity_class: str
    validator_verdict: str | None
    security_verdict: str | None
    cleanup_eligible: bool
    routing_instructions: str


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
        if self.mode == "epic":
            return "run-epic"
        return "run"


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
    """Return dispatchable Worklink leaves and epics plus ready/blocked/epic counts.

    Leaves require ``worklink:ready`` and Chainlink actionability. ``worklink:
    epic`` issues are dispatched to the feature-factory adapter (#833) — the
    poller starts/resumes the opencode feature-factory which reads the factory's
    run.json and mirrors progress/gates/PR/terminal state back to Chainlink.

    Epics require ``worklink:ready`` + ``worklink:epic`` labels + Chainlink
    actionability. The epic count is reported for observability.
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
    epics = {record.issue_id for record in epic_records}
    actionable = set(actionable_ids)
    dispatchable_leaves = sorted(
        record.issue_id
        for record in ready_records
        if record.issue_id in actionable
        and record.issue_id not in epics
        and record.parent_id not in epics
    )
    dispatchable_epics = sorted(
        record.issue_id
        for record in epic_records
        if record.issue_id in actionable
    )
    plan = [DispatchItem(issue_id=issue_id, mode="leaf") for issue_id in dispatchable_leaves]
    if _factory_epics_enabled():
        plan.extend(DispatchItem(issue_id=issue_id, mode="epic") for issue_id in dispatchable_epics)
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


def _run_id_to_issue_attempt(run_id: str) -> tuple[int, int | None]:
    parts = run_id.removeprefix("chainlink-").split("-")
    if not parts:
        return 0, None
    try:
        issue_id = int(parts[0])
    except ValueError:
        return 0, None
    attempt = None
    if len(parts) > 1 and parts[-1].startswith("attempt"):
        try:
            attempt = int(parts[-1].removeprefix("attempt"))
        except ValueError:
            pass
    return issue_id, attempt


def _discover_factory_runs(repo: Path) -> list[tuple[Path, Path, str]]:
    discovered: list[tuple[Path, Path, str]] = []
    worklink_root = repo / WORKLINK_DIR
    worktree_roots = [repo]
    if worklink_root.is_dir():
        try:
            for attempt_dir in worklink_root.iterdir():
                if attempt_dir.is_dir():
                    worktree_roots.append(attempt_dir)
        except OSError:
            pass
    for worktree_root in worktree_roots:
        factory_dir = worktree_root / FACTORY_DIR
        if not factory_dir.is_dir():
            continue
        try:
            for run_dir in factory_dir.iterdir():
                if run_dir.is_dir() and (run_dir / RUN_JSON).exists():
                    discovered.append((worktree_root, run_dir, run_dir.name))
        except OSError:
            pass
    return discovered


def _observe_factory_run(worktree_root: Path, run_id: str) -> FactoryRunObservation | None:
    state = read_factory_run_state(worktree_root, run_id)
    if state is None:
        return None
    run_dir = factory_run_dir(worktree_root, run_id)
    if state is None:
        return None
    issue_id, attempt = _run_id_to_issue_attempt(run_id)
    liveness_class = "unknown"
    if state.is_stale:
        liveness_class = "stale"
    elif state.status.strip().lower() == "running":
        liveness_class = "healthy"
    validity_class = "valid"
    if state.error or not state.run_id:
        validity_class = "invalid"
    reason = state.error
    if state.terminal_result and state.terminal_result.reason:
        reason = state.terminal_result.reason
    summary = None
    if state.terminal_result and state.terminal_result.summary:
        summary = state.terminal_result.summary
    pr_url = state.pr_url
    if state.terminal_result and state.terminal_result.pr_url:
        pr_url = state.terminal_result.pr_url
    return FactoryRunObservation(
        run_id=run_id,
        issue_id=issue_id,
        attempt=attempt,
        physical_path=run_dir,
        status=state.status,
        pr_url=pr_url,
        reason=reason,
        summary=summary,
        pending_gate=state.pending_gate,
        is_terminal=state.is_terminal,
        is_stale=state.is_stale,
        validator_verdict=state.validator_verdict,
        security_verdict=state.security_verdict,
        terminal_result=state.terminal_result,
        liveness_class=liveness_class,
        validity_class=validity_class,
    )


def _load_cursor(state_dir: Path) -> LifecycleCursor:
    cursor_path = state_dir / CURSOR_FILE
    if not cursor_path.exists():
        return LifecycleCursor()
    try:
        data = json.loads(cursor_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return LifecycleCursor()
        version = data.get("version")
        if version != CURSOR_VERSION:
            return LifecycleCursor()
        entries = {}
        for key, entry_data in data.get("entries", {}).items():
            if not isinstance(entry_data, dict):
                continue
            try:
                entries[key] = CursorEntry(
                    run_id=entry_data.get("run_id", ""),
                    issue_id=entry_data.get("issue_id", 0),
                    attempt=entry_data.get("attempt"),
                    physical_path=entry_data.get("physical_path", ""),
                    fingerprint=entry_data.get("fingerprint", ""),
                    last_observed=entry_data.get("last_observed", ""),
                    alerted=entry_data.get("alerted", False),
                    alerted_at=entry_data.get("alerted_at"),
                    cleaned=entry_data.get("cleaned", False),
                    cleaned_at=entry_data.get("cleaned_at"),
                    tombstone=entry_data.get("tombstone", False),
                )
            except (TypeError, ValueError):
                continue
        return LifecycleCursor(
            version=version,
            entries=entries,
            updated_at=data.get("updated_at", datetime.now(UTC).isoformat()),
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return LifecycleCursor()


def _save_cursor(state_dir: Path, cursor: LifecycleCursor) -> None:
    data = {
        "version": cursor.version,
        "entries": {
            key: {
                "run_id": entry.run_id,
                "issue_id": entry.issue_id,
                "attempt": entry.attempt,
                "physical_path": entry.physical_path,
                "fingerprint": entry.fingerprint,
                "last_observed": entry.last_observed,
                "alerted": entry.alerted,
                "alerted_at": entry.alerted_at,
                "cleaned": entry.cleaned,
                "cleaned_at": entry.cleaned_at,
                "tombstone": entry.tombstone,
            }
            for key, entry in cursor.entries.items()
        },
        "updated_at": cursor.updated_at,
    }
    atomic_write_json(state_dir / CURSOR_FILE, data)


def _is_pr_merged(pr_url: str | None, repo: Path) -> bool:
    if not pr_url:
        return False
    match = re.search(r"/pull/(\d+)", pr_url)
    if not match:
        return False
    pr_number = match.group(1)
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_number, "--state", "--json", "state", "-t", "{{.state}}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "merged"


def _run_factory_cleanup(
    worktree: Path, dry_run: bool = True
) -> tuple[bool, str, list[dict] | None]:
    cmd = ["factory", "cleanup", "--all"]
    if dry_run:
        cmd.append("--dry-run")
    cmd.append("--json")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc), None
    if proc.returncode != 0:
        return False, proc.stderr or proc.stdout, None
    try:
        digest = json.loads(proc.stdout)
        if not isinstance(digest, list):
            return False, "invalid digest format", None
        if dry_run:
            return True, "dry-run success", digest
        return True, "cleanup executed", None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return False, str(exc), None


def _reconcile_factory_runs(
    repo: Path, state_dir: Path
) -> tuple[list[LifecycleAlert], list[LifecycleAlert]]:
    observed = _discover_factory_runs(repo)
    observations: list[FactoryRunObservation] = []
    for worktree_root, run_dir, run_id in observed:
        obs = _observe_factory_run(worktree_root, run_id)
        if obs:
            observations.append(obs)
    cursor = _load_cursor(state_dir)
    now = datetime.now(UTC).isoformat()
    new_alerts: list[LifecycleAlert] = []
    cleanup_alerts: list[LifecycleAlert] = []
    updated_entries: dict[str, CursorEntry] = dict(cursor.entries)
    for obs in observations:
        key = f"{obs.run_id}:{obs.physical_path}"
        prior = cursor.entries.get(key)
        prior_fingerprint = prior.fingerprint if prior else None
        prior_status = None
        if prior:
            prior_obs_data = prior.fingerprint
            if prior_obs_data:
                prior_status = "unknown"
        cleanup_eligible = (
            obs.is_terminal
            and obs.status.strip().lower() == "completed"
            and obs.pr_url
            and _is_pr_merged(obs.pr_url, repo)
        )
        is_actionable = (
            obs.status.strip().lower() in ACTIONABLE_STATUSES
            or obs.validity_class == "invalid"
            or obs.liveness_class == "stale"
        )
        should_alert = False
        if not prior:
            if is_actionable:
                should_alert = True
        elif prior.tombstone:
            continue
        elif prior.fingerprint != obs.fingerprint:
            if is_actionable:
                should_alert = True
            elif prior.alerted and not is_actionable:
                pass
        elif prior.alerted and is_actionable:
            should_alert = True
        if should_alert:
            status = obs.status.strip().lower()
            routing = "Inspect and perform only safe/reversible remediation."
            if cleanup_eligible:
                routing += " This run is eligible for cleanup."
            if is_actionable and status in ("blocked", "partial", "needs-human"):
                routing += (
                    " If credentials, an operator decision, destructive/force cleanup, "
                    "or other operator authority is required, contact Jason with the exact blocker."
                )
            source_id = f"lifecycle:{obs.run_id}:{obs.physical_path}"
            alert = LifecycleAlert(
                source_id=source_id,
                signal="worklink_factory_actionable",
                run_id=obs.run_id,
                issue_id=obs.issue_id,
                attempt=obs.attempt,
                physical_path=str(obs.physical_path),
                prior_fingerprint=prior_fingerprint,
                current_fingerprint=obs.fingerprint,
                status=obs.status,
                prior_status=prior_status,
                reason=obs.reason,
                pr_url=obs.pr_url,
                pending_gate=obs.pending_gate,
                liveness_class=obs.liveness_class,
                validity_class=obs.validity_class,
                validator_verdict=obs.validator_verdict,
                security_verdict=obs.security_verdict,
                cleanup_eligible=cleanup_eligible,
                routing_instructions=routing,
            )
            new_alerts.append(alert)
            if cleanup_eligible:
                cleanup_alerts.append(alert)
        entry = CursorEntry(
            run_id=obs.run_id,
            issue_id=obs.issue_id,
            attempt=obs.attempt,
            physical_path=str(obs.physical_path),
            fingerprint=obs.fingerprint,
            last_observed=now,
            alerted=should_alert,
            alerted_at=now if should_alert else (prior.alerted_at if prior else None),
            cleaned=False,
            cleaned_at=None,
            tombstone=False,
        )
        updated_entries[key] = entry
    new_cursor = LifecycleCursor(
        version=CURSOR_VERSION,
        entries=updated_entries,
        updated_at=now,
    )
    _save_cursor(state_dir, new_cursor)
    return new_alerts, cleanup_alerts


def _attempt_cleanup(
    repo: Path, state_dir: Path, cleanup_alerts: list[LifecycleAlert]
) -> list[LifecycleAlert]:
    failed_alerts: list[LifecycleAlert] = []
    cursor = _load_cursor(state_dir)
    now = datetime.now(UTC).isoformat()
    updated_entries = dict(cursor.entries)
    for alert in cleanup_alerts:
        key = f"{alert.run_id}:{alert.physical_path}"
        entry = cursor.entries.get(key)
        if not entry or entry.cleaned:
            continue
        worktree = Path(alert.physical_path)
        if not worktree.exists():
            updated_entries[key] = CursorEntry(
                run_id=entry.run_id,
                issue_id=entry.issue_id,
                attempt=entry.attempt,
                physical_path=entry.physical_path,
                fingerprint=entry.fingerprint,
                last_observed=now,
                alerted=False,
                alerted_at=entry.alerted_at,
                cleaned=True,
                cleaned_at=now,
                tombstone=True,
            )
            continue
        success, msg, digest = _run_factory_cleanup(worktree, dry_run=True)
        if not success:
            failed_alerts.append(alert)
            continue
        if not digest:
            updated_entries[key] = CursorEntry(
                run_id=entry.run_id,
                issue_id=entry.issue_id,
                attempt=entry.attempt,
                physical_path=entry.physical_path,
                fingerprint=entry.fingerprint,
                last_observed=now,
                alerted=False,
                alerted_at=entry.alerted_at,
                cleaned=True,
                cleaned_at=now,
                tombstone=True,
            )
            continue
        success, msg, _ = _run_factory_cleanup(worktree, dry_run=False)
        if success:
            updated_entries[key] = CursorEntry(
                run_id=entry.run_id,
                issue_id=entry.issue_id,
                attempt=entry.attempt,
                physical_path=entry.physical_path,
                fingerprint=entry.fingerprint,
                last_observed=now,
                alerted=False,
                alerted_at=entry.alerted_at,
                cleaned=True,
                cleaned_at=now,
                tombstone=True,
            )
        else:
            failed_alerts.append(alert)
    new_cursor = LifecycleCursor(
        version=CURSOR_VERSION,
        entries=updated_entries,
        updated_at=now,
    )
    _save_cursor(state_dir, new_cursor)
    return failed_alerts


def _run_lifecycle_reconciliation(
    repo: Path, state_dir: Path
) -> list[LifecycleAlert]:
    all_alerts: list[LifecycleAlert] = []
    new_alerts, cleanup_alerts = _reconcile_factory_runs(repo, state_dir)
    all_alerts.extend(new_alerts)
    if cleanup_alerts:
        failed_cleanup = _attempt_cleanup(repo, state_dir, cleanup_alerts)
        for alert in new_alerts:
            if alert not in cleanup_alerts:
                all_alerts.append(alert)
        for alert in failed_cleanup:
            all_alerts.append(
                LifecycleAlert(
                    source_id=alert.source_id + ":cleanup_failed",
                    signal="worklink_factory_cleanup_failed",
                    run_id=alert.run_id,
                    issue_id=alert.issue_id,
                    attempt=alert.attempt,
                    physical_path=alert.physical_path,
                    prior_fingerprint=alert.prior_fingerprint,
                    current_fingerprint=alert.current_fingerprint,
                    status=alert.status,
                    prior_status=alert.prior_status,
                    reason=f"Cleanup failed: {alert.reason}",
                    pr_url=alert.pr_url,
                    pending_gate=alert.pending_gate,
                    liveness_class=alert.liveness_class,
                    validity_class=alert.validity_class,
                    validator_verdict=alert.validator_verdict,
                    security_verdict=alert.security_verdict,
                    cleanup_eligible=False,
                    routing_instructions=(
                        "Cleanup failed. Contact Jason with the exact blocker. "
                        "Do NOT use --force, direct filesystem deletion, or direct branch deletion."
                    ),
                )
            )
    return all_alerts


def main() -> int:
    home_env = os.environ.get("MIMIR_HOME")
    if not home_env:
        _emit({"signal": "worklink_poller_misconfigured", "reason": "MIMIR_HOME unset"})
        return 0
    home = Path(home_env)

    repo = os.environ.get("WORKLINK_REPO")
    state_dir = Path(os.environ.get("STATE_DIR") or (home / "state" / "pollers" / POLLER_NAME))
    state_dir.mkdir(parents=True, exist_ok=True)

    if repo:
        try:
            lifecycle_alerts = _run_lifecycle_reconciliation(Path(repo), state_dir)
            for alert in lifecycle_alerts:
                _emit({
                    "signal": alert.signal,
                    "source_id": alert.source_id,
                    "run_id": alert.run_id,
                    "issue_id": alert.issue_id,
                    "attempt": alert.attempt,
                    "physical_path": alert.physical_path,
                    "prior_fingerprint": alert.prior_fingerprint,
                    "current_fingerprint": alert.current_fingerprint,
                    "status": alert.status,
                    "prior_status": alert.prior_status,
                    "reason": alert.reason,
                    "pr_url": alert.pr_url,
                    "pending_gate": alert.pending_gate,
                    "liveness_class": alert.liveness_class,
                    "validity_class": alert.validity_class,
                    "validator_verdict": alert.validator_verdict,
                    "security_verdict": alert.security_verdict,
                    "cleanup_eligible": alert.cleanup_eligible,
                    "routing_instructions": alert.routing_instructions,
                })
        except Exception as exc:
            _emit({
                "signal": "worklink_lifecycle_reconciliation_error",
                "reason": str(exc),
            })

    active_lock_ids = _active_lock_issue_ids(home)
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
