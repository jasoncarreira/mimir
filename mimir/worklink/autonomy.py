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
from datetime import timedelta
import os
from pathlib import Path
import subprocess
from typing import Sequence

from .backends import WorklinkConfig
from .backends.registry import WorklinkDefaults
from .claims import ChainlinkClaims, ClaimRecord

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

    ``MIMIR_WORKLINK_REPO`` is accepted as a back-compat alias. Falls back to
    the process cwd only when neither is set (operator-CLI-style invocation from
    inside the repo). The in-turn ``worklink_run`` tool MUST use this rather
    than cwd so a standard install runs the executor against the configured repo.
    """
    return (
        os.environ.get("WORKLINK_REPO")
        or os.environ.get("MIMIR_WORKLINK_REPO")
        or os.getcwd()
    )


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

    ``allowed`` is ``active < cap``. Per-issue exclusivity is a separate,
    stronger guarantee enforced by ``chainlink locks claim`` inside
    :meth:`ChainlinkClaims.claim_issue` — this cap only bounds the *total*
    number of concurrent autonomous workers.
    """
    cap = worklink_defaults(home).max_concurrent
    cl = claims or make_claims(home, agent_id=agent_id)
    active = cl.active_claim_count()
    return ConcurrencyCheck(allowed=active < cap, active=active, cap=cap)


def reap_stale_claims_for_home(
    home: Path,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    claims: ChainlinkClaims | None = None,
) -> list[ClaimRecord]:
    """TTL-reaper entry point: recover claims whose worker died.

    Reads ``reaper_ttl_s`` from worklink.yaml and delegates discovery +
    staleness to :meth:`ChainlinkClaims.reap_home`.
    """
    ttl = timedelta(seconds=worklink_defaults(home).reaper_ttl_s)
    cl = claims or make_claims(home, agent_id=agent_id)
    return cl.reap_home(ttl=ttl)
