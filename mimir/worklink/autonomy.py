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

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Callable, Sequence

from .backends import WorklinkConfig
from .backends.registry import WorklinkDefaults
from .claims import ChainlinkClaims, ClaimRecord
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
        home_path=home,
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



def _attempt_is_active(child: Path) -> bool:
    """True if a stale-by-mtime attempt checkout is actually still live and must
    NOT be reaped: a non-terminal factory ``run.json`` OR a detached factory
    process still working in the checkout.

    A long-running detached factory does its work in deep subdirs
    (``.opencode/factory/<run-id>/``, ``.opencode/worktrees/...``), so the
    attempt's top-level mtime freezes at setup and the reaper's mtime-only TTL
    would otherwise reap a live run mid-flight (removing its ``run.json`` and
    checkout). Errs toward keeping (returns True) when activity cannot be
    determined, so the reaper never nukes a possibly-live run.

    Deliberate tradeoff: treating ANY non-terminal ``run.json`` as active means a
    factory that CRASHED while leaving ``status: running`` is not auto-reaped by
    the TTL prune — it must be retired explicitly (``feature-factory factory
    cleanup --force`` or manual removal). This prioritizes never deleting live
    work over reclaiming disk from a rare leaked run; a heartbeat-freshness or
    process-liveness gate here could misfire during a legitimately quiet phase
    (the pre_pr review panel) and reintroduce the very mid-flight reap this
    guards against.
    """
    from .backends.feature_factory import epic_run_id, read_factory_run_state
    from .orchestrator import _detached_factory_alive

    try:
        issue_id = int(child.name.split("-", 1)[0])
        state = read_factory_run_state(child, epic_run_id(issue_id))
        if state is not None and not state.is_terminal:
            return True
        return _detached_factory_alive(child) is True
    except Exception:  # noqa: BLE001 - undeterminable activity must not cause a reap
        return True


def prune_stale_attempt_worktrees_for_home(home: Path, *, repo: Path | str | None = None) -> list[Path]:
    """Prune retained Worklink attempt checkouts past the reaper TTL (#613).

    The claim reaper recovers Chainlink labels/locks, but failed or blocked
    local attempts intentionally leave their checkout on disk for autopsy.  Run
    the filesystem prune on the same TTL so retained attempts do not grow
    without bound.  If no Worklink repo is configured, return silently; homes can
    opt into claim reaping before they opt into autonomous dispatch.

    Passes ``is_active`` so an attempt with a live detached factory (or a
    non-terminal ``run.json``) is skipped rather than reaped: a detached epic can
    run for longer than the TTL, and its top-level attempt-dir mtime freezes
    while it works in subdirs, so the mtime-only staleness test alone would
    delete a live run's checkout out from under it.
    """
    defaults = worklink_defaults(home)
    repo_raw = repo or os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    if not repo_raw:
        return []
    return prune_attempt_worktrees(
        Path(repo_raw),
        older_than=timedelta(seconds=defaults.reaper_ttl_s),
        now=datetime.now(timezone.utc),
        is_active=_attempt_is_active,
    )


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
    return cl.reap_home(ttl=ttl)


@dataclasses.dataclass(frozen=True)
class PrMergeState:
    """Result of checking a PR's merge state."""

    pr_url: str
    merged: bool
    merged_at: str | None = None
    merge_commit_sha: str | None = None


def _normalize_pr_url(value: str | None) -> str | None:
    """Normalize a PR URL to a canonical form."""
    if not value:
        return None
    match = re.match(
        r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
        value.strip(),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    owner, repo, number = match.groups()
    return f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{number}"


def _check_pr_merged_via_gh(pr_url: str, gh_bin: str = "gh") -> PrMergeState | None:
    """Check if a PR is merged using the gh CLI.

    Returns None on any error (fail-safe: never close on uncertainty).
    """
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, check=False)

    return _check_pr_merged_via_gh_runner(pr_url, runner=runner)


