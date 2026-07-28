"""Execution-time idempotency for GitHub pull-request reviews."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_locks_guard = threading.Lock()
_locks: dict[tuple[str, int, str, str, str], threading.Lock] = {}


@dataclass(frozen=True)
class ReviewSubmission:
    executable: str
    repo: str | None
    number: int
    state: str
    cwd: str | None


@dataclass
class ReviewClaim:
    repo: str
    number: int
    head: str
    reviewer: str
    state: str
    duplicate: bool
    _lock: threading.Lock | None = None

    def release(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None


def _option_value(argv: list[str], names: set[str]) -> str | None:
    for index, value in enumerate(argv):
        if value in names and index + 1 < len(argv):
            return argv[index + 1]
        for name in names:
            if value.startswith(f"{name}="):
                return value.split("=", 1)[1]
    return None


def _review_argv(request: Any) -> tuple[list[str], str | None] | None:
    if str((request.tool_call or {}).get("name") or "") not in {
        "shell_exec", "bash_async", "Bash", "bash",
    }:
        return None
    args = (request.tool_call or {}).get("args") or {}
    cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else None
    direct = args.get("mimir_direct_argv")
    if isinstance(direct, list) and all(isinstance(value, str) for value in direct):
        return list(direct), cwd
    command = args.get("command")
    if not isinstance(command, str):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    # Only a standalone invocation can be safely identified before a shell
    # executes it. Review prompts use this shape; composed shell programs are
    # intentionally not guessed at here.
    if any(token in {"&&", "||", ";", "|", "&"} for token in argv):
        return None
    return argv, cwd


def review_submission_from_request(request: Any) -> ReviewSubmission | None:
    parsed = _review_argv(request)
    if parsed is None:
        return None
    argv, cwd = parsed
    if len(argv) < 4 or Path(argv[0]).name != "gh" or argv[1:3] != ["pr", "review"]:
        return None

    state_flags = {
        "--approve": "APPROVED",
        "--request-changes": "CHANGES_REQUESTED",
        "--comment": "COMMENTED",
    }
    states = {state for flag, state in state_flags.items() if flag in argv[3:]}
    if len(states) != 1:
        return None

    value_options = {"--repo", "-R", "--body", "--body-file"}
    number: int | None = None
    index = 3
    while index < len(argv):
        value = argv[index]
        if value in value_options:
            index += 2
            continue
        if any(value.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if not value.startswith("-"):
            try:
                number = int(value)
            except ValueError:
                return None
            break
        index += 1
    if number is None or number <= 0:
        return None
    repo = _option_value(argv[3:], {"--repo", "-R"})
    return ReviewSubmission(argv[0], repo, number, states.pop(), cwd)


def _gh_env() -> dict[str, str] | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {**os.environ, "GH_TOKEN": token} if token else None


def _run(spec: ReviewSubmission, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [spec.executable, *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=spec.cwd,
        env=_gh_env(),
    )


def _text(spec: ReviewSubmission, arguments: list[str]) -> str | None:
    try:
        result = _run(spec, arguments)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _repo(spec: ReviewSubmission) -> str | None:
    if spec.repo:
        return spec.repo.strip() or None
    return _text(spec, ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])


def _head(spec: ReviewSubmission, repo: str) -> str | None:
    return _text(
        spec,
        ["api", f"repos/{repo}/pulls/{spec.number}", "--jq", ".head.sha"],
    )


def _reviewer(spec: ReviewSubmission) -> str | None:
    return _text(spec, ["api", "user", "--jq", ".login"])


def _matching_review_exists(spec: ReviewSubmission, repo: str, head: str, reviewer: str) -> bool | None:
    text = _text(
        spec,
        ["api", f"repos/{repo}/pulls/{spec.number}/reviews", "--paginate"],
    )
    if text is None:
        return None
    try:
        reviews = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(reviews, list):
        return None
    reviewer_key = reviewer.casefold()
    return any(
        isinstance(review, dict)
        and str((review.get("user") or {}).get("login") or "").casefold() == reviewer_key
        and str(review.get("commit_id") or "") == head
        and str(review.get("state") or "").upper() == spec.state
        for review in reviews
    )


def claim_review_submission(spec: ReviewSubmission) -> ReviewClaim | None:
    """Reconcile under a local per-key lock, returning a held claim.

    ``None`` means GitHub identity/head metadata was unavailable. The caller
    fails open in that case so an outage cannot silently discard a legitimate
    review. A non-duplicate claim holds its lock through the outbound command.
    """
    repo = _repo(spec)
    reviewer = _reviewer(spec)
    if not repo or not reviewer:
        return None
    while True:
        head = _head(spec, repo)
        if not head:
            return None
        key = (repo.casefold(), spec.number, head, reviewer.casefold(), spec.state)
        with _locks_guard:
            lock = _locks.setdefault(key, threading.Lock())
        lock.acquire()
        # If the PR moved while this caller waited, claim the new-head key
        # instead. This keeps the key aligned with the head checked immediately
        # before submission.
        current_head = _head(spec, repo)
        if current_head != head:
            lock.release()
            if not current_head:
                return None
            continue
        exists = _matching_review_exists(spec, repo, head, reviewer)
        if exists is None:
            lock.release()
            return None
        return ReviewClaim(
            repo=repo,
            number=spec.number,
            head=head,
            reviewer=reviewer,
            state=spec.state,
            duplicate=exists,
            _lock=lock,
        )
