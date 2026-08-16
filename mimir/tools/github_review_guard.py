"""Execution-time idempotency for GitHub pull-request reviews."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .extra import _effective_shell_cwd
from .refusals import ToolPolicyRefusal


_locks_guard = threading.Lock()
_locks: dict[tuple[str, int, str, str, str], threading.Lock] = {}


@dataclass(frozen=True)
class ReviewSubmission:
    executable: str
    repo: str | None
    number: int | None
    state: str
    cwd: str | None
    repository_context_known: bool = True


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


def _review_segment(argv: list[str]) -> tuple[list[str], int] | None:
    start = 0
    for index in range(len(argv) + 1):
        if index < len(argv):
            token = argv[index]
            if not token or not all(character in "();|&\n" for character in token):
                continue
        segment = argv[start:index]
        if len(segment) >= 3 and Path(segment[0]).name == "gh" and segment[1:3] == ["pr", "review"]:
            return segment, start
        start = index + 1
    return None


def _review_argv(request: Any) -> tuple[list[str], str | None, bool] | None:
    if str((request.tool_call or {}).get("name") or "") not in {
        "shell_exec", "bash_async", "Bash", "bash",
    }:
        return None
    args = (request.tool_call or {}).get("args") or {}
    raw_cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else None
    effective_cwd = _effective_shell_cwd(raw_cwd)
    cwd = str(effective_cwd) if effective_cwd is not None else None
    direct = args.get("mimir_direct_argv")
    if isinstance(direct, list) and all(isinstance(value, str) for value in direct):
        return list(direct), cwd, True
    command = args.get("command")
    if not isinstance(command, str):
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv: list[str] = []
    try:
        while (token := lexer.get_token()) is not None:
            argv.append(token)
    except ValueError as exc:
        if _review_segment(argv) is not None:
            raise ToolPolicyRefusal(
                f"GitHub review submission refused: shell command could not be parsed ({exc})",
            ) from exc
        return None
    segment = _review_segment(argv)
    if segment is None:
        return None
    review_argv, start = segment
    repository_context_known = True
    prefix = argv[:start]
    if "cd" in prefix:
        repository_context_known = False
        if (
            len(prefix) == 3
            and prefix[0] == "cd"
            and prefix[2] == "&&"
            and not any(character in prefix[1] for character in "$`*?[]{}")
        ):
            target = Path(prefix[1]).expanduser()
            if not target.is_absolute():
                target = (effective_cwd or Path.cwd()) / target
            cwd = str(target.resolve())
            repository_context_known = True
    if _option_value(review_argv[3:], {"--repo", "-R"}):
        repository_context_known = True
    return review_argv, cwd, repository_context_known


def _review_refusal(reason: str) -> ToolPolicyRefusal:
    return ToolPolicyRefusal(f"GitHub review submission refused: {reason}")


def review_submission_from_request(request: Any) -> ReviewSubmission | None:
    parsed = _review_argv(request)
    if parsed is None:
        return None
    argv, cwd, repository_context_known = parsed
    if len(argv) < 3 or Path(argv[0]).name != "gh" or argv[1:3] != ["pr", "review"]:
        return None
    if len(argv) < 4:
        raise _review_refusal("an approval, change request, or comment flag is required")

    state_flags = {
        "--approve": "APPROVED",
        "-a": "APPROVED",
        "--request-changes": "CHANGES_REQUESTED",
        "-r": "CHANGES_REQUESTED",
        "--comment": "COMMENTED",
        "-c": "COMMENTED",
    }
    value_options = {"--repo", "-R", "--body", "-b", "--body-file", "-F"}
    states: set[str] = set()
    number: int | None = None
    index = 3
    while index < len(argv):
        value = argv[index]
        if value in state_flags:
            states.add(state_flags[value])
            index += 1
            continue
        if value in value_options:
            if index + 1 >= len(argv):
                raise _review_refusal(f"option {value} requires a value")
            index += 2
            continue
        if any(value.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if value.startswith("-"):
            raise _review_refusal(f"unrecognised option {value}")
        try:
            parsed_number = int(value)
        except ValueError:
            raise _review_refusal(f"pull request number is not an integer: {value}") from None
        if number is not None:
            raise _review_refusal("more than one pull request number was provided")
        number = parsed_number
        index += 1
    if len(states) != 1:
        raise _review_refusal("exactly one review state flag is required")
    if number is not None and number <= 0:
        raise _review_refusal("pull request number must be positive")
    repo = _option_value(argv[3:], {"--repo", "-R"})
    return ReviewSubmission(
        argv[0], repo, number, states.pop(), cwd, repository_context_known,
    )


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


def _number(spec: ReviewSubmission, repo: str) -> int | None:
    if spec.number is not None:
        return spec.number
    value = _text(
        spec,
        ["pr", "view", "--repo", repo, "--json", "number", "--jq", ".number"],
    )
    try:
        number = int(value or "")
    except ValueError:
        return None
    return number if number > 0 else None


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
    if not spec.repo and not spec.repository_context_known:
        return None
    repo = _repo(spec)
    reviewer = _reviewer(spec)
    if not repo or not reviewer:
        return None
    number = _number(spec, repo)
    if number is None:
        return None
    spec = replace(spec, number=number)
    while True:
        head = _head(spec, repo)
        if not head:
            return None
        key = (repo.casefold(), number, head, reviewer.casefold(), spec.state)
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
            number=number,
            head=head,
            reviewer=reviewer,
            state=spec.state,
            duplicate=exists,
            _lock=lock,
        )
