"""Chainlink-backed claim protocol for Worklink.

This module deliberately uses Chainlink as the coordination surface instead of
introducing a second claim database. Attempt state is recorded as structured
issue comments and locks are delegated to ``chainlink locks`` (slice-0 verified
as atomic during chainlink #438).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import fcntl
import json
import logging
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable, Sequence

CLAIM_PREFIX = "WORKLINK_CLAIM "

# Standalone callers without a Worklink config get the same finite safety
# budget as a missing or malformed defaults.max_claim_attempts setting.
DEFAULT_MAX_CLAIM_ATTEMPTS = 3

#: Operator marker that forgives the attempts consumed before it. The attempt
#: budget is derived from claim comments, so an infrastructure fault that fails
#: every attempt — a broken base-repo fetch, a reclaimed object store — exhausts a
#: leaf that was never itself at fault, and relabelling cannot undo it because the
#: count is recomputed from history on each dispatch. That happened to #1019,
#: #1020 and #1023 on 2026-07-28: six attempts burned by dangling git alternates,
#: three leaves demoted, and re-promotion re-exhausted them within seconds.
#:
#: Add with:  chainlink issue comment <id> 'WORKLINK_CLAIM_RESET {"reason": "..."}'
CLAIM_RESET_PREFIX = "WORKLINK_CLAIM_RESET "

#: How many resets one issue may be granted. Unbounded forgiveness would let a
#: genuinely stuck leaf loop forever by resetting itself — the comment carries no
#: author, so intent cannot be verified — which is exactly what ``max_attempts``
#: exists to prevent. After this many, the cap re-asserts permanently and the leaf
#: needs a human decision rather than another retry.
MAX_CLAIM_RESETS = 2
SHUTDOWN_ABORT_PREFIX = "WORKLINK_SHUTDOWN_ABORT "
# A planned restart must not consume the ordinary retry budget, but repeated
# restarts must not turn max_attempts into an infinite-retry loophole.
MAX_SHUTDOWN_ABORT_FORGIVENESS = 2
REAPER_PREFIX = "WORKLINK_REAPER "
REAPER_SKIP_SAMPLE_LIMIT = 20

# Retrying remains useful even with Mimir's mutex: another Chainlink caller may
# not participate in it. Five total attempts (including the first call) sleep for
# 0.1 + 0.2 + 0.4 + 0.8 = 1.5 seconds, long enough to absorb a short external
# Chainlink fetch while keeping genuine persistent contention bounded.
CLAIM_CONTENTION_MAX_ATTEMPTS = 5
CLAIM_CONTENTION_INITIAL_BACKOFF_S = 0.1
CLAIM_CONTENDED_RESOURCE = "chainlink_locks_worktree"

_GIT_CONTENTION_PATTERNS = (
    re.compile(
        r"unable to create .*[/\\]index\.lock.*file exists",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"another git process seems to be running", re.IGNORECASE),
    re.compile(r"another process is using this repository", re.IGNORECASE),
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
EventLogger = Callable[..., None]

log = logging.getLogger(__name__)


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _is_git_contention(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    detail = (result.stderr or "") + "\n" + (result.stdout or "")
    return any(pattern.search(detail) for pattern in _GIT_CONTENTION_PATTERNS)


@dataclass(frozen=True)
class ClaimRecord:
    issue_id: int
    attempt: int
    agent_id: str
    claimed_at: datetime
    heartbeat_at: datetime | None = None
    # Unique attempt ordinals can exceed the charged budget after a forgiven
    # shutdown. Kept on the wire so terminal transitions use the right budget.
    budget_attempt: int | None = None
    # How many honoured ``WORKLINK_CLAIM_RESET`` markers precede this record.
    # Derived from position in the comment history rather than carried on the
    # wire, so ``to_comment`` stays byte-identical. Generation still orders
    # legacy histories whose reset restarted attempt numbering.
    generation: int = 0

    def is_stale(self, now: datetime, ttl: timedelta) -> bool:
        anchor = self.heartbeat_at or self.claimed_at
        return now - anchor > ttl

    def to_comment(self) -> str:
        payload = {
            "issue_id": self.issue_id,
            "attempt": self.attempt,
            "agent_id": self.agent_id,
            "claimed_at": self.claimed_at.isoformat(),
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
        }
        if self.budget_attempt is not None:
            payload["budget_attempt"] = self.budget_attempt
        return CLAIM_PREFIX + json.dumps(payload, sort_keys=True)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ClaimRecord":
        return cls(
            issue_id=int(payload["issue_id"]),
            attempt=int(payload["attempt"]),
            agent_id=str(payload["agent_id"]),
            claimed_at=_parse_dt(str(payload["claimed_at"])),
            heartbeat_at=_parse_dt(str(payload["heartbeat_at"])) if payload.get("heartbeat_at") else None,
            budget_attempt=(
                int(payload["budget_attempt"])
                if payload.get("budget_attempt") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ShutdownAbortRecord:
    issue_id: int
    attempt: int
    agent_id: str
    claimed_at: datetime
    aborted_at: datetime
    generation: int = 0

    def to_comment(self) -> str:
        return SHUTDOWN_ABORT_PREFIX + json.dumps(
            {
                "issue_id": self.issue_id,
                "attempt": self.attempt,
                "agent_id": self.agent_id,
                "claimed_at": self.claimed_at.isoformat(),
                "aborted_at": self.aborted_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ShutdownAbortRecord":
        return cls(
            issue_id=int(payload["issue_id"]),
            attempt=int(payload["attempt"]),
            agent_id=str(payload["agent_id"]),
            claimed_at=_parse_dt(str(payload["claimed_at"])),
            aborted_at=_parse_dt(str(payload["aborted_at"])),
        )


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    record: ClaimRecord | None = None
    attempts_exhausted: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ReapResult:
    reaped: list[ClaimRecord]
    examined: int
    skipped: dict[str, int]
    skipped_issue_ids: dict[str, list[int]]


@dataclass(frozen=True)
class ShutdownClaimFailure:
    issue_id: int | None
    reason: str


@dataclass(frozen=True)
class ReviewReadyEvidence:
    path: Path
    payload: dict[str, Any]


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _scan_claim_history(
    comments: Iterable[str],
) -> tuple[list[ClaimRecord], list[ShutdownAbortRecord], int]:
    """Parse every claim record with its reset generation, plus the final one.

    Scans in comment order, so a ``WORKLINK_CLAIM_RESET`` marker advances the
    generation of everything after it. Only the first ``MAX_CLAIM_RESETS``
    markers advance it, matching the reset budget: later markers are inert, and
    the records following them stay in the last honoured generation.

    Every record is returned regardless of generation. The duplicate-liveness
    guard and the stale-claim reaper both have to see a live claim even when it
    predates a reset, so a generation changes how records are ORDERED, never
    whether they exist.

    The returned generation is the final counter, which is not always the
    highest generation among the records: a reset posted after the last claim
    leaves its generation empty, which is what tells ``next_attempt`` the
    budget is fresh.
    """
    records: list[ClaimRecord] = []
    aborts: list[ShutdownAbortRecord] = []
    seen_claims: set[tuple[int, int, str, datetime]] = set()
    generation = 0
    for comment in comments:
        for line in comment.splitlines():
            if line.startswith(CLAIM_RESET_PREFIX):
                if generation < MAX_CLAIM_RESETS:
                    generation += 1
                continue
            if not line.startswith(CLAIM_PREFIX):
                if not line.startswith(SHUTDOWN_ABORT_PREFIX):
                    continue
                try:
                    abort = ShutdownAbortRecord.from_payload(
                        json.loads(line[len(SHUTDOWN_ABORT_PREFIX) :])
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                abort_key = (
                    abort.issue_id,
                    abort.attempt,
                    abort.agent_id,
                    abort.claimed_at,
                )
                if abort_key not in seen_claims:
                    continue
                aborts.append(replace(abort, generation=generation))
                continue
            try:
                record = ClaimRecord.from_payload(json.loads(line[len(CLAIM_PREFIX) :]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            records.append(replace(record, generation=generation))
            seen_claims.add(
                (record.issue_id, record.attempt, record.agent_id, record.claimed_at)
            )
    return records, aborts, generation


def _scan_claim_comments(comments: Iterable[str]) -> tuple[list[ClaimRecord], int]:
    records, _aborts, generation = _scan_claim_history(comments)
    return records, generation


def claim_records_from_comments(comments: Iterable[str]) -> list[ClaimRecord]:
    return _scan_claim_comments(comments)[0]


def _claim_is_newer(candidate: ClaimRecord, current: ClaimRecord) -> bool:
    """True when ``candidate`` supersedes ``current`` for the same issue.

    Generation is compared first for persisted histories from before attempt
    ordinals became monotonic across resets. Comparing attempt first made every
    post-reset build in those histories lose to the stale pre-reset record with
    the higher attempt number, and the reaper then judged staleness from that
    dead record's anchor and released a claim that was still heartbeating.
    Within one generation: a higher attempt, then a later claim/heartbeat
    anchor (the record the reaper should judge for staleness).
    """
    if candidate.generation != current.generation:
        return candidate.generation > current.generation
    if candidate.attempt != current.attempt:
        return candidate.attempt > current.attempt
    cand_anchor = candidate.heartbeat_at or candidate.claimed_at
    cur_anchor = current.heartbeat_at or current.claimed_at
    return cand_anchor > cur_anchor


def _lock_issue_id(lock: dict[str, Any]) -> int | None:
    for key in ("issue_id", "id", "issue"):
        raw = lock.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


class ChainlinkClaims:
    """Small wrapper around the Chainlink CLI claim/label/comment protocol."""

    def __init__(
        self,
        *,
        chainlink_bin: str = "chainlink",
        agent_id: str,
        runner: Runner = _default_runner,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = DEFAULT_MAX_CLAIM_ATTEMPTS,
        duplicate_freshness_s: float = 600.0,
        home_path: str | Path | None = None,
        event_logger: EventLogger | None = None,
        contention_max_attempts: int = CLAIM_CONTENTION_MAX_ATTEMPTS,
        contention_initial_backoff_s: float = CLAIM_CONTENTION_INITIAL_BACKOFF_S,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.chainlink_bin = chainlink_bin
        self.agent_id = agent_id
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_attempts = max_attempts
        self.home_path = Path(home_path) if home_path is not None else None
        self.event_logger = event_logger
        self.contention_max_attempts = max(1, contention_max_attempts)
        self.contention_initial_backoff_s = max(0.0, contention_initial_backoff_s)
        self.sleeper = sleeper
        # chainlink #822: a claim-comment heartbeat younger than this means the
        # owning process is alive (2x the max epic heartbeat interval of 300s).
        self.duplicate_freshness_s = duplicate_freshness_s

    def claim_issue(
        self,
        issue_id: int,
        comments: Iterable[str] = (),
        *,
        labels: Iterable[str] | None = None,
        home_path: str | Path | None = None,
        max_active_locks: int | None = None,
        active_label: str | None = None,
        exclude_active_label: str | None = None,
        before_claim: Callable[[], None] | None = None,
    ) -> ClaimResult:
        """Claim ``issue_id`` if its lifecycle, evidence, attempts, and cap allow it.

        ``max_active_locks`` is the autonomous-dispatch hard bound. The issue
        lock is acquired first, then the active lock count is checked while this
        claim is already reserved; if admitting this reservation would exceed
        the cap, the lock is released before any label/comment mutation or
        backend compute launch.

        Admission resolves current labels itself when a caller does not supply
        them and refuses ``worklink:review`` before claiming. It also reads the
        latest persisted evidence under the configured home (or the per-call
        override) and refuses completed evidence with an associated PR, even if
        labels have drifted. ``worklink:in-progress`` remains admissible for
        legitimate reattach scenarios.
        """
        comments = list(comments)
        label_set = self._issue_labels(issue_id)
        if labels is not None:
            label_set.update(labels)
        if "worklink:review" in label_set:
            return ClaimResult(False, reason="lifecycle_state_incompatible")

        review_ready = self.review_ready_evidence(issue_id, home_path=home_path)
        if review_ready is not None:
            # Completed PR evidence is the publication authority. Repair labels
            # before refusing a duplicate run so ready and undispatchable cannot
            # persist together after transient post-publication bookkeeping fails.
            self.transition_issue(
                issue_id,
                status="completed",
                review_ready=True,
            )
            log.info(
                "Worklink claim refused: issue_id=%s reason=review_ready_evidence_exists "
                "evidence_path=%s pr_url=%s",
                issue_id,
                review_ready.path,
                review_ready.payload.get("pr_url"),
            )
            return ClaimResult(False, reason="review_ready_evidence_exists")

        claim_home = Path(home_path) if home_path is not None else self.home_path
        lock = self._claim_lock_with_retry(
            issue_id, home_path=claim_home, before_claim=before_claim
        )
        if lock.returncode != 0:
            if _is_git_contention(lock):
                return ClaimResult(False, reason="claim_contention_exhausted")
            return ClaimResult(False, reason=(lock.stderr or lock.stdout).strip() or "claim_failed")
        if "already hold" in ((lock.stdout or "") + (lock.stderr or "")).lower():
            # chainlink #822: the chainlink CLI treats a same-agent re-claim as
            # idempotent success ("You already hold the lock", rc=0). All poller
            # dispatches share one agent identity, so without this guard a
            # duplicate run-epic sails through and wrecks the live run (epic
            # #783 run 12). A FRESH claim-comment heartbeat means another live
            # process owns this run — refuse without touching any state. A
            # stale one is a crashed predecessor: steal explicitly and proceed.
            latest: ClaimRecord | None = None
            # Read comments through our own JSON reader rather than trusting the
            # caller's parse — a caller-side key mismatch here means stealing a
            # LIVE run's lock (exactly how the guard's first live test failed).
            guard_comments = list(comments) or []
            try:
                guard_comments = self._issue_comments(issue_id) or guard_comments
            except Exception:
                pass
            for existing in claim_records_from_comments(guard_comments):
                if existing.issue_id != issue_id:
                    continue
                if latest is None or _claim_is_newer(existing, latest):
                    latest = existing
            if latest is not None:
                anchor = latest.heartbeat_at or latest.claimed_at
                age_s = (self.clock() - anchor).total_seconds()
                if age_s < self.duplicate_freshness_s:
                    return ClaimResult(False, reason="duplicate_run_live")
            self._run("locks", "steal", str(issue_id), check=False)

        # chainlink #825: exhaustion is judged AFTER the duplicate-liveness
        # guard — a duplicate bouncing off a LIVE final-attempt run must yield
        # duplicate_run_live above, never label the epic blocked (a poller
        # duplicate did exactly that to run 15's healthy attempt-3 claim).
        # Reaching here means we genuinely own the (fresh or stolen) lock.
        attempt = self.next_attempt(comments)
        attempts_used = self.attempts_used(comments)
        if attempts_used >= self.max_attempts:
            self.release_issue(issue_id)
            self._attempts_exhausted(issue_id, attempts_used)
            return ClaimResult(False, attempts_exhausted=True, reason="attempts_exhausted")

        if max_active_locks is not None:
            try:
                active_ids = self._active_worklink_lock_ids_for_scope(
                    label=active_label,
                    exclude_label=exclude_active_label,
                )
                active = len(active_ids)
            except Exception:
                self.release_issue(issue_id)
                raise
            if active > max_active_locks:
                self.release_issue(issue_id)
                consuming_ids = sorted(
                    lock_id
                    for lock_id in active_ids
                    if lock_id > 0 and lock_id != issue_id
                )
                ids_suffix = f"; active issue ids: {consuming_ids}" if consuming_ids else ""
                return ClaimResult(
                    False,
                    reason=(
                        f"concurrency cap reached ({active - 1}/{max_active_locks} active "
                        f"claims before this reservation{ids_suffix})"
                    ),
                )

        record = ClaimRecord(
            issue_id=issue_id,
            attempt=attempt,
            agent_id=self.agent_id,
            claimed_at=self.clock(),
            budget_attempt=attempts_used + 1,
        )
        try:
            self._run("issue", "unlabel", str(issue_id), "worklink:ready", check=False)
            self._run("issue", "label", str(issue_id), "worklink:in-progress")
            self._run("issue", "comment", str(issue_id), record.to_comment())
        except Exception:
            self.release_issue(issue_id)
            raise
        return ClaimResult(True, record=record)

    def _claim_lock_with_retry(
        self,
        issue_id: int,
        *,
        home_path: Path | None,
        before_claim: Callable[[], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run only the Chainlink claim under the shared-worktree mutex."""
        lock_path: Path | None = None
        if home_path is None:
            log.warning(
                "Worklink claim serialization unavailable: issue_id=%s reason=home_path_missing",
                issue_id,
            )
            if self.event_logger is not None:
                self.event_logger(
                    "worklink_claim_serialization_unavailable",
                    issue_id=issue_id,
                    resource=CLAIM_CONTENDED_RESOURCE,
                    reason="home_path_missing",
                )
        else:
            lock_dir = home_path / "state" / "worklink"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = lock_dir / "chainlink-claim.lock"

        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, self.contention_max_attempts + 1):
            if lock_path is None:
                if attempt == 1 and before_claim is not None:
                    before_claim()
                result = self._run("locks", "claim", str(issue_id), check=False)
            else:
                with lock_path.open("a", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        if attempt == 1 and before_claim is not None:
                            before_claim()
                        result = self._run("locks", "claim", str(issue_id), check=False)
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

            if not _is_git_contention(result):
                if attempt > 1:
                    self._emit_claim_contention(issue_id, attempt, "succeeded")
                return result

            if attempt < self.contention_max_attempts:
                self._emit_claim_contention(issue_id, attempt, "retrying")
                self.sleeper(self.contention_initial_backoff_s * (2 ** (attempt - 1)))

        assert result is not None
        self._emit_claim_contention(issue_id, self.contention_max_attempts, "exhausted")
        return result

    def _emit_claim_contention(self, issue_id: int, attempt: int, outcome: str) -> None:
        if self.event_logger is None:
            return
        self.event_logger(
            "worklink_claim_contention",
            issue_id=issue_id,
            resource=CLAIM_CONTENDED_RESOURCE,
            retry_attempt=attempt,
            max_attempts=self.contention_max_attempts,
            outcome=outcome,
        )

    def review_ready_evidence(
        self,
        issue_id: int,
        *,
        home_path: str | Path | None = None,
    ) -> ReviewReadyEvidence | None:
        """Return the latest active completed evidence associated with a PR.

        Archived ``.json.closed-unmerged`` evidence is intentionally excluded by
        the shared finder, so operator-approved re-attempts remain admissible.
        """
        effective_home = Path(home_path) if home_path is not None else self.home_path
        if effective_home is None:
            return None

        from .autonomy import _find_latest_evidence_file_for_issue

        found = _find_latest_evidence_file_for_issue(effective_home, issue_id)
        if found is None:
            return None
        path, payload = found
        if payload.get("status") != "completed" or not payload.get("pr_url"):
            return None
        return ReviewReadyEvidence(path=path, payload=payload)

    def release_issue(self, issue_id: int) -> bool:
        """Attempt to release ``issue_id`` and report whether Chainlink confirmed it."""
        result = self._run("locks", "release", str(issue_id), check=False)
        return result.returncode == 0

    def release_owned_claims_for_shutdown(
        self,
    ) -> tuple[list[ClaimRecord], list[ShutdownClaimFailure]]:
        """Return this process's in-flight claims to ready, best-effort.

        Ownership is taken only from the latest structured claim comment. A
        different process/host therefore cannot be released even when both use
        the same underlying Chainlink tracker identity.
        """
        released: list[ClaimRecord] = []
        failed: list[ShutdownClaimFailure] = []
        try:
            issue_ids = self._list_issue_ids("worklink:in-progress")
        except Exception as exc:  # noqa: BLE001 - shutdown must continue
            log.warning("Worklink shutdown claim discovery failed: %s", exc)
            failed.append(ShutdownClaimFailure(issue_id=None, reason=f"discovery_failed: {exc}"))
            self._emit_shutdown_release_failures(released, failed)
            return released, failed

        for index, issue_id in enumerate(issue_ids):
            try:
                latest: ClaimRecord | None = None
                for record in claim_records_from_comments(self._issue_comments(issue_id)):
                    if record.issue_id != issue_id:
                        continue
                    if latest is None or _claim_is_newer(record, latest):
                        latest = record
                if latest is None or latest.agent_id != self.agent_id:
                    continue

                abort = ShutdownAbortRecord(
                    issue_id=issue_id,
                    attempt=latest.attempt,
                    agent_id=latest.agent_id,
                    claimed_at=latest.claimed_at,
                    aborted_at=self.clock(),
                )
                # Keep the issue dispatchable throughout partial failure: first
                # record forgiveness, then add ready, release the lock, and only
                # then remove in-progress. No forceful lock steal is used here.
                self._run("issue", "comment", str(issue_id), abort.to_comment())
                self._run("issue", "label", str(issue_id), "worklink:ready")
                lock = self._run("locks", "release", str(issue_id), check=False)
                if lock.returncode != 0:
                    raise RuntimeError(
                        (lock.stderr or lock.stdout).strip() or "chainlink lock release failed"
                    )
                self._run("issue", "unlabel", str(issue_id), "worklink:in-progress")
                released.append(latest)
            except Exception as exc:  # noqa: BLE001 - one claim cannot hang shutdown
                log.warning(
                    "Worklink shutdown claim release failed: issue_id=%s error=%s",
                    issue_id,
                    exc,
                )
                failed.append(
                    ShutdownClaimFailure(
                        issue_id=issue_id,
                        reason=f"{type(exc).__name__}: {exc}"[:500],
                    )
                )
                if isinstance(exc, subprocess.TimeoutExpired):
                    failed.extend(
                        ShutdownClaimFailure(
                            issue_id=remaining_issue_id,
                            reason="abandoned_after_timeout",
                        )
                        for remaining_issue_id in issue_ids[index + 1 :]
                    )
                    break
        self._emit_shutdown_release_failures(released, failed)
        return released, failed

    def _emit_shutdown_release_failures(
        self,
        released: list[ClaimRecord],
        failed: list[ShutdownClaimFailure],
    ) -> None:
        if self.event_logger is None or not failed:
            return
        try:
            self.event_logger(
                "worklink_shutdown_claim_release_failed",
                released_issue_ids=[record.issue_id for record in released],
                failed=[
                    {"issue_id": failure.issue_id, "reason": failure.reason}
                    for failure in failed
                ],
            )
        except Exception:  # noqa: BLE001 - telemetry cannot block shutdown
            pass

    def heartbeat_issue(self, record: ClaimRecord) -> ClaimRecord:
        """Append a refreshed claim record so the TTL reaper sees liveness."""
        updated = ClaimRecord(
            issue_id=record.issue_id,
            attempt=record.attempt,
            agent_id=record.agent_id,
            claimed_at=record.claimed_at,
            heartbeat_at=self.clock(),
            budget_attempt=record.budget_attempt,
        )
        self._run("issue", "comment", str(record.issue_id), updated.to_comment(), check=False)
        return updated

    def transition_issue(
        self,
        issue_id: int,
        *,
        status: str,
        review_ready: bool,
        attempt: int | None = None,
        reason: str | None = None,
    ) -> None:
        """Move Worklink labels after evidence validation."""
        self._run("issue", "unlabel", str(issue_id), "worklink:in-progress", check=False)
        self._run("issue", "unlabel", str(issue_id), "worklink:ready", check=False)
        self._run("issue", "unlabel", str(issue_id), "worklink:review", check=False)
        self._run("issue", "unlabel", str(issue_id), "worklink:blocked", check=False)
        self._run("issue", "unlabel", str(issue_id), "worklink:failed", check=False)
        if review_ready:
            self._run("issue", "label", str(issue_id), "worklink:review")
            return
        if status == "blocked" or (attempt is not None and attempt >= self.max_attempts):
            self._run("issue", "label", str(issue_id), "worklink:blocked")
        else:
            self._run("issue", "label", str(issue_id), "worklink:ready")
        if reason:
            prefix = "WORKLINK_BLOCKED" if status == "blocked" else "WORKLINK_FAILED"
            self._run("issue", "comment", str(issue_id), f"{prefix} {reason}")

    def next_attempt(self, comments: Iterable[str]) -> int:
        """The globally monotonic attempt number a fresh claim would take.

        Only the attempt BUDGET is reset. ``claim_records_from_comments`` still
        reports every claim, because the duplicate-liveness guard and the stale
        claim reaper judge live runs from those records — forgetting them here
        would let a second run claim an issue a live run already holds. Admission
        checks liveness before exhaustion, so a reset cannot smuggle a concurrent
        build past that guard.

        Reset markers forgive the attempt budget, not attempt ordinals. Keeping
        ordinals monotonic prevents collisions with retained checkout directories,
        branches, and evidence files from earlier reset generations.
        """
        records, _generation = _scan_claim_comments(comments)
        attempts = [record.attempt for record in records]
        if not attempts:
            return 1
        return max(attempts) + 1

    def attempts_used(self, comments: Iterable[str]) -> int:
        """Charged attempts in the active reset generation.

        The first bounded set of valid shutdown markers across the leaf's
        history forgive their matching claims. Attempt ordinals still advance,
        preventing checkout/branch/evidence collisions.
        """
        records, aborts, generation = _scan_claim_history(comments)
        claim_keys = {
            (record.issue_id, record.attempt, record.agent_id, record.claimed_at)
            for record in records
        }
        forgiven: set[tuple[int, int, str, datetime]] = set()
        for abort in aborts:
            key = (abort.issue_id, abort.attempt, abort.agent_id, abort.claimed_at)
            if key not in claim_keys or key in forgiven:
                continue
            if len(forgiven) >= MAX_SHUTDOWN_ABORT_FORGIVENESS:
                break
            forgiven.add(key)
        active_claims = {
            (record.issue_id, record.attempt, record.agent_id, record.claimed_at)
            for record in records
            if record.generation == generation
        }
        return len(active_claims - forgiven)

    def reap_stale_claims(
        self,
        records: Iterable[ClaimRecord],
        *,
        ttl: timedelta,
    ) -> ReapResult:
        """Release stale claims and move the issue back to ready or blocked.

        ``chainlink locks steal`` is forceful in the verified Chainlink version,
        so staleness is decided here from claim/heartbeat timestamps before the
        steal is attempted.
        """
        now = self.clock()
        reaped: list[ClaimRecord] = []
        examined = 0
        skipped: dict[str, int] = {}
        skipped_issue_ids: dict[str, list[int]] = {}

        def record_skip(reason: str, issue_id: int) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1
            sample = skipped_issue_ids.setdefault(reason, [])
            if len(sample) < REAPER_SKIP_SAMPLE_LIMIT:
                sample.append(issue_id)

        for record in records:
            if not record.is_stale(now, ttl):
                continue
            examined += 1
            try:
                lock_held = self._lock_still_held_by(record)
            except RuntimeError:
                record_skip("lock_query_failed", record.issue_id)
                continue
            if not lock_held:
                record_skip("lock_not_held", record.issue_id)
                continue
            steal = self._run("locks", "steal", str(record.issue_id), check=False)
            if steal.returncode != 0:
                record_skip("lock_steal_failed", record.issue_id)
                continue
            if not self._issue_has_label(record.issue_id, "worklink:in-progress"):
                self._run("locks", "release", str(record.issue_id), check=False)
                record_skip("in_progress_label_missing", record.issue_id)
                continue
            self._run("locks", "release", str(record.issue_id), check=False)
            self._run("issue", "unlabel", str(record.issue_id), "worklink:in-progress", check=False)
            if (record.budget_attempt or record.attempt) >= self.max_attempts:
                self._run("issue", "label", str(record.issue_id), "worklink:blocked")
                transition = "blocked"
            else:
                self._run("issue", "label", str(record.issue_id), "worklink:ready")
                transition = "ready"
            payload = {
                "issue_id": record.issue_id,
                "stale_agent_id": record.agent_id,
                "attempt": record.attempt,
                "transition": transition,
                "last_heartbeat": (
                    record.heartbeat_at.isoformat() if record.heartbeat_at else None
                ),
                "resulting_label": f"worklink:{transition}",
                "reaped_at": now.isoformat(),
            }
            self._run(
                "issue",
                "comment",
                str(record.issue_id),
                REAPER_PREFIX + json.dumps(payload, sort_keys=True),
            )
            if self.event_logger is not None:
                self.event_logger(
                    "worklink_claim_reaped",
                    issue_id=record.issue_id,
                    agent_id=record.agent_id,
                    last_heartbeat=payload["last_heartbeat"],
                    resulting_label=payload["resulting_label"],
                )
            reaped.append(record)
        if self.event_logger is not None and skipped:
            self.event_logger(
                "worklink_claim_reap_skipped",
                examined=examined,
                skipped=skipped,
                skipped_issue_ids=skipped_issue_ids,
                sample_limit=REAPER_SKIP_SAMPLE_LIMIT,
            )
        return ReapResult(
            reaped=reaped,
            examined=examined,
            skipped=skipped,
            skipped_issue_ids=skipped_issue_ids,
        )

    def _issue_labels(self, issue_id: int) -> set[str]:
        """Return current labels when Chainlink exposes them, otherwise empty."""
        result = self._run("issue", "show", str(issue_id), "--json", check=False)
        if result.returncode != 0:
            return set()
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return set()
        raw_labels = data.get("labels")
        labels: set[str] = set()
        if isinstance(raw_labels, list):
            for item in raw_labels:
                if isinstance(item, str):
                    labels.add(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("label")
                    if name:
                        labels.add(str(name))
        elif isinstance(raw_labels, dict):
            labels.update(str(name) for name in raw_labels)
        return labels

    def _issue_has_label(self, issue_id: int, label: str) -> bool:
        """Best-effort current-label check for reaper race avoidance.

        Reaper discovery is necessarily two-step (list in-progress, then inspect
        comments). A worker can transition the issue to review/blocked between
        discovery and ``locks steal``. When ``issue show --json`` exposes labels,
        refuse to relabel anything no longer in-progress. If the label shape is
        unavailable, preserve the prior behavior rather than disabling reaping.
        """
        result = self._run("issue", "show", str(issue_id), "--json", check=False)
        if result.returncode != 0:
            return True
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return True
        raw_labels = data.get("labels")
        if raw_labels is None:
            return True
        labels: set[str] = set()
        if isinstance(raw_labels, list):
            for item in raw_labels:
                if isinstance(item, str):
                    labels.add(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("label")
                    if name:
                        labels.add(str(name))
        elif isinstance(raw_labels, dict):
            labels.update(str(name) for name in raw_labels)
        return label in labels

    def _lock_still_held_by(self, record: ClaimRecord) -> bool:
        """Return whether the issue still consumes a Chainlink lock.

        If the original worker already released the lock during its normal
        transition, do not steal/relabel the issue back to ready. The lock's
        ``agent_id`` is the Chainlink tracker identity, while ``record.agent_id``
        is the Worklink process identity; comparing them made every normally
        shaped live lock invisible to the reaper. Claim ownership and freshness
        therefore come from the latest structured claim comment, while this
        guard checks only that the concurrency-slot lock still exists.
        """
        result = self._run("locks", "list", "--json", check=False)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout).strip()
                or f"chainlink locks list failed (rc={result.returncode})"
            )
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("chainlink locks list returned invalid JSON") from exc
        locks = data.get("locks", data if isinstance(data, list) else {})
        lock: Any | None = None
        if isinstance(locks, dict):
            lock = locks.get(str(record.issue_id))
            if lock is None:
                for value in locks.values():
                    if isinstance(value, dict) and _lock_issue_id(value) == record.issue_id:
                        lock = value
                        break
        elif isinstance(locks, list):
            for value in locks:
                if isinstance(value, dict) and _lock_issue_id(value) == record.issue_id:
                    lock = value
                    break
        if lock is None:
            return False
        return True

    # ---- Discovery / concurrency (slice-3 autonomy) ------------------

    def _list_issue_ids(self, label: str | None, *, status: str = "open") -> list[int]:
        """Query issue ids, optionally carrying ``label``; raise on failure.

        The strict path behind the safety cap: it must distinguish "no active
        claims" from "couldn't read active claims" so the cap can fail closed.
        """
        args = ["issue", "list"]
        if label is not None:
            args.extend(["--label", label])
        args.extend(["--status", status, "--json"])
        result = self._run(*args, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout).strip()
                or f"chainlink issue list failed (rc={result.returncode})"
            )
        try:
            data = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "chainlink issue list returned invalid JSON"
            ) from exc
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

    def issue_ids_with_label(self, label: str, *, status: str = "open") -> list[int]:
        """Best-effort id list for ``label`` ([] on any query failure).

        Use for *discovery* (ready-queue scan, reaper sweep) where a missing
        list just means "do less this cycle". The concurrency CAP must NOT use
        this — see :meth:`active_worklink_lock_count`, which fails closed instead.
        """
        try:
            return self._list_issue_ids(label, status=status)
        except RuntimeError:
            return []

    def issue_ids(self, *, status: str = "open") -> list[int]:
        """Best-effort issue id list without a label filter."""
        try:
            return self._list_issue_ids(None, status=status)
        except RuntimeError:
            return []

    def active_claim_count(self) -> int:
        """Number of ``worklink:in-progress`` issues.

        Kept for discovery/telemetry compatibility. The autonomous concurrency
        cap uses :meth:`active_worklink_lock_count` instead: labels are applied
        after process start, while locks are the atomic reservation surface.
        """
        return len(self._list_issue_ids("worklink:in-progress"))

    def active_worklink_lock_count(
        self,
        *,
        label: str | None = None,
        exclude_label: str | None = None,
    ) -> int:
        """Number of active Chainlink locks — the autonomous hard-cap surface.

        RAISES if the lock table can't be read or parsed, so the cap fails
        closed. Chainlink locks are the atomic reservation mechanism; counting
        them avoids the label-based check-then-act window where a worker has
        been admitted but has not yet applied ``worklink:in-progress``.
        """
        return len(
            self._active_worklink_lock_ids_for_scope(
                label=label,
                exclude_label=exclude_label,
            )
        )

    def _active_worklink_lock_ids_for_scope(
        self,
        *,
        label: str | None = None,
        exclude_label: str | None = None,
    ) -> set[int]:
        if label is not None and exclude_label is not None:
            raise ValueError("active lock scope accepts label or exclude_label, not both")
        active_ids = self._active_worklink_lock_ids(
            require_identity=label is not None or exclude_label is not None
        )
        if label is not None:
            scoped_ids = set(self._list_issue_ids(label))
            return active_ids & scoped_ids
        if exclude_label is not None:
            excluded_ids = set(self._list_issue_ids(exclude_label))
            return active_ids - excluded_ids
        return active_ids

    def _active_worklink_lock_ids(self, *, require_identity: bool) -> set[int]:
        result = self._run("locks", "list", "--json", check=False)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout).strip()
                or f"chainlink locks list --json failed (rc={result.returncode})"
            )
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("chainlink locks list --json returned invalid JSON") from exc
        locks = data.get("locks", data if isinstance(data, list) else {})
        ids: set[int] = set()
        if isinstance(locks, dict):
            iterable = locks.items()
        elif isinstance(locks, list):
            iterable = enumerate(locks)
        else:
            raise RuntimeError("chainlink locks list --json returned unexpected shape")
        for index, (key, value) in enumerate(iterable):
            raw = _lock_issue_id(value) if isinstance(value, dict) else None
            if raw is None:
                try:
                    raw = int(key)
                except (TypeError, ValueError):
                    if require_identity:
                        raise RuntimeError("chainlink locks list --json omitted issue identity")
                    raw = -(index + 1)
            ids.add(raw)
        return ids

    def _issue_comments(self, issue_id: int) -> list[str]:
        result = self._run("issue", "show", str(issue_id), "--json", check=False)
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return []
        out: list[str] = []
        for item in payload.get("comments") or ():
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("content") or item.get("text") or item.get("body") or ""
                if text:
                    out.append(str(text))
        return out

    def reap_home(self, *, ttl: timedelta) -> ReapResult:
        """Discover ``worklink:in-progress`` issues, gather the latest claim
        record per issue from their comments, and reap any stale ones.

        This is the entry point the scheduler's TTL-reaper callable uses:
        it owns the discovery so :meth:`reap_stale_claims` stays a pure,
        records-in transform that's trivial to unit-test.
        """
        latest: dict[int, ClaimRecord] = {}
        issue_ids = set(self.issue_ids_with_label("worklink:in-progress"))
        try:
            # Locks are the authoritative reservation surface. Including them
            # recovers terminal-phase leaks after transition removed in-progress;
            # freshness and holder checks below still prevent stealing live runs.
            issue_ids.update(self._active_worklink_lock_ids(require_identity=True))
        except RuntimeError as exc:
            log.warning("Worklink reaper lock discovery failed: %s", exc)
        for issue_id in sorted(issue_ids):
            if self._issue_has_label(issue_id, "worklink:epic"):
                continue
            for record in claim_records_from_comments(self._issue_comments(issue_id)):
                current = latest.get(record.issue_id)
                if current is None or _claim_is_newer(record, current):
                    latest[record.issue_id] = record
        return self.reap_stale_claims(latest.values(), ttl=ttl)

    def _attempts_exhausted(self, issue_id: int, attempts: int) -> None:
        self._run("issue", "unlabel", str(issue_id), "worklink:ready", check=False)
        self._run("issue", "unlabel", str(issue_id), "worklink:in-progress", check=False)
        self._run("issue", "label", str(issue_id), "worklink:blocked")
        self._run(
            "issue",
            "comment",
            str(issue_id),
            REAPER_PREFIX
            + json.dumps(
                {
                    "issue_id": issue_id,
                    "attempts": attempts,
                    "transition": "blocked",
                    "reason": "attempts_exhausted",
                },
                sort_keys=True,
            ),
        )

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self.runner([self.chainlink_bin, *args])
        if check and result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"chainlink {' '.join(args)} failed")
        return result
