"""Chainlink-backed claim protocol for Worklink.

This module deliberately uses Chainlink as the coordination surface instead of
introducing a second claim database. Attempt state is recorded as structured
issue comments and locks are delegated to ``chainlink locks`` (slice-0 verified
as atomic during chainlink #438).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterable, Sequence

CLAIM_PREFIX = "WORKLINK_CLAIM "
REAPER_PREFIX = "WORKLINK_REAPER "

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

log = logging.getLogger(__name__)


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class ClaimRecord:
    issue_id: int
    attempt: int
    agent_id: str
    claimed_at: datetime
    heartbeat_at: datetime | None = None

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
        return CLAIM_PREFIX + json.dumps(payload, sort_keys=True)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ClaimRecord":
        return cls(
            issue_id=int(payload["issue_id"]),
            attempt=int(payload["attempt"]),
            agent_id=str(payload["agent_id"]),
            claimed_at=_parse_dt(str(payload["claimed_at"])),
            heartbeat_at=_parse_dt(str(payload["heartbeat_at"])) if payload.get("heartbeat_at") else None,
        )


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    record: ClaimRecord | None = None
    attempts_exhausted: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ReviewReadyEvidence:
    path: Path
    payload: dict[str, Any]


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def claim_records_from_comments(comments: Iterable[str]) -> list[ClaimRecord]:
    records: list[ClaimRecord] = []
    for comment in comments:
        for line in comment.splitlines():
            if not line.startswith(CLAIM_PREFIX):
                continue
            try:
                records.append(ClaimRecord.from_payload(json.loads(line[len(CLAIM_PREFIX) :])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return records


def _claim_is_newer(candidate: ClaimRecord, current: ClaimRecord) -> bool:
    """True when ``candidate`` supersedes ``current`` for the same issue:
    a higher attempt, or the same attempt with a later claim/heartbeat
    anchor (the record the reaper should judge for staleness)."""
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


def _lock_owner(lock: dict[str, Any]) -> str | None:
    for key in ("agent_id", "owner", "holder", "holder_id", "claimant"):
        raw = lock.get(key)
        if raw not in (None, ""):
            return str(raw)
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
        max_attempts: int = 3,
        duplicate_freshness_s: float = 600.0,
        home_path: str | Path | None = None,
    ) -> None:
        self.chainlink_bin = chainlink_bin
        self.agent_id = agent_id
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_attempts = max_attempts
        self.home_path = Path(home_path) if home_path is not None else None
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
        label_set = self._issue_labels(issue_id)
        if labels is not None:
            label_set.update(labels)
        if "worklink:review" in label_set:
            return ClaimResult(False, reason="lifecycle_state_incompatible")

        review_ready = self.review_ready_evidence(issue_id, home_path=home_path)
        if review_ready is not None:
            log.info(
                "Worklink claim refused: issue_id=%s reason=review_ready_evidence_exists "
                "evidence_path=%s pr_url=%s",
                issue_id,
                review_ready.path,
                review_ready.payload.get("pr_url"),
            )
            return ClaimResult(False, reason="review_ready_evidence_exists")

        lock = self._run("locks", "claim", str(issue_id), check=False)
        if lock.returncode != 0:
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
        if attempt > self.max_attempts:
            self.release_issue(issue_id)
            self._attempts_exhausted(issue_id, attempt - 1)
            return ClaimResult(False, attempts_exhausted=True, reason="attempts_exhausted")

        if max_active_locks is not None:
            try:
                active = self.active_worklink_lock_count()
            except Exception:
                self.release_issue(issue_id)
                raise
            if active > max_active_locks:
                self.release_issue(issue_id)
                return ClaimResult(
                    False,
                    reason=(
                        f"concurrency cap reached ({active - 1}/{max_active_locks} active "
                        "claims before this reservation)"
                    ),
                )

        record = ClaimRecord(
            issue_id=issue_id,
            attempt=attempt,
            agent_id=self.agent_id,
            claimed_at=self.clock(),
        )
        try:
            self._run("issue", "unlabel", str(issue_id), "worklink:ready", check=False)
            self._run("issue", "label", str(issue_id), "worklink:in-progress")
            self._run("issue", "comment", str(issue_id), record.to_comment())
        except Exception:
            self.release_issue(issue_id)
            raise
        return ClaimResult(True, record=record)

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

    def release_issue(self, issue_id: int) -> None:
        """Release the Chainlink lock for ``issue_id`` best-effort."""
        self._run("locks", "release", str(issue_id), check=False)

    def heartbeat_issue(self, record: ClaimRecord) -> ClaimRecord:
        """Append a refreshed claim record so the TTL reaper sees liveness."""
        updated = ClaimRecord(
            issue_id=record.issue_id,
            attempt=record.attempt,
            agent_id=record.agent_id,
            claimed_at=record.claimed_at,
            heartbeat_at=self.clock(),
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
        records = claim_records_from_comments(comments)
        if not records:
            return 1
        return max(record.attempt for record in records) + 1

    def reap_stale_claims(self, records: Iterable[ClaimRecord], *, ttl: timedelta) -> list[ClaimRecord]:
        """Release stale claims and move the issue back to ready or blocked.

        ``chainlink locks steal`` is forceful in the verified Chainlink version,
        so staleness is decided here from claim/heartbeat timestamps before the
        steal is attempted.
        """
        now = self.clock()
        reaped: list[ClaimRecord] = []
        for record in records:
            if not record.is_stale(now, ttl):
                continue
            if not self._lock_still_held_by(record):
                continue
            steal = self._run("locks", "steal", str(record.issue_id), check=False)
            if steal.returncode != 0:
                continue
            if not self._issue_has_label(record.issue_id, "worklink:in-progress"):
                self._run("locks", "release", str(record.issue_id), check=False)
                continue
            self._run("locks", "release", str(record.issue_id), check=False)
            self._run("issue", "unlabel", str(record.issue_id), "worklink:in-progress", check=False)
            if record.attempt >= self.max_attempts:
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
                "reaped_at": now.isoformat(),
            }
            self._run(
                "issue",
                "comment",
                str(record.issue_id),
                REAPER_PREFIX + json.dumps(payload, sort_keys=True),
            )
            reaped.append(record)
        return reaped

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
        """Best-effort race guard before TTL reaping.

        If the original worker already released the lock during its normal
        transition, do not steal/relabel the issue back to ready. When the lock
        table exposes an owner/agent field, require it to match the claim record;
        when the shape is too old to identify owners, retain the prior behavior
        rather than disabling reaping entirely.
        """
        result = self._run("locks", "list", "--json", check=False)
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False
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
        if not isinstance(lock, dict):
            return True
        owner = _lock_owner(lock)
        return owner is None or owner == record.agent_id

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

    def active_worklink_lock_count(self) -> int:
        """Number of active Chainlink locks — the autonomous hard-cap surface.

        RAISES if the lock table can't be read or parsed, so the cap fails
        closed. Chainlink locks are the atomic reservation mechanism; counting
        them avoids the label-based check-then-act window where a worker has
        been admitted but has not yet applied ``worklink:in-progress``.
        """
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
        if isinstance(locks, dict):
            return len(locks)
        if isinstance(locks, list):
            return len(locks)
        raise RuntimeError("chainlink locks list --json returned unexpected shape")

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

    def reap_home(self, *, ttl: timedelta) -> list[ClaimRecord]:
        """Discover ``worklink:in-progress`` issues, gather the latest claim
        record per issue from their comments, and reap any stale ones.

        This is the entry point the scheduler's TTL-reaper callable uses:
        it owns the discovery so :meth:`reap_stale_claims` stays a pure,
        records-in transform that's trivial to unit-test.
        """
        latest: dict[int, ClaimRecord] = {}
        for issue_id in self.issue_ids_with_label("worklink:in-progress"):
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