def _check_pr_merged_via_gh_runner(
    pr_url: str,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> PrMergeState | None:
    """Check if a PR is merged using the gh CLI (with injectable runner)."""
    normalized = _normalize_pr_url(pr_url)
    if not normalized:
        return None

    match = re.match(
        r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", normalized
    )
    if not match:
        return None

    repo, pr_number = match.groups()

    result = runner(["gh", "pr", "view", pr_number, "--repo", repo, "--json", "state,mergedAt,mergeCommit"])
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None

    state = data.get("state", "OPEN")
    merged = state == "MERGED"

    merged_at = data.get("mergedAt")
    if merged_at and merged_at != "null":
        merged_at = merged_at
    else:
        merged_at = None

    merge_commit = data.get("mergeCommit")
    merge_commit_sha = None
    if merge_commit and isinstance(merge_commit, dict):
        merge_commit_sha = merge_commit.get("oid")

    return PrMergeState(
        pr_url=normalized,
        merged=merged,
        merged_at=merged_at,
        merge_commit_sha=merge_commit_sha,
    )


def _find_latest_evidence_for_issue(home: Path, issue_id: int) -> dict | None:
    """Find the latest evidence file for an issue.

    Scans evidence directory for files matching <issue_id>-*.json and returns
    the content of the one with the highest attempt number.
    """
    evidence_dir = home / "state" / "worklink" / "evidence"
    if not evidence_dir.exists():
        return None

    prefix = f"{issue_id}-"
    latest_evidence: dict | None = None
    latest_attempt = -1

    for file in evidence_dir.iterdir():
        if not file.name.startswith(prefix) or not file.name.endswith(".json"):
            continue
        try:
            attempt = int(file.name[len(prefix):-5])
        except ValueError:
            continue
        if attempt > latest_attempt:
            try:
                latest_evidence = json.loads(file.read_text(encoding="utf-8"))
                latest_attempt = attempt
            except (json.JSONDecodeError, OSError):
                continue

    return latest_evidence


def _find_pr_url_from_comments(comments: list[str]) -> str | None:
    """Find PR URL from issue comments.

    Looks for WORKLINK_EVIDENCE comments which contain the pr_url field.
    Feature-factory epics historically mirrored their PR as
    ``draft PR opened: <url>`` instead of a WORKLINK_EVIDENCE record, so accept
    that shape too to reconcile already-stranded epic issues.
    """
    for comment in reversed(comments):
        match = re.search(r"pr_url=([^\s]+)", comment)
        if match:
            url = match.group(1)
            if url and url != "None":
                normalized = _normalize_pr_url(url)
                if normalized:
                    return normalized
        match = re.search(r"draft PR opened:\s*(https?://github\.com/[^\s]+)", comment)
        if match:
            normalized = _normalize_pr_url(match.group(1))
            if normalized:
                return normalized
    return None


def _find_latest_attempt_from_comments(comments: list[str]) -> int | None:
    """Find the latest attempt number from issue comments."""
    max_attempt = 0
    for comment in comments:
        match = re.search(r"WORKLINK_EVIDENCE.*attempt=(\d+)", comment)
        if match:
            attempt = int(match.group(1))
            if attempt > max_attempt:
                max_attempt = attempt
    return max_attempt if max_attempt > 0 else None


def get_pr_url_for_review_issue(
    home: Path,
    issue_id: int,
    chainlink_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> str | None:
    """Get the PR URL for a worklink:review issue.

    First tries the evidence file, then falls back to issue comments.
    Returns None if no PR URL is found (fail-safe: leave issue untouched).
    """
    evidence = _find_latest_evidence_for_issue(home, issue_id)
    if evidence:
        pr_url = evidence.get("pr_url")
        if pr_url:
            normalized = _normalize_pr_url(pr_url)
            if normalized:
                return normalized

    if chainlink_runner:
        result = chainlink_runner([chainlink_bin(), "issue", "show", str(issue_id), "--json"])
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                comments = data.get("comments") or []
                comment_texts = [
                    c.get("content") or c.get("text") or c.get("body") or ""
                    if isinstance(c, dict) else str(c)
                    for c in comments
                    if isinstance(c, (dict, str))
                ]
                return _find_pr_url_from_comments(comment_texts)
            except (json.JSONDecodeError, TypeError):
                pass

    return None


@dataclasses.dataclass(frozen=True)
class MergedChainlinkResult:
    """Result of closing a merged chainlink."""

    issue_id: int
    pr_url: str
    merged_at: str | None
    merge_commit_sha: str | None


def close_merged_chainlinks_for_home(
    home: Path,
    *,
    gh_bin: str = "gh",
    dry_run: bool = False,
    gh_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[MergedChainlinkResult]:
    """Close chainlinks in worklink:review whose PRs have been merged.

    This is the reconciliation pass that handles chainlink #844: when a chainlink
    reaches worklink:review (post-PR-open) and its PR is subsequently MERGED,
    this function detects that merge and closes the chainlink.

    The PR<->chainlink association is resolved from:
    1. The evidence file in <home>/state/worklink/evidence/<issue>-<attempt>.json
    2. The WORKLINK_EVIDENCE comment on the issue (fallback)

    Idempotent: re-running on an already-closed chainlink is a no-op.
    Fail-safe: an unresolvable PR state leaves the chainlink as-is.

    Returns a list of chainlinks that were closed.
    """

    if gh_runner is None:
        def gh_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(args, capture_output=True, text=True, check=False)

    cl = make_claims(home)

    review_issue_ids = cl.issue_ids_with_label("worklink:review", status="open")
    if not review_issue_ids:
        return []

    closed_results: list[MergedChainlinkResult] = []

    for issue_id in review_issue_ids:
        pr_url = get_pr_url_for_review_issue(home, issue_id, chainlink_runner=cl.runner)
        if not pr_url:
            continue

        merge_state = _check_pr_merged_via_gh_runner(pr_url, runner=gh_runner)
        if merge_state is None or not merge_state.merged:
            continue

        if dry_run:
            closed_results.append(MergedChainlinkResult(
                issue_id=issue_id,
                pr_url=pr_url,
                merged_at=merge_state.merged_at,
                merge_commit_sha=merge_state.merge_commit_sha,
            ))
            continue

        merge_info = ""
        if merge_state.merge_commit_sha:
            merge_info = f" (merged as {merge_state.merge_commit_sha[:7]})"
        comment = (
            f"WORKLINK_CLOSED: PR merged{merge_info}. "
            f"Chainlink complete via PR {pr_url}"
        )

        cl._run("issue", "unlabel", str(issue_id), "worklink:review", check=False)
        cl._run("issue", "comment", str(issue_id), comment, check=False)

        closed_results.append(MergedChainlinkResult(
            issue_id=issue_id,
            pr_url=pr_url,
            merged_at=merge_state.merged_at,
            merge_commit_sha=merge_state.merge_commit_sha,
        ))

    return closed_results
