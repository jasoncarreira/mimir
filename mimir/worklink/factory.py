"""Feature-factory epic driver (chainlink #834).

Drives the external opencode ``feature-factory`` headless for ``worklink:epic``
chainlink issues. The factory itself is chainlink-agnostic; this module is the
sole adapter that:

* claims the epic (per-issue exclusivity + the autonomous concurrency cap),
* launches ``feature-factory factory start --headless`` and drives its gate loop,
* AUTO-APPROVES the ``story`` and ``brief`` gates on the factory's own validator
  (decided 2026-07-04: trusted autonomously),
* runs an INDEPENDENT review subagent at the ``pre_pr`` gate — reads the diff,
  runs the suite, and returns approve / changes / stop (no human gate; the PR is
  the human review point),
* on approve, lets the factory open its draft PR, then promotes it to
  ready-for-review + requests the mimir reviewer, and transitions the epic.

Headless runs stop at each gate and exit, so the loop is: launch → read
``run.json`` → answer the pending gate → resume, until a terminal status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .claims import ChainlinkClaims, ClaimRecord
from .orchestrator import (
    ChainlinkIssueReader,
    IssueContext,
    Runner,
    WorklinkError,
    WorklinkRunResult,
    _heartbeat_while,
    _log_event,
    _runner_for_home,
)

log = logging.getLogger(__name__)

# Gate policy (chainlink #834). story/brief trust the factory's own validator;
# pre_pr is judged by an independent review subagent.
AUTO_APPROVE_GATES: tuple[str, ...] = ("story", "brief")
REVIEW_GATE = "pre_pr"
_GATE_ORDER: tuple[str, ...] = ("story", "brief", "pre_pr")

# Factory run statuses that mean "no more gates will open on their own".
_TERMINAL_STATUSES = frozenset({"done", "completed", "complete", "blocked", "failed"})
# Terminal statuses that mean the run SUCCEEDED — only these + a PR ship.
_SUCCESS_TERMINAL_STATUSES = frozenset({"done", "completed", "complete"})

# Reviewer verdict vocabulary (CriticFindings, mimir/subagents.py) → gate answer.
_APPROVE_VERDICTS = frozenset({"no_concerns", "nits", "approve"})
_CHANGES_VERDICTS = frozenset({"important", "changes"})
_STOP_VERDICTS = frozenset({"blocker", "stop"})

# The GitHub identity mimir requests review from (its own agent account).
MIMIR_REVIEWER = os.environ.get("MIMIR_FACTORY_REVIEWER", "mimir-carreira")

_DEFAULT_POLL_CYCLES = 40  # hard bound on gate cycles (build + changes loops)


@dataclass(frozen=True)
class FactoryReview:
    """Verdict from the independent pre_pr review subagent."""

    verdict: str
    rationale: str = ""

    @property
    def gate_answer(self) -> str:
        v = self.verdict.strip().lower()
        if v in _STOP_VERDICTS:
            return "stop"
        if v in _CHANGES_VERDICTS:
            detail = self.rationale.strip() or "address the reviewer's concerns"
            return f"changes: {detail}"
        return "approve"


@dataclass(frozen=True)
class FactoryReviewContext:
    repo: Path
    run_dir: Path
    run_id: str
    issue: IssueContext


FactoryReviewer = Callable[[FactoryReviewContext], FactoryReview]


def factory_bin_from_env(default: str = "feature-factory") -> tuple[str, ...]:
    """``MIMIR_FEATURE_FACTORY_BIN`` (shlex-split) or the default binary name."""
    raw = os.environ.get("MIMIR_FEATURE_FACTORY_BIN", "").strip()
    return tuple(shlex.split(raw)) if raw else (default,)


# Verdict severity for combining review passes (chainlink #835). Higher = worse.
_VERDICT_SEVERITY = {
    "no_concerns": 0, "approve": 0,
    "nits": 1,
    "important": 2, "changes": 2,
    "blocker": 3, "stop": 3,
}


def _default_reviewer_factory(
    *, runner: Runner, timeout_s: int, review_bin: tuple[str, ...]
) -> FactoryReviewer:
    """Two-pass reviewer (chainlink #835): a general review PLUS a dedicated
    security-lens pass, combined so the STRICTER verdict wins. A single generic
    review demonstrably under-covers trust-boundary / prompt-injection issues
    (see PR #1027, where a generic review + the factory's own validator both
    missed a /event forgery bypass and an args prompt-injection)."""

    def review(ctx: FactoryReviewContext) -> FactoryReview:
        general = _run_opencode_review(runner, review_bin, ctx, _review_prompt(ctx))
        security = _run_opencode_review(runner, review_bin, ctx, _security_review_prompt(ctx))
        return _combine_reviews(general, security)

    return review


def _run_opencode_review(
    runner: Runner, review_bin: tuple[str, ...], ctx: FactoryReviewContext, prompt: str
) -> FactoryReview:
    argv = [*review_bin, "run", "--dir", str(ctx.repo), "--", prompt]
    result = runner(argv)
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    return _parse_review(text)


def _combine_reviews(*reviews: FactoryReview | None) -> FactoryReview:
    """Combine review passes — the strictest verdict wins; the rationale carries
    the concern(s) that drove any changes/stop outcome."""
    present = [r for r in reviews if r is not None]
    if not present:
        return FactoryReview(verdict="important", rationale="no review produced")
    worst = max(present, key=lambda r: _VERDICT_SEVERITY.get(r.verdict.strip().lower(), 2))
    notes = [
        r.rationale.strip()
        for r in present
        if _VERDICT_SEVERITY.get(r.verdict.strip().lower(), 2) >= 2 and r.rationale.strip()
    ]
    return FactoryReview(verdict=worst.verdict, rationale="; ".join(notes) or worst.rationale)


def _review_prompt(ctx: FactoryReviewContext) -> str:
    return (
        f"You are an independent pre-PR reviewer for chainlink #{ctx.issue.issue_id} "
        f"({ctx.issue.title}). The feature-factory has assembled an integration branch "
        "in this repo. Review it rigorously before it becomes a PR.\n\n"
        "Do all of the following:\n"
        "1. Inspect the full diff against the base branch (git log/diff).\n"
        "2. Run the project's test suite and report pass/fail.\n"
        "3. Read the factory's own validation report under "
        f".opencode/factory/{ctx.run_id}/artifacts/ if present.\n"
        "4. SECURITY: check trust boundaries — does untrusted/client-controlled "
        "input (request bodies, event metadata, tool/command args) reach a "
        "privileged sink (LLM prompt/system/skill instructions, shell, file "
        "writes, auth) without validation? Is untrusted text interpolated into a "
        "privileged prompt region rather than rendered as labeled data? Can a "
        "server-owned marker be forged via an alternate endpoint? (A separate "
        "security-lens pass runs too — but flag anything you see here.)\n\n"
        "Cite the specific file:line for every concern.\n\n"
        "Then decide a verdict using EXACTLY this vocabulary:\n"
        "  no_concerns | nits | important | blocker\n"
        "- no_concerns/nits => ship it (approve)\n"
        "- important => request changes (the factory will fix and re-gate)\n"
        "- blocker => stop the run\n\n"
        "End your reply with a single final line of JSON and nothing after it:\n"
        '{"verdict": "<one of the four>", "rationale": "<one sentence with file:line; for '
        'important/blocker state the specific change needed>"}'
    )


def _security_review_prompt(ctx: FactoryReviewContext) -> str:
    return (
        f"You are a SECURITY reviewer for chainlink #{ctx.issue.issue_id} "
        f"({ctx.issue.title}). Review ONLY the trust boundaries of the integration "
        "branch in this repo (assume functional correctness is covered elsewhere). "
        "Inspect the full diff (git diff against the base) and answer each of these, "
        "enumerating EVERY relevant path — not just the one the feature is 'about':\n"
        "1. TRUST BOUNDARIES: does any untrusted/client-controlled input (HTTP "
        "request bodies, event.extra/metadata, tool/command arguments, env, file "
        "contents) reach a privileged sink (LLM prompt/system/skill instructions, "
        "shell/subprocess, file writes, DB, auth decisions) WITHOUT sanitization or "
        "validation? Check ALL ingress endpoints/handlers, not only the obvious one.\n"
        "2. PROMPT INJECTION: is any untrusted string interpolated into a privileged "
        "prompt region? It MUST be rendered as clearly-labeled untrusted data "
        "(JSON-encoded or fenced), never as instructions.\n"
        "3. FORGEABLE PROVENANCE: can a server-owned marker (a skill invocation, an "
        "author/identity/source field, a signed flag) be manufactured by a client "
        "via an alternate endpoint or by setting request fields directly?\n"
        "4. SECRETS: are any secrets/tokens logged, echoed, or written to artifacts?\n\n"
        "For EACH concern cite the specific file:line. Prefer a false positive you "
        "can point to over silence.\n\n"
        "Verdict vocabulary (EXACTLY): no_concerns | nits | important | blocker\n"
        "- a real trust-boundary bypass or injection => at least 'important'; a "
        "critical/unauth RCE-class issue => 'blocker'.\n\n"
        "End with a single final JSON line and nothing after it:\n"
        '{"verdict": "<one of the four>", "rationale": "<file:line + the specific fix needed>"}'
    )


def _parse_review(text: str) -> FactoryReview:
    """Parse the reviewer's final ``{"verdict":..,"rationale":..}`` JSON line.

    Fail SAFE: if no parseable verdict is found, request changes rather than
    silently approving unreviewed work.
    """
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict in _APPROVE_VERDICTS | _CHANGES_VERDICTS | _STOP_VERDICTS:
            return FactoryReview(verdict=verdict, rationale=str(payload.get("rationale") or ""))
    return FactoryReview(
        verdict="important",
        rationale="reviewer did not emit a parseable verdict; re-run review",
    )


@dataclass(frozen=True)
class FactoryEpicRunner:
    """Drives one ``worklink:epic`` issue through the feature-factory."""

    home: Path
    repo: Path
    chainlink_bin: str = "chainlink"
    agent_id: str = "mimir-factory"
    factory_bin: tuple[str, ...] = field(default_factory=factory_bin_from_env)
    runner: Runner | None = None
    reviewer: FactoryReviewer | None = None
    claims: ChainlinkClaims | None = None
    factory_timeout_s: int = 3600
    review_timeout_s: int = 1800
    max_cycles: int = _DEFAULT_POLL_CYCLES

    def _runner(self) -> Runner:
        return self.runner or _runner_for_home(self.home, self.chainlink_bin)

    def _claims(self) -> ChainlinkClaims:
        return self.claims or ChainlinkClaims(
            chainlink_bin=self.chainlink_bin, agent_id=self.agent_id, runner=self._runner()
        )

    def _autonomous_cap(self, autonomous: bool) -> int | None:
        """The autonomous concurrency cap (worklink.yaml ``defaults.max_concurrent``),
        or None for operator-invoked runs. Matches the leaf runner's contract."""
        if not autonomous:
            return None
        from .backends.registry import WorklinkConfig

        return WorklinkConfig.load(self.home / "worklink.yaml").defaults.max_concurrent

    def _reviewer(self) -> FactoryReviewer:
        if self.reviewer is not None:
            return self.reviewer
        return _default_reviewer_factory(
            runner=self._runner(), timeout_s=self.review_timeout_s, review_bin=("opencode",)
        )

    def run_id_for(self, issue_id: int) -> str:
        return f"chainlink-{issue_id}"

    def run_dir_for(self, issue_id: int) -> Path:
        return self.repo / ".opencode" / "factory" / self.run_id_for(issue_id)

    async def run(self, issue_id: int, *, autonomous: bool = False) -> WorklinkRunResult:
        return await self._run(issue_id, autonomous=autonomous)

    async def _run(self, issue_id: int, *, autonomous: bool) -> WorklinkRunResult:
        runner = self._runner()
        run_id = self.run_id_for(issue_id)
        run_dir = self.run_dir_for(issue_id)

        issue = ChainlinkIssueReader(chainlink_bin=self.chainlink_bin, runner=runner).read(issue_id)

        claims = self._claims()
        # Mirror the leaf runner's claim contract: pass the freshly-read comments
        # (attempt accounting + duplicate-live guard) and the autonomous
        # concurrency cap (prevents autonomous dispatch over-admitting workers).
        claim = claims.claim_issue(
            issue.issue_id,
            issue.comments,
            max_active_locks=self._autonomous_cap(autonomous),
        )
        if not claim.claimed or claim.record is None:
            reason = claim.reason or "could not claim epic"
            _log_event("factory_epic_claim_declined", issue_id=issue_id, reason=reason)
            return WorklinkRunResult(issue_id=issue_id, attempt=None, status="refused", reason=reason)

        record = claim.record
        # ``autonomous`` marks a poller dispatch (vs. an operator-invoked run).
        # The autonomy OPT-IN for factory epics is the poller-side
        # ``MIMIR_FACTORY_EPICS_ENABLED`` gate — the factory's compute posture is
        # governed by the opencode/factory config, not mimir's local_subprocess
        # autonomy policy — so this flag is recorded for telemetry, not enforced here.
        _log_event(
            "factory_epic_claimed",
            issue_id=issue_id, attempt=record.attempt, run_id=run_id, autonomous=autonomous,
        )
        try:
            return await self._drive(issue, run_id, run_dir, claims, record)
        except Exception as exc:  # noqa: BLE001 — surface as a failed epic, not a crash
            reason = f"{type(exc).__name__}: {exc}"
            _log_event("factory_epic_failed", issue_id=issue_id, reason=reason)
            claims.transition_issue(
                issue_id, status="failed", review_ready=False, attempt=record.attempt, reason=reason
            )
            return WorklinkRunResult(
                issue_id=issue_id, attempt=record.attempt, status="failed", reason=reason
            )
        finally:
            claims.release_issue(issue_id)

    async def _drive(
        self,
        issue: IssueContext,
        run_id: str,
        run_dir: Path,
        claims: ChainlinkClaims,
        record: ClaimRecord,
    ) -> WorklinkRunResult:
        answered: set[tuple[str, int]] = set()
        started = False

        for cycle in range(self.max_cycles):
            # Launch — START on the first cycle, RESUME thereafter. Each call is a
            # long subprocess (the whole build runs during the post-brief resume).
            if not started:
                launch = asyncio.to_thread(self._factory_start, self._initial_prompt(issue, run_id))
                started = True
            else:
                launch = asyncio.to_thread(self._factory_resume, run_id)
            await _heartbeat_while(launch, claims=claims, record=record)
            run = self._read_run(run_dir)
            if run is None:
                raise WorklinkError(
                    f"feature-factory produced no run.json at {run_dir} (run id {run_id})"
                )

            gate = self._pending_gate(run)
            if gate is None:
                return self._finalize(issue, run_id, run_dir, run, claims, record)

            gate_key = (gate, self._question_mtime(run_dir, gate))
            if gate_key in answered:
                # We already answered this exact gate instance and the resume did
                # not consume it → the factory is stuck. Bail rather than spin.
                raise WorklinkError(
                    f"pre_pr gate '{gate}' not consumed after answering (factory stuck)"
                    if gate == REVIEW_GATE
                    else f"gate '{gate}' not consumed after answering (factory stuck)"
                )

            if gate in AUTO_APPROVE_GATES:
                self._answer(run_id, gate, "approve")
                _log_event("factory_gate_answered", issue_id=issue.issue_id, gate=gate, answer="approve")
            elif gate == REVIEW_GATE:
                review = self._reviewer()(
                    FactoryReviewContext(repo=self.repo, run_dir=run_dir, run_id=run_id, issue=issue)
                )
                answer = review.gate_answer
                self._answer(run_id, gate, answer)
                _log_event(
                    "factory_pre_pr_reviewed",
                    issue_id=issue.issue_id,
                    verdict=review.verdict,
                    answer=answer.split(":", 1)[0],
                )
                if answer == "stop":
                    reason = review.rationale.strip() or "reviewer stopped the run"
                    claims.transition_issue(
                        issue.issue_id, status="blocked", review_ready=False,
                        attempt=record.attempt, reason=reason,
                    )
                    return WorklinkRunResult(
                        issue_id=issue.issue_id, attempt=record.attempt,
                        status="blocked", reason=reason,
                    )
            else:
                # Unknown gate — do not guess an answer.
                raise WorklinkError(f"feature-factory presented an unknown gate: {gate!r}")

            answered.add(gate_key)

        raise WorklinkError(f"feature-factory did not terminate within {self.max_cycles} gate cycles")

    def _finalize(
        self,
        issue: IssueContext,
        run_id: str,
        run_dir: Path,
        run: dict,
        claims: ChainlinkClaims,
        record: ClaimRecord,
    ) -> WorklinkRunResult:
        runner = self._runner()
        pr_url = _run_pr_url(run)
        status = str(run.get("status") or "").strip().lower()

        # Ship ONLY on a successful terminal status AND a PR. A run can carry a
        # draft pr_url yet still end blocked/failed (validation or push/finalize
        # trouble) — that must NOT be promoted to review.
        if status in _SUCCESS_TERMINAL_STATUSES and pr_url:
            # Promote the factory's draft PR to ready-for-review + request the
            # mimir reviewer, then move the epic to review.
            runner(["gh", "pr", "ready", pr_url])
            runner(["gh", "pr", "edit", pr_url, "--add-reviewer", MIMIR_REVIEWER])
            claims.transition_issue(
                issue.issue_id, status="review", review_ready=True,
                attempt=record.attempt,
            )
            _log_event("factory_epic_pr_opened", issue_id=issue.issue_id, pr_url=pr_url)
            return WorklinkRunResult(
                issue_id=issue.issue_id, attempt=record.attempt, status="review_ready",
                review_ready=True, pr_url=pr_url,
            )

        # Anything else: blocked/failed status, success-without-PR, or (the case
        # this guards) a PR URL under a non-success status. Surface for operator
        # follow-up; keep pr_url on the result for visibility.
        reason = _run_blocked_reason(run) or (
            f"factory ended in status '{status or 'unknown'}'"
            + (" with a PR URL but a non-success status" if pr_url else " with no PR")
        )
        epic_status = "failed" if status == "failed" else "blocked"
        claims.transition_issue(
            issue.issue_id, status=epic_status, review_ready=False,
            attempt=record.attempt, reason=reason,
        )
        _log_event(
            "factory_epic_not_shipped",
            issue_id=issue.issue_id, status=epic_status, reason=reason,
            pr_url=pr_url or None,
        )
        return WorklinkRunResult(
            issue_id=issue.issue_id, attempt=record.attempt, status=epic_status,
            reason=reason, pr_url=pr_url,
        )

    # --- factory CLI seams -------------------------------------------------

    def _factory_start(self, prompt: str):
        """Start a fresh factory run (runs headless to the first gate, exits)."""
        return self._factory_invoke(prompt)

    def _factory_resume(self, run_id: str):
        """Resume a gated run and run headless to the next gate.

        NOTE: this factory's CLI has NO separate resume subcommand — resume is
        ``factory start`` with a ``resume <run-id>`` prompt (verified against
        opencode-feature-factory src/cli.js: only start/status/watch/answer
        exist). Kept as a distinct method so start-vs-resume is explicit and
        testable; change the argv here if a future factory adds a resume command.
        """
        return self._factory_invoke(f"resume {run_id}")

    def _factory_invoke(self, prompt: str):
        argv = [*self.factory_bin, "factory", "start", "--headless", "--repo", str(self.repo), prompt]
        return self._runner()(argv)

    def _answer(self, run_id: str, gate: str, answer: str) -> None:
        argv = [*self.factory_bin, "factory", "answer", run_id, gate, answer, "--repo", str(self.repo)]
        result = self._runner()(argv)
        if result.returncode != 0:
            raise WorklinkError(
                (result.stderr or result.stdout).strip() or f"factory answer {gate} failed"
            )

    # --- run.json helpers --------------------------------------------------

    def _read_run(self, run_dir: Path) -> dict | None:
        path = run_dir / "run.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _pending_gate(self, run: dict) -> str | None:
        gates = run.get("gates") or {}
        for name in _GATE_ORDER:
            gate = gates.get(name) or {}
            if str(gate.get("status") or "").strip().lower() == "pending":
                return name
        # Tolerate gates the factory adds beyond the known set.
        for name, gate in gates.items():
            if str((gate or {}).get("status") or "").strip().lower() == "pending":
                return str(name)
        return None

    def _question_mtime(self, run_dir: Path, gate: str) -> int:
        # Nanosecond mtime so a fast re-opened gate (changes loop) never collides
        # with the prior instance within the same wall-clock second.
        try:
            return (run_dir / "gates" / f"{gate}.question.md").stat().st_mtime_ns
        except OSError:
            return 0

    def _initial_prompt(self, issue: IssueContext, run_id: str) -> str:
        parts = [f"Build chainlink #{issue.issue_id}: {issue.title}".strip(), ""]
        if issue.description.strip():
            parts += [issue.description.strip(), ""]
        parts.append(f"Use factory run id `{run_id}` for the control plane.")
        return "\n".join(parts).strip()


def _run_pr_url(run: dict) -> str | None:
    value = run.get("pr_url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _run_blocked_reason(run: dict) -> str | None:
    for key in ("blocked_reason", "reason", "error"):
        value = run.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def run_factory_epic(
    *, home: Path, repo: Path, issue_id: int, autonomous: bool = False
) -> WorklinkRunResult:
    """Synchronous entry point mirroring ``orchestrator.run_worklink``."""
    return asyncio.run(FactoryEpicRunner(home=home, repo=repo).run(issue_id, autonomous=autonomous))
