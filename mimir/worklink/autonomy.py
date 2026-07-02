"""Slice-3 autonomy helpers for Worklink (chainlink #444).

The deterministic executor (``orchestrator.py``) and claim protocol
(``claims.py``) stay backend-shaped and operator-invokable. This module adds
the thin *autonomous-dispatch* layer they don't need on their own:

* the concurrent-claim cap (``worklink:in-progress`` count vs
  ``defaults.max_concurrent``), enforced by both the in-turn ``worklink_run``
  tool and the ready-queue poller before they start new work;
* the TTL-reaper entry point the scheduler callable runs to recover claims
  whose worker died (delegates to the already-tested
  :meth:`ChainlinkClaims.reap_home`);
* small config reads (autonomous priority, cap, reaper TTL) from
  ``<home>/worklink.yaml``.

Arbiter gating (``HomeostaticArbiter.should_fire``) lives at the call sites
that can reach an arbiter — the ``worklink_run`` tool and the scheduler's
poller-fire path — not here, so this module stays import-light and trivially
testable with a fake chainlink runner. The operator CLI deliberately uses
neither the cap nor the arbiter: ``mimir worklink run`` always proceeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shlex
import subprocess
from typing import Sequence

from .backends import WorklinkConfig
from .backends.registry import WorklinkDefaults
from .claims import ChainlinkClaims, ClaimRecord
from .epic_state import load_epic_state
from .worktree import prune_attempt_worktrees

#: Chainlink agent identity the executor + reaper claim under. Mirrors
#: ``WorklinkRunner.agent_id`` so reaped/dispatched records line up.
DEFAULT_AGENT_ID = "mimir-worklink"


def chainlink_bin() -> str:
    """Resolve the chainlink binary (env override, else ``chainlink`` on PATH)."""
    return os.environ.get("CHAINLINK_BIN") or "chainlink"


def _home_runner(home: Path):
    """A chainlink runner pinned to the home dir (the Chainlink repo cwd)."""

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=str(home), capture_output=True, text=True, check=False,
        )

    return run


def worklink_defaults(home: Path) -> WorklinkDefaults:
    """Load ``<home>/worklink.yaml`` defaults (or the dataclass defaults)."""
    return WorklinkConfig.load(home / "worklink.yaml").defaults


def worklink_priority(home: Path) -> str:
    """Autonomous-dispatch arbiter priority from worklink.yaml (default normal)."""
    return worklink_defaults(home).priority


def worklink_repo() -> str:
    """Resolve the git repo the backend works in, consistently with the
    ready-queue poller / opt-in skill, which expose ``WORKLINK_REPO``.

    ``MIMIR_WORKLINK_REPO`` is accepted as a back-compat alias. Autonomous
    dispatch must be explicit: falling back to the server process cwd can run
    Worklink against an unintended checkout. The operator CLI has its own
    ``--repo`` defaulting behavior and does not use this helper.
    """
    repo = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    if not repo:
        raise RuntimeError("WORKLINK_REPO is required for autonomous Worklink dispatch")
    return repo


def make_claims(home: Path, *, agent_id: str = DEFAULT_AGENT_ID) -> ChainlinkClaims:
    return ChainlinkClaims(
        chainlink_bin=chainlink_bin(),
        agent_id=agent_id,
        runner=_home_runner(home),
    )


@dataclass(frozen=True)
class ConcurrencyCheck:
    allowed: bool
    active: int
    cap: int

    @property
    def reason(self) -> str:
        if self.allowed:
            return f"{self.active}/{self.cap} active claims"
        return f"concurrency cap reached ({self.active}/{self.cap} active claims)"


def check_concurrency(
    home: Path,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    claims: ChainlinkClaims | None = None,
) -> ConcurrencyCheck:
    """Whether autonomous dispatch may start one more leaf right now.

    ``allowed`` is ``active < cap`` where ``active`` is the active Chainlink
    lock count. Per-issue exclusivity and the final hard-cap reservation are
    both enforced by ``chainlink locks claim`` inside
    :meth:`ChainlinkClaims.claim_issue`; this preflight is advisory/fail-closed
    so callers can skip work before entering the executor when the cap is
    already full.
    """
    cap = worklink_defaults(home).max_concurrent
    cl = claims or make_claims(home, agent_id=agent_id)
    active = cl.active_worklink_lock_count()
    return ConcurrencyCheck(allowed=active < cap, active=active, cap=cap)



def prune_stale_attempt_worktrees_for_home(home: Path, *, repo: Path | str | None = None) -> list[Path]:
    """Prune retained Worklink attempt checkouts past the reaper TTL (#613).

    The claim reaper recovers Chainlink labels/locks, but failed or blocked
    local attempts intentionally leave their checkout on disk for autopsy.  Run
    the filesystem prune on the same TTL so retained attempts do not grow
    without bound.  If no Worklink repo is configured, return silently; homes can
    opt into claim reaping before they opt into autonomous dispatch.
    """
    defaults = worklink_defaults(home)
    repo_raw = repo or os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    if not repo_raw:
        return []
    return prune_attempt_worktrees(
        Path(repo_raw),
        older_than=timedelta(seconds=defaults.reaper_ttl_s),
        now=datetime.now(timezone.utc),
    )


def dispatch_detached_epic_resume(*, home: Path, repo: Path, epic_id: int) -> Path:
    """Launch manifest-backed epic recovery as a detached CLI subprocess.

    Mirrors the ready-queue poller's detached ``mimir worklink run`` shape: the
    reaper has already stolen the stale parent lock, so it should record the
    recovery comment and return instead of running a whole epic inline.
    """
    run_bin = shlex.split(os.environ.get("WORKLINK_RUN_BIN") or "mimir")
    state_dir = home / "state" / "worklink" / "epic-reaper"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / f"run-epic-{epic_id}.log"
    argv = [
        *run_bin,
        "worklink",
        "run-epic",
        str(epic_id),
        "--home",
        str(home),
        "--repo",
        str(repo),
        "--autonomous",
    ]
    try:
        log_fh = log_path.open("ab")
    except OSError:
        log_fh = subprocess.DEVNULL
    try:
        subprocess.Popen(
            argv,
            cwd=str(repo),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    finally:
        if log_fh not in (subprocess.DEVNULL, None):
            try:
                log_fh.close()
            except OSError:
                pass
    return log_path


def reap_stale_claims_for_home(
    home: Path,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    claims: ChainlinkClaims | None = None,
) -> list[ClaimRecord]:
    """TTL-reaper entry point: recover claims whose worker died.

    Reads ``reaper_ttl_s`` from worklink.yaml and delegates discovery +
    staleness to :meth:`ChainlinkClaims.reap_home`. Epic-parent recovery
    dispatches a detached ``mimir worklink run-epic`` process and returns
    immediately, keeping the scheduler reaper fast enough to continue leaf
    reaping in the same tick.
    """
    defaults = worklink_defaults(home)
    min_reaper_ttl_s = defaults.timeout_s * 2
    if defaults.reaper_ttl_s <= min_reaper_ttl_s:
        raise RuntimeError(
            "worklink reaper_ttl_s must be greater than 2 * timeout_s so the TTL "
            "reaper cannot steal a worker that is still finalizing its remote "
            "test job"
        )
    ttl = timedelta(seconds=defaults.reaper_ttl_s)
    cl = claims or make_claims(home, agent_id=agent_id)
    reaped: list[ClaimRecord] = []

    def recover_epic(record: ClaimRecord) -> bool:
        if load_epic_state(home, record.issue_id) is None:
            return False
        dispatch_detached_epic_resume(home=home, repo=Path(worklink_repo()), epic_id=record.issue_id)
        return True

    reaped.extend(cl.reap_epic_home(ttl=ttl, recover=recover_epic))
    reaped.extend(cl.reap_home(ttl=ttl))
    return reaped
