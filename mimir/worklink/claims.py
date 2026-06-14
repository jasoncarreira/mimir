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
import subprocess
from typing import Any, Callable, Iterable, Sequence

CLAIM_PREFIX = "WORKLINK_CLAIM "
REAPER_PREFIX = "WORKLINK_REAPER "

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


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
    ) -> None:
        self.chainlink_bin = chainlink_bin
        self.agent_id = agent_id
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_attempts = max_attempts

    def claim_issue(self, issue_id: int, comments: Iterable[str] = ()) -> ClaimResult:
        """Claim ``issue_id`` if the attempts cap has not been exhausted."""
        attempt = self.next_attempt(comments)
        if attempt > self.max_attempts:
            self._attempts_exhausted(issue_id, attempt - 1)
            return ClaimResult(False, attempts_exhausted=True, reason="attempts_exhausted")

        lock = self._run("locks", "claim", str(issue_id), check=False)
        if lock.returncode != 0:
            return ClaimResult(False, reason=(lock.stderr or lock.stdout).strip() or "claim_failed")

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

    def release_issue(self, issue_id: int) -> None:
        """Release the Chainlink lock for ``issue_id`` best-effort."""
        self._run("locks", "release", str(issue_id), check=False)

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
            steal = self._run("locks", "steal", str(record.issue_id), check=False)
            if steal.returncode != 0:
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
