"""GitHub adapter for the closed :class:`mimir.forge.ForgeClient` protocol."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import requests

from ..models import NormalizedPullRequestSnapshot, RepoPRActionScope
from .client import (
    CheckProjection,
    CommentProjection,
    FileProjection,
    ForgeError,
    ForgeResponseTooLarge,
    IssueTarget,
    PullRequestProjection,
    ReviewProjection,
    ReviewRequestProjection,
    ReviewVerdict,
)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_DIFF_BYTES = 524_288
_MAX_ITEMS = 500
_MAX_PAGES = 10
_MAX_BODY_BYTES = 65_536
# Identity bindings intentionally expire only with the process. Effects never
# refresh this cache, so a changed or missing credential fails closed.
GITHUB_IDENTITY_CACHE_TTL_SECONDS: None = None
_identity_lock = threading.Lock()
_verified_identity: tuple[str, str] | None = None


def _diff_path(section: bytes) -> str:
    header = section.splitlines()[0] if section else b""
    marker = b" b/"
    if marker in header:
        path = header.rsplit(marker, 1)[1]
    elif b' "b/' in header:
        path = header.rsplit(b' "b/', 1)[1].removesuffix(b'"')
    else:
        path = b"(unknown)"
    text = path[:4_096].decode("utf-8", errors="replace")
    return "".join(character if ord(character) >= 32 else "?" for character in text)


def _diff_truncation_marker(
    *, original_bytes: int, omitted: list[tuple[str, str, int]], paths_shown: int | None = None,
) -> bytes:
    shown = omitted if paths_shown is None else omitted[:paths_shown]
    reasons = list(dict.fromkeys(reason for _path, reason, _size in omitted))
    lines = [
        "",
        "[pr_diff truncated]",
        f"truncation_reasons: {', '.join(reasons)}",
        f"original_bytes: {original_bytes}",
        f"max_bytes: {_MAX_DIFF_BYTES}",
        f"omitted_file_count: {len(omitted)}",
        "omitted_files:",
    ]
    lines.extend(
        f"- {json.dumps(path, ensure_ascii=True)} ({reason}, {size} bytes)"
        for path, reason, size in shown
    )
    if len(shown) < len(omitted):
        lines.append(f"- [{len(omitted) - len(shown)} additional paths not shown]")
    return ("\n".join(lines) + "\n").encode("utf-8")


def bound_diff(diff: str) -> str:
    """Return a UTF-8 bounded diff containing only complete file sections."""
    raw = diff.encode("utf-8")
    if len(raw) <= _MAX_DIFF_BYTES:
        return diff

    starts = [match.start() for match in re.finditer(br"(?m)^diff --git ", raw)]
    if not starts:
        sections = [raw]
    else:
        starts.append(len(raw))
        sections = [raw[starts[index]:starts[index + 1]] for index in range(len(starts) - 1)]
        if starts[0]:
            sections[0] = raw[:starts[0]] + sections[0]

    included = list(range(len(sections)))
    omitted: dict[int, tuple[str, str, int]] = {
        index: (_diff_path(section), "per_file_byte_limit", len(section))
        for index, section in enumerate(sections)
        if len(section) > _MAX_DIFF_BYTES
    }
    included = [index for index in included if index not in omitted]

    while True:
        ordered_omitted = [omitted[index] for index in sorted(omitted)]
        marker = _diff_truncation_marker(
            original_bytes=len(raw), omitted=ordered_omitted,
        )
        output_bytes = sum(len(sections[index]) for index in included) + len(marker)
        if output_bytes <= _MAX_DIFF_BYTES:
            break
        index = max(included, key=lambda item: (len(sections[item]), item))
        included.remove(index)
        section = sections[index]
        omitted[index] = (_diff_path(section), "whole_diff_byte_limit", len(section))

    if len(marker) > _MAX_DIFF_BYTES:
        paths_shown = len(ordered_omitted)
        while len(marker) > _MAX_DIFF_BYTES and paths_shown:
            paths_shown -= 1
            marker = _diff_truncation_marker(
                original_bytes=len(raw), omitted=ordered_omitted, paths_shown=paths_shown,
            )
    bounded = b"".join(sections[index] for index in sorted(included)) + marker
    return bounded.decode("utf-8")


class GitHubIdentityFailureKind(StrEnum):
    """Typed provenance for deciding whether identity verification may retry."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


class _GitHubRequestError(ForgeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class GitHubIdentityVerificationError(ForgeError):
    """A safe, pre-effect failure to bind credentials to a declared login."""

    def __init__(
        self,
        message: str,
        *,
        declared_login: str = "",
        authenticated_login: str = "",
        failure_kind: GitHubIdentityFailureKind = GitHubIdentityFailureKind.PERMANENT,
    ) -> None:
        super().__init__(message)
        self.declared_login = declared_login
        self.authenticated_login = authenticated_login
        self.failure_kind = failure_kind


def _credential_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def confirm_github_identity(principal: str, token: str | None = None) -> str:
    """Confirm *principal* against the process-cached authenticated identity."""
    expected = principal.strip()
    credential = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
    fingerprint = _credential_fingerprint(credential.strip())
    with _identity_lock:
        verified = _verified_identity
    if verified is None:
        raise GitHubIdentityVerificationError(
            "github identity verification cache is empty",
            declared_login=expected,
        )
    login, verified_fingerprint = verified
    if fingerprint != verified_fingerprint:
        raise GitHubIdentityVerificationError(
            "github identity verification cache does not match active credential",
            declared_login=expected,
            authenticated_login=login,
        )
    if login.casefold() != expected.casefold():
        raise GitHubIdentityVerificationError(
            f"github acting identity mismatch: authenticated as {login}, scope principal is {principal}",
            declared_login=expected,
            authenticated_login=login,
        )
    return login


class GitHubForgeClient:
    """Construct and execute bounded GitHub REST requests inside the adapter."""

    def __init__(
        self,
        *,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self._session = session or requests.Session()
        self._timeout = timeout

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        token = self._token
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mimir-forge",
        }
        if token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
        return headers

    def verify_identity(self, declared_login: str) -> str:
        """Resolve and process-cache the token owner, refusing any mismatch."""
        expected = declared_login.strip()
        if not expected:
            raise GitHubIdentityVerificationError("github declared identity is empty")
        fingerprint = _credential_fingerprint(self._token.strip())
        global _verified_identity
        with _identity_lock:
            if _verified_identity is not None:
                login, cached_fingerprint = _verified_identity
                if cached_fingerprint != fingerprint:
                    raise GitHubIdentityVerificationError(
                        "github identity verification cache does not match active credential",
                        declared_login=expected,
                        authenticated_login=login,
                    )
                if login.casefold() != expected.casefold():
                    raise GitHubIdentityVerificationError(
                        f"github identity mismatch: authenticated as {login}, declared as {expected}",
                        declared_login=expected,
                        authenticated_login=login,
                    )
                return login
            try:
                data = self._request("GET", "/user")
            except _GitHubRequestError as exc:
                raise GitHubIdentityVerificationError(
                    str(exc),
                    declared_login=expected,
                    failure_kind=(
                        GitHubIdentityFailureKind.TRANSIENT
                        if exc.retryable
                        else GitHubIdentityFailureKind.PERMANENT
                    ),
                ) from exc
            login = str(data.get("login", "")).strip() if isinstance(data, Mapping) else ""
            if _REVIEWER.fullmatch(login) is None:
                raise GitHubIdentityVerificationError(
                    "github identity verification returned an invalid login",
                    declared_login=expected,
                )
            if login.casefold() != expected.casefold():
                raise GitHubIdentityVerificationError(
                    f"github identity mismatch: authenticated as {login}, declared as {expected}",
                    declared_login=expected,
                    authenticated_login=login,
                )
            _verified_identity = (login, fingerprint)
            return login

    def _confirm_effect_identity(self, scope: RepoPRActionScope) -> None:
        confirm_github_identity(scope.principal, self._token)

    @staticmethod
    def _target(scope: RepoPRActionScope) -> tuple[str, int]:
        repository = scope.canonical_repo
        number = scope.pr_number
        if (
            _REPOSITORY.fullmatch(repository) is None
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
        ):
            raise ForgeError("invalid immutable pull-request scope")
        return repository, number

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        body: Mapping[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        max_bytes: int | None = _MAX_RESPONSE_BYTES,
        not_found: str = "pull request not found",
    ) -> Any:
        url = f"https://api.github.com{endpoint}"
        if body is not None and len(
            json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ) > _MAX_BODY_BYTES:
            raise ForgeError("forge request body exceeded size limit")
        try:
            response = self._session.request(
                method,
                url,
                headers=self._headers(accept),
                json=dict(body) if body is not None else None,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise _GitHubRequestError(
                f"forge transport failed: {type(exc).__name__}", retryable=True,
            ) from exc
        raw = response.content
        if max_bytes is not None and len(raw) > max_bytes:
            raise ForgeResponseTooLarge("forge response exceeded size limit")
        if response.status_code >= 400:
            reasons = {
                401: "authentication failed",
                403: "operation forbidden",
                404: not_found,
                409: "operation conflicted",
                422: "operation rejected",
                429: "rate limited",
            }
            raise _GitHubRequestError(
                reasons.get(response.status_code, "forge request failed"),
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        if not raw:
            return None
        if "application/json" not in response.headers.get("Content-Type", ""):
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ForgeError("forge returned invalid text") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise ForgeError("forge returned invalid JSON") from exc

    def _paginate(self, endpoint: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, _MAX_PAGES + 1):
            payload = self._request("GET", f"{endpoint}{separator}per_page=50&page={page}")
            if not isinstance(payload, list):
                raise ForgeError("forge returned an invalid collection")
            page_items = [item for item in payload if isinstance(item, Mapping)]
            items.extend(page_items)
            if len(items) > _MAX_ITEMS:
                raise ForgeResponseTooLarge("forge collection exceeded item limit")
            if len(payload) < 50:
                return items
        raise ForgeResponseTooLarge("forge collection exceeded page limit")

    @staticmethod
    def _text(value: Any, limit: int = 65_536) -> str:
        return str(value or "")[:limit]

    @staticmethod
    def _body(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or "\x00" in value
            or len(value.encode("utf-8")) > _MAX_BODY_BYTES
        ):
            raise ForgeError("invalid or oversized body")
        return value

    @staticmethod
    def _path(value: str) -> str:
        parts = value.split("/") if isinstance(value, str) else []
        if (
            not parts
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(character) < 32 for character in value)
            or len(value.encode("utf-8")) > 4_096
        ):
            raise ForgeError("invalid repository path")
        return value

    @staticmethod
    def _user(payload: Mapping[str, Any]) -> str:
        user = payload.get("user")
        return str(user.get("login", "")) if isinstance(user, Mapping) else ""

    def get_pull_request_snapshot(
        self, repository: str, number: int,
    ) -> NormalizedPullRequestSnapshot:
        """Fetch and normalize all authority-bearing PR facts server-side."""
        if (
            _REPOSITORY.fullmatch(repository) is None
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
        ):
            raise ForgeError("invalid pull-request selector")
        data = self._request("GET", f"/repos/{repository}/pulls/{number}")
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid pull-request metadata")
        observed_number = data.get("number")
        if (
            not isinstance(observed_number, int)
            or isinstance(observed_number, bool)
            or observed_number != number
        ):
            raise ForgeError("forge returned invalid pull-request metadata")
        base = data.get("base") if isinstance(data.get("base"), Mapping) else {}
        head = data.get("head") if isinstance(data.get("head"), Mapping) else {}
        base_repo = base.get("repo") if isinstance(base.get("repo"), Mapping) else {}
        normalized_repo = str(base_repo.get("full_name", ""))
        head_repo = head.get("repo") if isinstance(head.get("repo"), Mapping) else {}
        normalized_head_repo = str(head_repo.get("full_name", ""))
        return NormalizedPullRequestSnapshot(
            repo=normalized_repo,
            state=str(data.get("state", "")),
            number=observed_number,
            author=self._user(data),
            head_repo=normalized_head_repo,
            head_remote=(
                "origin" if normalized_head_repo.lower() == repository.lower() else "source"
            ),
            head_ref=str(head.get("ref", "")),
            head_sha=str(head.get("sha", "")),
            base_ref=str(base.get("ref", "")),
            base_sha=str(base.get("sha", "")),
        )

    def get_pull_request(self, scope: RepoPRActionScope) -> PullRequestProjection:
        repository, number = self._target(scope)
        data = self._request("GET", f"/repos/{repository}/pulls/{number}")
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid pull-request metadata")
        base = data.get("base") if isinstance(data.get("base"), Mapping) else {}
        head = data.get("head") if isinstance(data.get("head"), Mapping) else {}
        return PullRequestProjection(
            number=int(data.get("number", number)),
            title=self._text(data.get("title"), 1_024),
            state=self._text(data.get("state"), 32),
            author=self._user(data),
            draft=bool(data.get("draft", False)),
            base_ref=self._text(base.get("ref"), 255),
            head_ref=self._text(head.get("ref"), 255),
            head_sha=self._text(head.get("sha"), 64),
            mergeable=data.get("mergeable") if isinstance(data.get("mergeable"), bool) else None,
            created_at=self._text(data.get("created_at"), 64),
            updated_at=self._text(data.get("updated_at"), 64),
        )

    def list_files(self, scope: RepoPRActionScope) -> tuple[FileProjection, ...]:
        repository, number = self._target(scope)
        return tuple(
            FileProjection(
                path=self._text(item.get("filename"), 4_096),
                status=self._text(item.get("status"), 32),
                additions=int(item.get("additions", 0)),
                deletions=int(item.get("deletions", 0)),
                changes=int(item.get("changes", 0)),
                patch=self._text(item.get("patch")) if item.get("patch") is not None else None,
            )
            for item in self._paginate(f"/repos/{repository}/pulls/{number}/files")
        )

    def get_diff(self, scope: RepoPRActionScope) -> str:
        repository, number = self._target(scope)
        data = self._request(
            "GET",
            f"/repos/{repository}/pulls/{number}",
            accept="application/vnd.github.diff",
            max_bytes=None,
        )
        if not isinstance(data, str):
            raise ForgeError("forge returned an invalid diff")
        return bound_diff(data)

    def list_checks(self, scope: RepoPRActionScope) -> tuple[CheckProjection, ...]:
        repository, _number = self._target(scope)
        data = self._request(
            "GET", f"/repos/{repository}/commits/{scope.observed_head_sha}/check-runs?per_page=100",
        )
        rows = data.get("check_runs") if isinstance(data, Mapping) else None
        if not isinstance(rows, list) or len(rows) > _MAX_ITEMS:
            raise ForgeError("forge returned invalid checks")
        return tuple(
            CheckProjection(
                name=self._text(item.get("name"), 512),
                status=self._text(item.get("status"), 32),
                conclusion=self._text(item.get("conclusion"), 32) or None,
                started_at=self._text(item.get("started_at"), 64) or None,
                completed_at=self._text(item.get("completed_at"), 64) or None,
            )
            for item in rows if isinstance(item, Mapping)
        )

    def list_reviews(self, scope: RepoPRActionScope) -> tuple[ReviewProjection, ...]:
        repository, number = self._target(scope)
        return tuple(self._review(item) for item in self._paginate(
            f"/repos/{repository}/pulls/{number}/reviews"
        ))

    def _review(self, item: Mapping[str, Any]) -> ReviewProjection:
        return ReviewProjection(
            id=self._text(item.get("id"), 64),
            author=self._user(item),
            state=self._text(item.get("state"), 32).lower(),
            body=self._text(item.get("body")),
            submitted_at=self._text(item.get("submitted_at"), 64) or None,
            commit_sha=self._text(item.get("commit_id"), 64) or None,
        )

    def list_comments(self, scope: RepoPRActionScope) -> tuple[CommentProjection, ...]:
        repository, number = self._target(scope)
        issue = self._paginate(f"/repos/{repository}/issues/{number}/comments")
        inline = self._paginate(f"/repos/{repository}/pulls/{number}/comments")
        return tuple(self._comment(item) for item in (*issue, *inline))

    def _comment(self, item: Mapping[str, Any]) -> CommentProjection:
        line = item.get("line") or item.get("original_line")
        return CommentProjection(
            id=self._text(item.get("id"), 64),
            author=self._user(item),
            body=self._text(item.get("body")),
            created_at=self._text(item.get("created_at"), 64),
            updated_at=self._text(item.get("updated_at"), 64),
            path=self._text(item.get("path"), 4_096) or None,
            line=int(line) if isinstance(line, int) and not isinstance(line, bool) else None,
        )

    def list_review_requests(
        self, scope: RepoPRActionScope,
    ) -> tuple[ReviewRequestProjection, ...]:
        repository, number = self._target(scope)
        data = self._request("GET", f"/repos/{repository}/pulls/{number}/requested_reviewers")
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid review requests")
        users = data.get("users") if isinstance(data.get("users"), list) else []
        teams = data.get("teams") if isinstance(data.get("teams"), list) else []
        if len(users) + len(teams) > _MAX_ITEMS:
            raise ForgeResponseTooLarge("review requests exceeded item limit")
        return tuple(
            [
                ReviewRequestProjection(self._text(item.get("login"), 256), "user")
                for item in users if isinstance(item, Mapping)
            ]
            + [
                ReviewRequestProjection(self._text(item.get("slug"), 256), "team")
                for item in teams if isinstance(item, Mapping)
            ]
        )

    def submit_review(
        self, scope: RepoPRActionScope, verdict: ReviewVerdict, body: str,
    ) -> ReviewProjection:
        repository, number = self._target(scope)
        body = self._body(body)
        events = {
            ReviewVerdict.APPROVE: "APPROVE",
            ReviewVerdict.COMMENT: "COMMENT",
            ReviewVerdict.REQUEST_CHANGES: "REQUEST_CHANGES",
        }
        self._confirm_effect_identity(scope)
        data = self._request(
            "POST",
            f"/repos/{repository}/pulls/{number}/reviews",
            body={"commit_id": scope.observed_head_sha, "event": events[verdict], "body": body},
        )
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid review result")
        return self._review(data)

    def add_inline_review_comment(
        self, scope: RepoPRActionScope, *, path: str, line: int, body: str,
    ) -> CommentProjection:
        repository, number = self._target(scope)
        path = self._path(path)
        body = self._body(body)
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= 10_000_000:
            raise ForgeError("invalid line")
        self._confirm_effect_identity(scope)
        data = self._request(
            "POST",
            f"/repos/{repository}/pulls/{number}/comments",
            body={
                "body": body,
                "commit_id": scope.observed_head_sha,
                "path": path,
                "line": line,
                "side": "RIGHT",
            },
        )
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid comment result")
        return self._comment(data)

    def add_pull_request_comment(
        self, scope: RepoPRActionScope, body: str,
    ) -> CommentProjection:
        repository, number = self._target(scope)
        body = self._body(body)
        self._confirm_effect_identity(scope)
        data = self._request(
            "POST", f"/repos/{repository}/issues/{number}/comments", body={"body": body},
        )
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid comment result")
        return self._comment(data)

    def get_open_issue_target(self, repository: str, issue: int) -> IssueTarget:
        """Resolve an exact open issue from server-returned identity fields."""
        if (
            _REPOSITORY.fullmatch(repository) is None
            or not isinstance(issue, int)
            or isinstance(issue, bool)
            or issue < 1
        ):
            raise ForgeError("invalid issue selector")
        target = self._request(
            "GET", f"/repos/{repository}/issues/{issue}", not_found="issue not found",
        )
        if not isinstance(target, Mapping):
            raise ForgeError("forge returned invalid issue result")
        if target.get("pull_request") is not None:
            raise ForgeError("target is a pull request; use the pull-request comment path")
        if target.get("state") != "open":
            raise ForgeError("issue is not open")
        observed_number = target.get("number")
        repository_url = target.get("repository_url")
        prefix = "https://api.github.com/repos/"
        observed_repo = (
            repository_url.removeprefix(prefix)
            if isinstance(repository_url, str) and repository_url.startswith(prefix)
            else ""
        )
        if (
            not isinstance(observed_number, int)
            or isinstance(observed_number, bool)
            or observed_number != issue
            or _REPOSITORY.fullmatch(observed_repo) is None
            or observed_repo.casefold() != repository.casefold()
        ):
            raise ForgeError("forge returned mismatched issue identity")
        return IssueTarget(observed_repo, observed_number)

    def add_issue_comment(
        self, repository: str, issue: int, body: str,
    ) -> CommentProjection:
        """Comment on a server-resolved open issue, never a pull request."""
        body = self._body(body)
        target = self.get_open_issue_target(repository, issue)
        data = self._request(
            "POST",
            f"/repos/{target.canonical_repo}/issues/{target.issue_number}/comments",
            body={"body": body},
        )
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid comment result")
        return self._comment(data)

    def rerequest_review(self, scope: RepoPRActionScope, reviewer: str) -> None:
        repository, number = self._target(scope)
        if _REVIEWER.fullmatch(reviewer) is None:
            raise ForgeError("invalid reviewer")
        self._confirm_effect_identity(scope)
        self._request(
            "POST",
            f"/repos/{repository}/pulls/{number}/requested_reviewers",
            body={"reviewers": [reviewer]},
        )
