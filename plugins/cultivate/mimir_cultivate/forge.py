"""Cultivate-owned forge client, GitHub identity and scoped tools."""

from __future__ import annotations


from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from mimir.models import NormalizedPullRequestSnapshot
from .policy import RepoPRActionScope
from .review_checks import approval_blocker


class ForgeError(RuntimeError):
    """A normalized forge operation failure safe to expose to a tool caller."""


class ForgeReadUnavailable(ForgeError):
    """A fixed server diagnostic raised without exposing forge content."""

    def __init__(self) -> None:
        super().__init__("run is still in progress; retry after completion")


class ForgeResponseTooLarge(ForgeError):
    """A forge response exceeded the closed tool's configured bound."""


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


@dataclass(frozen=True)
class PullRequestProjection:
    number: int
    title: str
    state: str
    author: str
    draft: bool
    base_ref: str
    head_ref: str
    head_sha: str
    mergeable: bool | None
    created_at: str
    updated_at: str
    body: str = ""
    merged_at: str | None = None


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    state: str
    author: str
    head_ref: str
    base_ref: str
    head_sha: str
    updated_at: str
    merged_at: str | None
    url: str


@dataclass(frozen=True)
class FileProjection:
    path: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None


@dataclass(frozen=True)
class CheckProjection:
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    details_url: str | None = None


@dataclass(frozen=True)
class ReviewProjection:
    id: str
    author: str
    state: str
    body: str
    submitted_at: str | None
    commit_sha: str | None


@dataclass(frozen=True)
class CommentProjection:
    id: str
    author: str
    body: str
    created_at: str
    updated_at: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class IssueTarget:
    canonical_repo: str
    issue_number: int

    @property
    def sink_destination(self) -> str:
        return f"{self.canonical_repo}#issue/{self.issue_number}"


@dataclass(frozen=True)
class ReviewRequestProjection:
    reviewer: str
    kind: str


class ForgeClient(Protocol):
    """Narrow adapter contract; every target comes from an immutable scope."""

    def get_pull_request_snapshot(
        self, repository: str, number: int,
    ) -> NormalizedPullRequestSnapshot: ...

    def get_pull_request(self, scope: RepoPRActionScope) -> PullRequestProjection: ...

    def list_pull_requests(
        self, repository: str, *, state: str, base: str | None,
        head: str | None, limit: int,
        author: str | None = None, merged_since: datetime | None = None,
    ) -> tuple[PullRequestSummary, ...]: ...

    def search_pull_requests(
        self, repository: str, *, query: str, state: str, base: str | None,
        head: str | None, author: str | None, merged_since: datetime | None,
        limit: int,
    ) -> tuple[PullRequestSummary, ...]: ...

    def list_files(self, scope: RepoPRActionScope) -> tuple[FileProjection, ...]: ...

    def get_diff(self, scope: RepoPRActionScope) -> str: ...

    def get_file_content(self, scope: RepoPRActionScope, path: str) -> str: ...

    def list_checks(self, scope: RepoPRActionScope) -> tuple[CheckProjection, ...]: ...

    def get_job_log(
        self, scope: RepoPRActionScope, job_id: int, run_id: int | None = None,
    ) -> str: ...

    def list_run_jobs(self, repository: str, run_id: int) -> list[dict[str, Any]]: ...

    def get_run(self, repository: str, run_id: int) -> dict[str, Any]: ...

    def list_runs(
        self, repository: str, branch: str, workflow_id: int | None, limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_reviews(self, scope: RepoPRActionScope) -> tuple[ReviewProjection, ...]: ...

    def list_comments(self, scope: RepoPRActionScope) -> tuple[CommentProjection, ...]: ...

    def list_review_requests(
        self, scope: RepoPRActionScope,
    ) -> tuple[ReviewRequestProjection, ...]: ...

    def submit_review(
        self, scope: RepoPRActionScope, verdict: ReviewVerdict, body: str,
    ) -> ReviewProjection: ...

    def add_inline_review_comment(
        self, scope: RepoPRActionScope, *, path: str, line: int, body: str,
    ) -> CommentProjection: ...

    def add_pull_request_comment(
        self, scope: RepoPRActionScope, body: str,
    ) -> CommentProjection: ...

    def edit_pull_request_body(self, scope: RepoPRActionScope, body: str) -> None: ...

    def get_open_issue_target(self, repository: str, issue: int) -> IssueTarget: ...

    def add_issue_comment(
        self, repository: str, issue: int, body: str,
    ) -> CommentProjection: ...

    def rerequest_review(self, scope: RepoPRActionScope, reviewer: str) -> None: ...

import codecs
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import quote, urlencode

import requests

from mimir import github_identity as _core_identity
from mimir.github_identity import GitHubIdentityFailureKind
from mimir.github_identity import credential_fingerprint as _credential_fingerprint
from mimir.models import NormalizedPullRequestSnapshot
from .policy import RepoPRActionScope

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_SHA = re.compile(r"[a-fA-F0-9]{40}")
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_DIFF_BYTES = 524_288
_MAX_DIFF_FETCH_BYTES = 8_388_608
_FILE_TRUNCATION_MARKER = "\n[pr_file_content truncated: file exceeded size limit]\n"
_MAX_ITEMS = 500
_MAX_PAGES = 10
_MAX_BODY_BYTES = 65_536
# Identity bindings intentionally expire only with the process. Effects never
# refresh this cache, so a changed or missing credential fails closed. The
# cache is core's (mimir.github_identity), shared with core's credentialed gh.
GITHUB_IDENTITY_CACHE_TTL_SECONDS: None = None


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
        content_bytes = sum(len(sections[index]) for index in included)
        minimum_marker = _diff_truncation_marker(
            original_bytes=len(raw), omitted=ordered_omitted, paths_shown=0,
        )
        if content_bytes + len(minimum_marker) <= _MAX_DIFF_BYTES:
            break
        if not included:
            raise ForgeResponseTooLarge("forge diff truncation marker exceeded size limit")
        index = max(included, key=lambda item: (len(sections[item]), item))
        included.remove(index)
        section = sections[index]
        omitted[index] = (_diff_path(section), "whole_diff_byte_limit", len(section))

    paths_low = 0
    paths_high = len(ordered_omitted)
    while paths_low < paths_high:
        paths_shown = (paths_low + paths_high + 1) // 2
        candidate = _diff_truncation_marker(
            original_bytes=len(raw), omitted=ordered_omitted, paths_shown=paths_shown,
        )
        if content_bytes + len(candidate) <= _MAX_DIFF_BYTES:
            paths_low = paths_shown
        else:
            paths_high = paths_shown - 1
    marker = _diff_truncation_marker(
        original_bytes=len(raw), omitted=ordered_omitted, paths_shown=paths_low,
    )
    bounded = b"".join(sections[index] for index in sorted(included)) + marker
    return bounded.decode("utf-8")


class _GitHubRequestError(ForgeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class GitHubIdentityVerificationError(ForgeError, _core_identity.GitHubIdentityVerificationError):
    """Core's identity failure, also a ``ForgeError`` for provider callers."""


def _as_forge_identity_error(
    exc: _core_identity.GitHubIdentityVerificationError,
) -> GitHubIdentityVerificationError:
    if isinstance(exc, GitHubIdentityVerificationError):
        return exc
    return GitHubIdentityVerificationError(
        str(exc), declared_login=exc.declared_login,
        authenticated_login=exc.authenticated_login, failure_kind=exc.failure_kind,
    )


def confirm_github_identity(principal: str, token: str | None = None) -> str:
    """Confirm *principal* against core's process-cached authenticated identity."""
    try:
        return _core_identity.confirm_github_identity(principal, token)
    except _core_identity.GitHubIdentityVerificationError as exc:
        raise _as_forge_identity_error(exc) from exc


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

        def fetch_login() -> str:
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
            return str(data.get("login", "")) if isinstance(data, Mapping) else ""

        try:
            return _core_identity.verify_github_identity(expected, self._token, fetch_login)
        except _core_identity.GitHubIdentityVerificationError as exc:
            raise _as_forge_identity_error(exc) from exc

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
        max_bytes: int = _MAX_RESPONSE_BYTES,
        not_found: str = (
            "pull request not found; this may be an issue, not a PR; "
            "issues aren't handled on service turns yet"
        ),
        truncate_text: bool = False,
    ) -> Any:
        """Read a bounded response; oversized text is checked only through the cap.

        Invalid UTF-8 after the cap cannot be detected and a valid prefix is
        returned as truncated text instead.
        """
        url = f"https://api.github.com{endpoint}"
        if body is not None and len(
            json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ) > _MAX_BODY_BYTES:
            raise ForgeError("forge request body exceeded size limit")
        response = None
        try:
            response = self._session.request(
                method,
                url,
                headers=self._headers(accept),
                json=dict(body) if body is not None else None,
                timeout=self._timeout,
                stream=True,
            )
            # Error responses need only enough bytes to enforce small caps;
            # their payload is never returned or used to choose the reason.
            limit = min(max_bytes + 1, 65_536) if response.status_code >= 400 else max_bytes + 1
            collected = bytearray()
            for chunk in response.iter_content(chunk_size=65_536):
                collected.extend(chunk[:limit - len(collected)])
                if len(collected) >= limit:
                    break
        except requests.RequestException as exc:
            warning = (
                "; the write may have been applied; check pr_reviews / pr_comments "
                "before retrying"
                if method.upper() != "GET" else ""
            )
            raise _GitHubRequestError(
                f"forge transport failed: {type(exc).__name__}{warning}", retryable=True,
            ) from exc
        finally:
            if response is not None:
                response.close()
        raw = bytes(collected)
        if len(raw) > max_bytes:
            if not truncate_text or response.status_code >= 400:
                raise ForgeResponseTooLarge("forge response exceeded size limit")
            try:
                codecs.getincrementaldecoder("utf-8")("strict").decode(raw, final=False)
            except UnicodeDecodeError as exc:
                raise ForgeError("forge returned invalid text (binary file refused)") from exc
            prefix = raw[:max_bytes - len(_FILE_TRUNCATION_MARKER.encode("utf-8"))]
            return prefix.decode("utf-8", errors="ignore") + _FILE_TRUNCATION_MARKER
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
            return "" if accept == "application/vnd.github.raw" else None
        if accept == "application/vnd.github.raw":
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ForgeError("forge returned invalid text (binary file refused)") from exc
        if "application/json" not in response.headers.get("Content-Type", ""):
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ForgeError("forge returned invalid text") from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ForgeError("forge returned invalid JSON") from exc

    def _paginate(
        self, endpoint: str, *, collection_key: str | None = None,
        limit: int | None = None, merged_only: bool = False,
        predicate: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> list[Mapping[str, Any]]:
        """Count matching rows toward limit; limited queries return partial at 10 pages.

        Unlimited collections still raise on overflow. collection_key supports
        object-wrapped collections such as Actions jobs without losing this bound.
        """
        items: list[Mapping[str, Any]] = []
        separator = "&" if "?" in endpoint else "?"
        offset = 0
        page_size = 50
        for _ in range(_MAX_PAGES):
            # Filtering may reject every row, so retain scan depth independently
            # of the output limit. Keep any size reduction across later pages.
            if limit is not None and not merged_only and predicate is None:
                page_size = min(page_size, max(limit - len(items), 1))
            while True:
                # GitHub's page number is relative to per_page. Recompute it
                # after each shrink and discard any overlap with prior pages.
                page = offset // page_size + 1
                try:
                    payload = self._request(
                        "GET", f"{endpoint}{separator}per_page={page_size}&page={page}",
                    )
                    break
                except ForgeResponseTooLarge:
                    if page_size == 1:
                        raise
                    page_size = max(page_size // 2, 1)
            if collection_key is not None:
                payload = payload.get(collection_key) if isinstance(payload, Mapping) else None
            if not isinstance(payload, list):
                raise ForgeError("forge returned an invalid collection")
            page_rows = payload[offset % page_size:]
            offset += len(page_rows)
            page_items = [
                item for item in page_rows
                if isinstance(item, Mapping)
                and (not merged_only or item.get("merged_at") is not None)
                and (predicate is None or predicate(item))
            ]
            items.extend(page_items)
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if len(items) > _MAX_ITEMS:
                raise ForgeResponseTooLarge("forge collection exceeded item limit")
            if len(payload) < page_size:
                return items
        if limit is not None:
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

    def author_is_trusted(self, repository: str, author: str) -> bool | None:
        """Share poller collaborator/org attestation, not payload trust claims."""
        from mimir.pollers import _github_author_is_trusted

        return _github_author_is_trusted(
            repository, author, self._token, timeout=self._timeout,
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
            merged_at=self._text(data.get("merged_at"), 64) if isinstance(data.get("merged_at"), str) else None,
            body=self._text(
                data.get("body") if isinstance(data.get("body"), str) else "",
                _MAX_BODY_BYTES,
            ).encode("utf-8")[:_MAX_BODY_BYTES].decode("utf-8", errors="ignore"),
        )

    def list_pull_requests(
        self, repository: str, *, state: str, base: str | None = None,
        head: str | None = None, limit: int = 30,
        author: str | None = None, merged_since: datetime | None = None,
    ) -> tuple[PullRequestSummary, ...]:
        """List bounded, updated-descending PR summaries from the fixed GitHub host."""
        if _REPOSITORY.fullmatch(repository) is None:
            raise ForgeError("invalid repository selector")
        if state not in {"open", "closed", "merged", "all"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ForgeError("invalid pull-request list selector")
        query = {"state": "closed" if state == "merged" else state,
                 "sort": "updated", "direction": "desc"}
        if base is not None:
            query["base"] = base
        head_branch = head.partition(":")[2] if head is not None and ":" in head else head
        if head is not None:
            query["head"] = head if ":" in head else f"{repository.split('/')[0]}:{head}"

        def matches(item: Mapping[str, Any]) -> bool:
            head_data = item.get("head")
            if head_branch is not None and (
                not isinstance(head_data, Mapping) or head_data.get("ref") != head_branch
            ):
                return False
            if author is not None and self._user(item).casefold() != author.casefold():
                return False
            if merged_since is not None:
                merged_at = item.get("merged_at")
                if not isinstance(merged_at, str):
                    return False
                try:
                    merged = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                    merged = merged.replace(tzinfo=merged.tzinfo or timezone.utc)
                except ValueError as exc:
                    raise ForgeError("forge returned invalid merged timestamp") from exc
                if merged < merged_since:
                    return False
            return True

        rows = self._paginate(
            f"/repos/{repository}/pulls?{urlencode(query)}",
            limit=limit, merged_only=state == "merged",
            predicate=matches if any(value is not None for value in (head, author, merged_since)) else None,
        )
        return tuple(
            PullRequestSummary(
                number=int(item.get("number", 0)),
                title=self._text(item.get("title"), 1_024),
                state="merged" if item.get("merged_at") is not None else self._text(item.get("state"), 32),
                author=self._text(self._user(item), 256),
                head_ref=self._text(head_data.get("ref"), 255),
                base_ref=self._text(base_data.get("ref"), 255),
                head_sha=self._text(head_data.get("sha"), 64),
                updated_at=self._text(item.get("updated_at"), 64),
                merged_at=self._text(item.get("merged_at"), 64) or None,
                url=self._text(item.get("html_url"), 4_096),
            )
            for item in rows
            for head_data, base_data in [(
                item.get("head") if isinstance(item.get("head"), Mapping) else {},
                item.get("base") if isinstance(item.get("base"), Mapping) else {},
            )]
        )

    def search_pull_requests(
        self, repository: str, *, query: str, state: str, base: str | None = None,
        head: str | None = None, author: str | None = None,
        merged_since: datetime | None = None, limit: int = 30,
    ) -> tuple[PullRequestSummary, ...]:
        """Search PRs server-side on the fixed GitHub host, before applying the limit."""
        if _REPOSITORY.fullmatch(repository) is None:
            raise ForgeError("invalid repository selector")
        if state not in {"open", "closed", "merged", "all"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ForgeError("invalid pull-request search selector")
        qualifiers = [query, "is:pr", f"repo:{repository}"]
        if state != "all":
            qualifiers.append(f"is:{state}")
        if author is not None:
            qualifiers.append(f"author:{author}")
        if base is not None:
            qualifiers.append(f"base:{base}")
        if head is not None:
            qualifiers.append(f"head:{head.partition(':')[2] if ':' in head else head}")
        if merged_since is not None:
            qualifiers.append(f"merged:>={merged_since.date().isoformat()}")
        search_query = " ".join(qualifiers)
        # Validate the final query too: direct callers can supply selectors as
        # well as query text, and advanced search can otherwise escape repo:.
        if any(char in search_query for char in '\"()') or any(
            token.casefold() in {"or", "and", "not"} for token in search_query.split()
        ):
            raise ForgeError("pull-request search cannot contain quotes, parentheses, or boolean operators")
        # GitHub ORs repeated repo: qualifiers; reject even a duplicate of our repo.
        if search_query.casefold().count("repo:") != 1 or f"repo:{repository}" not in search_query.split():
            raise ForgeError("pull-request search must target exactly the configured repository")
        rows = self._paginate(
            f"/search/issues?{urlencode({'q': search_query, 'sort': 'updated', 'order': 'desc'})}",
            collection_key="items", limit=limit,
        )
        return tuple(
            PullRequestSummary(
                number=int(item.get("number", 0)),
                title=self._text(item.get("title"), 1_024),
                state="merged" if merged_at is not None else self._text(item.get("state"), 32),
                author=self._text(self._user(item), 256),
                head_ref="", base_ref="", head_sha="",
                updated_at=self._text(item.get("updated_at"), 64),
                merged_at=self._text(merged_at, 64) or None,
                url=self._text(item.get("html_url"), 4_096),
            )
            for item in rows
            for pr_data in [item.get("pull_request") if isinstance(item.get("pull_request"), Mapping) else {}]
            for merged_at in [pr_data.get("merged_at")]
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
            max_bytes=_MAX_DIFF_FETCH_BYTES,
        )
        if not isinstance(data, str):
            raise ForgeError("forge returned an invalid diff")
        return bound_diff(data)

    def get_file_content(self, scope: RepoPRActionScope, path: str) -> str:
        repository, _number = self._target(scope)
        # Validate before requesting the pinned commit or walking its tree.
        parts = path.split("/") if isinstance(path, str) else []
        if (
            not parts or not path or path.startswith("/")
            or any(part in {".", ".."} for part in parts)
            or any(not part for part in parts[1:]) or "\\" in path
            or any(ord(character) < 32 for character in path)
            or len(path.encode("utf-8")) > 4_096
        ):
            raise ForgeError("invalid repository path")
        # The pinned commit's Git tree proves every component's actual mode;
        # unlike Contents, it never dereferences a symlink to a regular file.
        commit = self._request(
            "GET", f"/repos/{repository}/git/commits/{scope.observed_head_sha}",
        )
        tree = commit.get("tree") if isinstance(commit, Mapping) else None
        tree_sha = tree.get("sha") if isinstance(tree, Mapping) else None
        if (
            not isinstance(commit, Mapping)
            or commit.get("sha") != scope.observed_head_sha
            or not isinstance(tree_sha, str) or _SHA.fullmatch(tree_sha) is None
        ):
            raise ForgeError("file content refused: scoped commit tree is unavailable")
        for index, part in enumerate(parts):
            listing = self._request("GET", f"/repos/{repository}/git/trees/{tree_sha}")
            entries = listing.get("tree") if isinstance(listing, Mapping) else None
            if not isinstance(entries, list):
                raise ForgeError("file content refused: scoped tree is unavailable")
            matches = [entry for entry in entries if isinstance(entry, Mapping) and entry.get("path") == part]
            if not matches:
                missing = f" (missing component {part!r})" if index < len(parts) - 1 else ""
                raise ForgeError(
                    f"file content refused: path not found at scoped head "
                    f"{scope.observed_head_sha[:12]}{missing}; list the PR's changed paths "
                    "with pr_files, or check the path's spelling"
                )
            if len(matches) != 1:
                raise ForgeError("file content refused: scoped tree is unavailable")
            entry = matches[0]
            final = index == len(parts) - 1
            if final:
                if (
                    entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}
                ):
                    raise ForgeError("file content refused: path is not a regular file at scoped head")
            elif entry.get("type") != "tree" or entry.get("mode") != "040000":
                raise ForgeError("file content refused: path crosses a symlink or submodule")
            tree_sha = entry.get("sha")
            if not isinstance(tree_sha, str) or _SHA.fullmatch(tree_sha) is None:
                raise ForgeError("file content refused: invalid scoped tree entry")
        # Fetch the verified blob once: no base64 Contents metadata response
        # can exceed the smaller JSON cap before this bounded raw read.
        data = self._request(
            "GET", f"/repos/{repository}/git/blobs/{tree_sha}",
            accept="application/vnd.github.raw",
            max_bytes=_MAX_DIFF_FETCH_BYTES, not_found="file not found at scoped head",
            truncate_text=True,
        )
        if not isinstance(data, str):
            raise ForgeError("file content refused: directory, symlink or submodule response")
        return data

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
                details_url=(
                    self._text(item.get("details_url"), 4_096)
                    or self._text(item.get("html_url"), 4_096)
                    or None
                ),
            )
            for item in rows if isinstance(item, Mapping)
        )

    def get_job_log(
        self, scope: RepoPRActionScope, job_id: int, run_id: int | None = None,
    ) -> str:
        """Read one failing job only after independently binding its run and head."""
        repository, _number = self._target(scope)
        for name, value in (("job_id", job_id), ("run_id", run_id)):
            if name == "run_id" and value is None:
                continue
            if type(value) is not int or value < 1:
                raise ForgeError(f"{name} must be a positive integer")
        job = self._request(
            "GET", f"/repos/{repository}/actions/jobs/{job_id}",
            not_found="job not found",
        )
        if not isinstance(job, Mapping):
            raise ForgeError("invalid job metadata")
        observed_run = job.get("run_id")
        if (
            type(job.get("id")) is not int or job["id"] != job_id
            or type(observed_run) is not int or observed_run < 1
            or (run_id is not None and observed_run != run_id)
            or job.get("head_sha") != scope.observed_head_sha
            or not isinstance(job.get("run_url"), str)
            or job["run_url"].casefold() != f"https://api.github.com/repos/{repository}/actions/runs/{observed_run}".casefold()
        ):
            raise ForgeError("job is outside the scoped repository/run/head")
        run = self._request(
            "GET", f"/repos/{repository}/actions/runs/{observed_run}",
            not_found="run not found",
        )
        if not isinstance(run, Mapping):
            raise ForgeError("invalid run metadata")
        repo = run.get("repository")
        repo_name = repo.get("full_name") if isinstance(repo, Mapping) else None
        if (
            type(run.get("id")) is not int or run["id"] != observed_run
            or not isinstance(repo_name, str)
            or repo_name.casefold() != repository.casefold()
            or run.get("head_sha") != scope.observed_head_sha
        ):
            raise ForgeError("run is outside the scoped repository/run/head")
        if run.get("status") != "completed":

            raise ForgeReadUnavailable()
        if job.get("status") != "completed" or job.get("conclusion") not in {
            "failure", "timed_out", "startup_failure", "action_required", "cancelled",
        }:
            raise ForgeError("job is not a completed failing job")

        from .ci import capture_job_log as capture_bounded_job_log

        text, error = capture_bounded_job_log(
            repository, job_id, token=self._token, limit=LOG_EXCERPT_BYTES,
            timeout=self._timeout,
        )
        if error:
            raise ForgeError(error)
        return text.decode("utf-8")

    def list_run_jobs(self, repository: str, run_id: int) -> list[dict[str, Any]]:
        """Return only bounded job and failed-step metadata for a named run."""
        if _REPOSITORY.fullmatch(repository) is None or type(run_id) is not int or run_id < 1:
            raise ForgeError("invalid workflow run selector")
        jobs = self._paginate(
            f"/repos/{repository}/actions/runs/{run_id}/jobs",
            collection_key="jobs", limit=100,
        )
        return [
            {
                "id": job.get("id") if type(job.get("id")) is int else None,
                "name": self._text(job.get("name"), 200),
                "status": self._text(job.get("status"), 32),
                "conclusion": self._text(job.get("conclusion"), 32) or None,
                "started_at": self._text(job.get("started_at"), 64) or None,
                "completed_at": self._text(job.get("completed_at"), 64) or None,
                "failed_steps": [
                    {
                        "number": step.get("number") if type(step.get("number")) is int else None,
                        "name": self._text(step.get("name"), 200),
                        "conclusion": self._text(step.get("conclusion"), 32),
                    }
                    for step in (job.get("steps") if isinstance(job.get("steps"), list) else [])[:100]
                    if isinstance(step, Mapping) and step.get("conclusion") == "failure"
                ],
            }
            for job in jobs
        ]

    @classmethod
    def _run_projection(cls, run: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": run.get("id") if type(run.get("id")) is int else None,
            "name": cls._text(run.get("name"), 200),
            "display_title": cls._text(run.get("display_title"), 200),
            "status": cls._text(run.get("status"), 32),
            "conclusion": cls._text(run.get("conclusion"), 32) or None,
            "event": cls._text(run.get("event"), 100),
            "head_branch": cls._text(run.get("head_branch"), 200),
            "head_sha": cls._text(run.get("head_sha"), 64),
            "head_repository": cls._text(
                run.get("head_repository", {}).get("full_name")
                if isinstance(run.get("head_repository"), Mapping) else None, 200,
            ),
            "run_attempt": run.get("run_attempt") if type(run.get("run_attempt")) is int else None,
            "workflow_id": run.get("workflow_id") if type(run.get("workflow_id")) is int else None,
            "created_at": cls._text(run.get("created_at"), 64) or None,
            "updated_at": cls._text(run.get("updated_at"), 64) or None,
            "run_started_at": cls._text(run.get("run_started_at"), 64) or None,
        }

    def get_run(self, repository: str, run_id: int) -> dict[str, Any]:
        if _REPOSITORY.fullmatch(repository) is None or type(run_id) is not int or run_id < 1:
            raise ForgeError("invalid workflow run selector")
        run = self._request("GET", f"/repos/{repository}/actions/runs/{run_id}")
        if not isinstance(run, Mapping):
            raise ForgeError("invalid run metadata")
        return self._run_projection(run)

    def list_runs(
        self, repository: str, branch: str, workflow_id: int | None, limit: int,
    ) -> list[dict[str, Any]]:
        if (
            _REPOSITORY.fullmatch(repository) is None
            or not isinstance(branch, str) or not branch
            or (workflow_id is not None and (type(workflow_id) is not int or workflow_id < 1))
            or type(limit) is not int or not 1 <= limit <= 20
        ):
            raise ForgeError("invalid workflow runs selector")
        path = (
            f"/repos/{repository}/actions/workflows/{workflow_id}/runs"
            if workflow_id is not None else f"/repos/{repository}/actions/runs"
        )
        runs = self._paginate(
            f"{path}?branch={quote(branch, safe='')}",
            collection_key="workflow_runs", limit=limit,
        )
        return [self._run_projection(run) for run in runs]

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
        if verdict == ReviewVerdict.APPROVE:
            try:
                live = self._request("GET", f"/repos/{repository}/pulls/{number}")
            except ForgeError as exc:
                raise ForgeError("approval refused: reviewed PR head could not be read") from exc
            if not isinstance(live, Mapping) or not isinstance(live.get("head"), Mapping) or live["head"].get("sha") != scope.observed_head_sha:
                raise ForgeError("approval refused: reviewed PR head is no longer current")
            try:
                checks = self._request(
                    "GET", f"/repos/{repository}/commits/{scope.observed_head_sha}/check-runs?per_page=100",
                )
                statuses = self._request(
                    "GET", f"/repos/{repository}/commits/{scope.observed_head_sha}/status",
                )
                blocker = approval_blocker(checks, statuses)
            except (ForgeError, requests.RequestException) as exc:
                raise ForgeError("approval refused: CI checks could not be read") from exc
            if blocker:
                raise ForgeError(f"approval refused: {blocker}")
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

    def edit_pull_request_body(self, scope: RepoPRActionScope, body: str) -> None:
        """Update only the description, never other pull-request metadata."""
        repository, number = self._target(scope)
        body = self._body(body)
        self._confirm_effect_identity(scope)
        self._request(
            "PATCH", f"/repos/{repository}/pulls/{number}", body={"body": body},
        )

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

from .github_trust import WithheldItem, placeholder, sanitize_login, sanitize_url
from .tracker.record import strip_external_record
from .ci import LOG_EXCERPT_BYTES, capture_job_log, clean_log_tail

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import asdict
from datetime import date, datetime, time, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException, tool
from langchain_core.tools.base import create_schema_from_function
from pydantic import StrictInt

from mimir.redaction import redact_text
from mimir.models import AuthContext
from .policy import (
    RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState,
    ServerDiscoveredPRScopeStore, ServerDiscoveredPRStates,
)
from mimir.tools.refusals import ToolPolicyRefusal

log = logging.getLogger(__name__)

_BODY_MAX_BYTES = 65_536
_PATH_MAX_BYTES = 4_096
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_BOT_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}\[bot\]", re.I)
_DIAGNOSTIC_LOGIN = re.compile(r"(?:[A-Za-z0-9_.-]{1,100}|[A-Za-z0-9-]{1,39}\[bot\])", re.I)
_CHECK_NAME = re.compile(r"[A-Za-z0-9 _./()\[\],-]{1,128}")
_CHECK_STATUSES = frozenset({"queued", "in_progress", "completed", "waiting", "pending", "requested"})
_CHECK_CONCLUSIONS = frozenset({
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out",
    "action_required", "startup_failure", "stale",
})
_ESCALATION_DESCRIPTION_MAX_BYTES = 4_096
_ESCALATION_ATTEMPT_MAX_BYTES = 512
_ESCALATION_MAX_ATTEMPTS = 16
_SCOPE_REFUSAL_TARGET_LIMIT = 5
_clients: dict[str, ForgeClient] = {}
_default_client: ForgeClient | None = None
_escalation_lock = threading.Lock()
_github_identity_degraded = False
_github_identity_degraded_error: ForgeError | None = None
_github_identity_degraded_lock = threading.Lock()
_github_identity_degraded_callback: Any | None = None


def set_github_identity_degraded_callback(
    callback: Any | None, *, notify_current: bool = False,
) -> None:
    """Install the process callback that emits the degraded event and alert."""
    global _github_identity_degraded_callback
    _github_identity_degraded_callback = callback
    if notify_current and callback is not None:
        with _github_identity_degraded_lock:
            current = _github_identity_degraded_error
        if current is not None:
            callback(current)


def github_identity_is_degraded() -> bool:
    """Return whether GitHub identity verification latched coding off."""
    with _github_identity_degraded_lock:
        return _github_identity_degraded


def github_identity_recovery_pending() -> bool:
    """Return whether coding is disabled by an unverified transient failure."""
    with _github_identity_degraded_lock:
        current = _github_identity_degraded_error
    return (
        current is not None
        and getattr(current, "failure_kind", None) == GitHubIdentityFailureKind.TRANSIENT
    )


def _latch_github_identity_degraded(exc: ForgeError) -> None:
    global _github_identity_degraded, _github_identity_degraded_error
    with _github_identity_degraded_lock:
        if (
            _github_identity_degraded
            and getattr(_github_identity_degraded_error, "failure_kind", None)
            != GitHubIdentityFailureKind.TRANSIENT
        ):
            return
        _github_identity_degraded = True
        _github_identity_degraded_error = exc
    callback = _github_identity_degraded_callback
    if callback is not None:
        callback(exc)


def register_forge_client(client: ForgeClient, *, repositories: tuple[str, ...]) -> None:
    """Register an adapter for exact repositories without changing the tools."""
    for repository in repositories:
        normalized = repository.strip().lower()
        if not normalized or "/" not in normalized:
            raise ValueError("invalid repository registration")
        _clients[normalized] = client


def set_forge_client(client: ForgeClient | None) -> None:
    """Set the fallback adapter used when no repository registration exists."""
    global _default_client
    _default_client = client


def initialize_github_forge_identity() -> bool:
    """Bind the declared GitHub identity, degrading coding on any mismatch."""
    global _github_identity_degraded, _github_identity_degraded_error
    if github_identity_is_degraded() and not github_identity_recovery_pending():
        return False
    declared_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if not declared_login:
        _latch_github_identity_degraded(
            GitHubIdentityVerificationError("github declared identity is empty")
        )
        return False
    client = GitHubForgeClient()
    try:
        client.verify_identity(declared_login)
    except ForgeError as exc:
        _latch_github_identity_degraded(exc)
        return False
    set_forge_client(client)
    with _github_identity_degraded_lock:
        _github_identity_degraded = False
        _github_identity_degraded_error = None
    return True


def confirm_github_tool_identity(principal: str, token: str | None = None) -> str:
    """Refuse safely and latch coding off when the process binding no longer holds."""
    if github_identity_is_degraded():
        raise ToolPolicyRefusal(
            "coding capability is disabled until restart after GitHub identity verification failed"
        )
    try:
        return confirm_github_identity(principal, token)
    except ForgeError as exc:
        _latch_github_identity_degraded(exc)
        raise ToolPolicyRefusal(
            f"coding capability disabled: GitHub identity verification failed: {exc}"
        ) from exc


def _scope(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
) -> RepoPRActionScope:
    # PR action policy is enforced by access_control.authorize_repo_pr_tool.
    # This layer only resolves the server-issued scope used for safe execution.
    return resolve_review_state(runtime, repository, pull_request).action_scope


def resolve_review_state(
    runtime: ToolRuntime[AuthContext] | None,
    repository: str,
    pull_request: int,
) -> Any:
    """Resolve existing narrow authority or derive standing live review authority."""
    context = getattr(runtime, "context", None)
    return resolve_review_state_for_context(context, repository, pull_request)


def _scope_miss_refusal(
    cache: Any,
    registry: Any,
    repository: str,
    pull_request: int,
) -> str | None:
    states: tuple[RepoReviewState, ...] = ()
    if isinstance(cache, ServerDiscoveredPRStates):
        states = cache.review_states
    if isinstance(registry, RepoPRScopeRegistry):
        states += registry.review_states

    targets = tuple(dict.fromkeys(
        (state.repo.lower(), state.pr_number) for state in states
    ))
    if not targets:
        return None

    shown = targets[:_SCOPE_REFUSAL_TARGET_LIMIT]
    rendered = "; ".join(
        f"repository={json.dumps(repo)}, pull_request={number}"
        for repo, number in shown
    )
    if len(targets) > len(shown):
        rendered += (
            f"; [{len(shown)} of {len(targets)} shown; "
            f"{len(targets) - len(shown)} more]"
        )
    return (
        "pull-request operation rejected: requested "
        f"repository={json.dumps(repository)}, pull_request={pull_request} is outside "
        f"this turn's scope; in-scope targets: {rendered}"
    )


def resolve_review_state_for_context(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    """Context-level variant used by authorization before tool invocation."""
    from mimir.access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    service = get_trusted_service_from_auth_context(context)
    if heartbeat_git_authority_enabled(service) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        return _resolve_heartbeat_git_state(context, repository, pull_request)
    cache = getattr(context, "server_discovered_pr_states", None)
    state = cache.resolve(repository, pull_request) if (
        isinstance(cache, ServerDiscoveredPRStates)
        and isinstance(repository, str)
        and isinstance(pull_request, int)
    ) else None
    registry = getattr(context, "repo_pr_scope_registry", None)
    if state is None:
        state = registry.resolve(repository, pull_request) if isinstance(
            registry, RepoPRScopeRegistry,
        ) else None
    if state is not None:
        return state
    if (
        not isinstance(repository, str)
        or not isinstance(pull_request, int)
        or isinstance(pull_request, bool)
        or pull_request < 1
    ):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: repository must be text and pull_request "
            "must be a positive integer; for example, repository='owner/repo', pull_request=17"
        )
    from mimir.access_control import (
        can_resolve_forge_review_scope,
        create_server_discovered_review_scope,
        is_configured_github_repo,
        resolve_server_discovered_review_scope,
    )

    if not is_configured_github_repo(repository):
        raise ToolPolicyRefusal(
            "pull-request operation rejected: repository is not configured in GITHUB_REPOS"
        )
    refusal = cache.refusal(repository, pull_request) if isinstance(
        cache, ServerDiscoveredPRStates,
    ) else None
    if refusal is not None:
        raise ToolPolicyRefusal(refusal)
    store = getattr(context, "server_discovered_pr_scope_store", None)
    stored_scope = store.resolve(repository, pull_request) if (
        isinstance(store, ServerDiscoveredPRScopeStore)
        and context is not None
        and can_resolve_forge_review_scope(context, stage="stored")
    ) else None
    snapshot = None
    if stored_scope is not None:
        client = _client_for_repository(repository)
        try:
            snapshot = client.get_pull_request_snapshot(
                stored_scope.canonical_repo, stored_scope.pr_number,
            )
        except ForgeError as exc:
            raise ToolException(f"pull-request operation rejected: {exc}") from exc
        if (
            snapshot.state == "open"
            and isinstance(snapshot.repo, str)
            and snapshot.repo.lower() == stored_scope.canonical_repo
            and snapshot.number == stored_scope.pr_number
            and snapshot.head_sha.lower() == stored_scope.observed_head_sha
        ):
            reused = RepoReviewState(stored_scope)
            return cache.remember(reused) if isinstance(
                cache, ServerDiscoveredPRStates,
            ) else reused
        store.discard(
            repository, pull_request, expected_scope=stored_scope,
        )
        if isinstance(cache, ServerDiscoveredPRStates):
            cache.remember_escalation_scope(stored_scope)
        cause = None
        if snapshot.state != "open":
            cause = "the pull request is closed"
        elif (
            not isinstance(snapshot.repo, str)
            or snapshot.repo.lower() != stored_scope.canonical_repo
            or snapshot.number != stored_scope.pr_number
        ):
            cause = "the provider repository or pull request number does not match"
        if cause is not None:
            stale_refusal = (
                "pull-request operation rejected: stored server-discovered scope for "
                f"repository={json.dumps(repository)}, pull_request={pull_request} is stale; "
                f"{cause}"
            )
            if isinstance(cache, ServerDiscoveredPRStates):
                cache.remember_refusal(repository, pull_request, stale_refusal)
            raise ToolException(stale_refusal)
    if (
        not can_resolve_forge_review_scope(context, stage="fetch")
    ):
        scope_refusal = _scope_miss_refusal(
            cache, registry, repository, pull_request,
        )
        if scope_refusal is not None:
            if stored_scope is not None:
                scope_refusal += "; head advanced; discovery not permitted for this turn"
            if isinstance(cache, ServerDiscoveredPRStates):
                cache.remember_refusal(
                    repository, pull_request, scope_refusal,
                )
            raise ToolPolicyRefusal(scope_refusal)
        scope_refusal = (
            "pull-request operation rejected: requested "
            f"repository={json.dumps(repository)}, pull_request={pull_request}; "
            "live scope discovery requires an authenticated operator user turn"
        )
        if stored_scope is not None:
            scope_refusal += "; head advanced; discovery not permitted for this turn"
        if isinstance(cache, ServerDiscoveredPRStates):
            cache.remember_refusal(repository, pull_request, scope_refusal)
        raise ToolPolicyRefusal(scope_refusal)
    cached = cache.resolve(repository, pull_request) if cache is not None else None
    if cached is not None:
        return cached
    if snapshot is None:
        client = _client_for_repository(repository)
        try:
            snapshot = client.get_pull_request_snapshot(repository.lower(), pull_request)
        except ForgeError as exc:
            raise ToolException(f"pull-request operation rejected: {exc}") from exc
    self_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if (
        not can_resolve_forge_review_scope(
            context,
            stage="accept",
            pr_author=snapshot.author,
            self_login=self_login,
        )
    ):
        scope_refusal = (
            "pull-request operation rejected: requested "
            f"repository={json.dumps(repository)}, pull_request={pull_request}; "
            "live scope discovery requires an authenticated operator user turn or a "
            "trusted poller with pr_metadata and a configured MIMIR_GITHUB_SELF_LOGIN; "
            "reviewing another author's pull request also requires an explicit "
            "pr_review_others capability grant"
        )
        if stored_scope is not None:
            scope_refusal += "; head advanced; discovery not permitted for this turn"
        if isinstance(cache, ServerDiscoveredPRStates):
            cache.remember_refusal(repository, pull_request, scope_refusal)
        raise ToolPolicyRefusal(scope_refusal)
    review_state = None
    if snapshot.state == "open" and snapshot.author == self_login:
        discovery_scope = create_server_discovered_review_scope(repository, snapshot)
        if discovery_scope is not None:
            try:
                reviews = client.list_reviews(discovery_scope)
            except ForgeError as exc:
                raise ToolException(f"pull-request operation rejected: {exc}") from exc
            latest: dict[str, str] = {}
            for review in reviews:
                state = review.state.upper()
                if state in {"APPROVED", "CHANGES_REQUESTED"}:
                    latest[review.author] = state
            if "CHANGES_REQUESTED" in latest.values():
                review_state = "CHANGES_REQUESTED"
    verdict = (
        True if snapshot.author == self_login else
        _author_verdict(context, snapshot.repo, snapshot.author, client)
        if isinstance(snapshot.author, str) and snapshot.author else None
    )
    resolution = resolve_server_discovered_review_scope(
        snapshot.repo, snapshot, review_state=review_state,
        pr_author_is_trusted=verdict,
    )
    scope = resolution.scope
    if (
        scope is None
        or scope.canonical_repo != repository.lower()
        or scope.pr_number != pull_request
    ):
        raise ToolException(resolution.refusal_reason or (
            "pull-request operation rejected: live pull request is closed or invalid"
        ))
    state = RepoReviewState(scope)
    if snapshot.author != self_login:
        from mimir.access_control import get_trusted_service_from_auth_context
        from mimir.event_logger import log_event_sync

        service = get_trusted_service_from_auth_context(context)
        if service is not None and service.has_capability("pr_review_others"):
            log_event_sync(
                "forge_review_others_scope_resolved",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
                capability="pr_review_others",
                author=snapshot.author,
            )
    store = getattr(context, "server_discovered_pr_scope_store", None)
    if isinstance(store, ServerDiscoveredPRScopeStore):
        store.remember_server_discovery(scope)
    return cache.remember(state) if cache is not None else state


def _resolve_heartbeat_git_state(
    context: AuthContext,
    repository: str,
    pull_request: int,
) -> RepoReviewState:
    """Live own-PR authority; the operator's cross-turn store is not a grant."""
    from mimir.access_control import (
        can_resolve_forge_review_scope,
        create_server_discovered_heartbeat_scope,
        heartbeat_cached_scope_refusal,
        is_configured_github_repo,
    )

    if not is_configured_github_repo(repository):
        raise ToolPolicyRefusal("heartbeat_repository_denied: repository is not configured")
    if type(pull_request) is not int or pull_request < 1:
        raise ToolPolicyRefusal("heartbeat_pr_invalid: pull_request must be a positive integer")
    login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if not login:
        raise ToolPolicyRefusal("heartbeat_identity_missing: MIMIR_GITHUB_SELF_LOGIN is empty")
    if not can_resolve_forge_review_scope(context, stage="fetch"):
        raise ToolPolicyRefusal("heartbeat_authority_denied: trusted pr_metadata capability required")
    cache = context.server_discovered_pr_states
    registry = context.repo_pr_scope_registry
    existing = cache.resolve(repository, pull_request) if cache is not None else None
    if existing is None and registry is not None:
        existing = registry.resolve(repository, pull_request)
    if existing is not None and (
        existing.action_scope.pull_request_author != login
        or existing.action_scope.principal != login
    ):
        raise ToolPolicyRefusal("heartbeat_other_author: cached scope is not authored by the configured identity")
    try:
        snapshot = _client_for_repository(repository).get_pull_request_snapshot(
            repository.lower(), pull_request,
        )
    except ForgeError as exc:
        raise ToolException(f"heartbeat_pr_unverified: {exc}") from exc
    from mimir.models import NormalizedPullRequestSnapshot

    if (
        not isinstance(snapshot, NormalizedPullRequestSnapshot)
        or not isinstance(snapshot.repo, str)
        or snapshot.repo.lower() != repository.lower()
        or type(snapshot.number) is not int
        or snapshot.number != pull_request
        or snapshot.state != "open"
    ):
        raise ToolPolicyRefusal("heartbeat_pr_invalid: provider must report the requested open PR")
    if snapshot.author != login:
        raise ToolPolicyRefusal("heartbeat_other_author: PR is not authored by the configured identity")
    scope = create_server_discovered_heartbeat_scope(
        repository, snapshot, event_type="heartbeat_pr_maintenance",
    )
    if scope is None:
        raise ToolPolicyRefusal("heartbeat_pr_invalid: provider refs or configured repository binding are invalid")
    if existing is not None:
        # Never re-pin a live checkout after the provider advances. Git publication
        # also checks the remote ref, closing the race after this API observation.
        refusal = heartbeat_cached_scope_refusal(existing.action_scope, scope)
        if refusal is not None:
            raise ToolPolicyRefusal(refusal)
        return existing
    state = RepoReviewState(scope)
    return cache.remember(state) if cache is not None else state


def revalidate_review_head_for_context(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> None:
    """Refuse a typed review effect if the forge no longer reports its scoped head."""
    state = resolve_review_state_for_context(context, repository, pull_request)
    scope = state.action_scope
    client = _client_for_repository(scope.canonical_repo)
    try:
        snapshot = client.get_pull_request_snapshot(
            scope.canonical_repo, scope.pr_number,
        )
    except ForgeError as exc:
        _publish_scoped_error(SimpleNamespace(context=context), scope)
        raise ToolException(f"pull-request operation rejected: {exc}") from exc
    if (
        not isinstance(snapshot.repo, str)
        or snapshot.repo.lower() != scope.canonical_repo
        or snapshot.number != scope.pr_number
        or snapshot.state != "open"
        or snapshot.head_sha.lower() != scope.observed_head_sha
    ):
        raise ToolException(
            "pull-request operation rejected: pull request head advanced after scope issuance"
        )


def remediation_checkout_preflight(
    context: AuthContext | None,
    repository: str,
    pull_request: int,
) -> tuple[RepoReviewState | None, str | None]:
    """Refresh one stale own-remediation scope from a live provider snapshot."""
    from mimir.access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    if heartbeat_git_authority_enabled(get_trusted_service_from_auth_context(context)) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        return resolve_review_state_for_context(context, repository, pull_request), None
    registry = getattr(context, "repo_pr_scope_registry", None)
    original = registry.resolve(repository, pull_request) if isinstance(
        registry, RepoPRScopeRegistry,
    ) else None
    if original is None:
        return resolve_review_state_for_context(context, repository, pull_request), None
    scope = original.action_scope
    if scope.event_type not in {"pr_changes_requested_stale", "pr_ci_failure"} or scope.provenance.value != "poller_payload":
        return resolve_review_state_for_context(context, repository, pull_request), None

    client = _client_for_repository(repository)
    try:
        snapshot = client.get_pull_request_snapshot(repository.lower(), pull_request)
    except ForgeError as exc:
        _publish_scoped_error(SimpleNamespace(context=context), scope)
        raise ToolException(f"repository checkout rejected: {exc}") from exc
    if snapshot.state != "open":
        return None, "pull request is closed or merged"
    if scope.event_type == "pr_ci_failure":
        fresh = None
        if snapshot.head_sha.lower() != scope.observed_head_sha.lower():
            cache = getattr(context, "server_discovered_pr_states", None)
            if not isinstance(cache, ServerDiscoveredPRStates) or not cache.begin_remint(
                repository, pull_request,
            ):
                return resolve_review_state_for_context(context, repository, pull_request), None
            from mimir.access_control import create_server_discovered_heartbeat_scope

            fresh_scope = create_server_discovered_heartbeat_scope(
                repository, snapshot, event_type=scope.event_type,
            )
            if fresh_scope is None:
                return None, "pull request head was superseded"
            fresh = RepoReviewState(fresh_scope)
            scope = fresh_scope
        try:
            checks = client.list_checks(scope)
        except ForgeError as exc:
            _publish_scoped_error(SimpleNamespace(context=context), scope)
            raise ToolException(f"repository checkout rejected: {exc}") from exc
        if not any(
            check.status == "completed"
            and check.conclusion in {
                "failure", "timed_out", "startup_failure", "action_required",
            }
            for check in checks
        ):
            return None, "pull request checks are no longer failing"
        if fresh is not None:
            reminted = cache.remember_remint(original, fresh)
            _seed_verified_remint(context, original.action_scope, reminted.action_scope)
            return reminted, None
        return original, None
    if snapshot.head_sha.lower() == scope.observed_head_sha.lower():
        return original, None

    cache = getattr(context, "server_discovered_pr_states", None)
    if not isinstance(cache, ServerDiscoveredPRStates) or not cache.begin_remint(
        repository, pull_request,
    ):
        return resolve_review_state_for_context(context, repository, pull_request), None

    from mimir.access_control import create_server_discovered_heartbeat_scope

    fresh_scope = create_server_discovered_heartbeat_scope(
        repository, snapshot, event_type=scope.event_type,
    )
    if fresh_scope is None:
        # Failed re-authorization must retain today's exact stale-scope refusal.
        return original, None
    fresh = RepoReviewState(fresh_scope)

    from .repository import was_agent_push

    if not was_agent_push(
        repository, pull_request, scope.observed_head_sha, snapshot.head_sha,
    ):
        try:
            reviews = client.list_reviews(fresh_scope)
        except ForgeError as exc:
            raise ToolException(f"repository checkout rejected: {exc}") from exc
        latest: dict[str, str] = {}
        self_login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
        for review in reviews:
            state = review.state.upper()
            if review.author == self_login or state not in {"APPROVED", "CHANGES_REQUESTED"}:
                continue
            latest[review.author] = state
        if "CHANGES_REQUESTED" not in latest.values():
            return None, "pull request no longer has a blocking changes-requested review"
    reminted = cache.remember_remint(original, fresh)
    _seed_verified_remint(context, original.action_scope, reminted.action_scope)
    return reminted, None


def _seed_verified_remint(
    context: AuthContext | None, old: RepoPRActionScope, new: RepoPRActionScope,
) -> None:
    from .repository import was_verified_push

    if (
        context is not None
        and context.ifc_state is not None
        and old.canonical_repo.casefold() == new.canonical_repo.casefold()
        and old.pr_number == new.pr_number
        and was_verified_push(
            old.canonical_repo, old.pr_number, old.observed_head_sha, new.observed_head_sha,
        )
    ):
        context.ifc_state.record_own_push(
            old.canonical_repo, old.pr_number, old.observed_head_sha,
        )


def _client(scope: RepoPRActionScope) -> ForgeClient:
    return _client_for_repository(scope.canonical_repo)


def _client_for_repository(repository: str) -> ForgeClient:
    client = _clients.get(repository.lower()) or _default_client
    if client is None:
        client = GitHubForgeClient()
    return client


def _body(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolPolicyRefusal("body must be non-empty text; for example, body='Looks good'")
    if len(value.encode("utf-8")) > _BODY_MAX_BYTES:
        raise ToolPolicyRefusal(
            "body must be non-empty text within the 65536-byte UTF-8 limit; "
            "for example, body='Looks good'"
        )
    if "\x00" in value:
        raise ToolPolicyRefusal(
            "body must contain text without null bytes; for example, body='Looks good'"
        )
    return value


def _repository(value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise ToolPolicyRefusal(
            "repository must be text shaped 'owner/repo'; for example, repository='octocat/hello'"
        )
    return value


def _branch(value: str, name: str, *, qualified: bool = False) -> str:
    branch = value
    if qualified and isinstance(value, str) and ":" in value:
        owner, separator, branch = value.partition(":")
        if _REVIEWER.fullmatch(owner) is None or not separator:
            raise ToolPolicyRefusal(f"{name} must be a valid branch name")
    if (
        not isinstance(value, str) or not branch or branch.startswith("-") or ":" in branch
        or len(value.encode("utf-8")) > 255 or ".." in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ToolPolicyRefusal(f"{name} must be a valid branch name within 255 UTF-8 bytes")
    return value


def _merged_since(value: str) -> datetime:
    if not isinstance(value, str):
        raise ToolPolicyRefusal("merged_since must be an ISO-8601 date or datetime")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.combine(date.fromisoformat(value), time.min)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if "T" not in value and " " not in value:
                raise ValueError("not a datetime")
    except ValueError as exc:
        raise ToolPolicyRefusal("merged_since must be an ISO-8601 date or datetime") from exc
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _pr_search(value: str) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or len(value.encode("utf-8")) > 256
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ToolPolicyRefusal("search must be non-empty text within 256 UTF-8 bytes without control characters")
    if any(forbidden in value.casefold() for forbidden in ("repo:", "org:", "user:", "is:issue")):
        raise ToolPolicyRefusal("search cannot contain repo:, org:, user:, or is:issue")
    # Appending repo: is not confinement if boolean syntax or an open quote
    # can change how GitHub's advanced search parser binds those qualifiers.
    if any(char in value for char in '\"()') or any(
        token.casefold() in {"or", "and", "not"} for token in value.split()
    ):
        raise ToolPolicyRefusal("search cannot contain quotes, parentheses, or boolean operators")
    fixed = {
        "in:title", "in:body", "in:comments", "is:open", "is:closed",
        "is:merged", "is:unmerged", "is:draft",
    }
    for token in value.split():
        if ":" not in token or token in fixed:
            continue
        qualifier, _, argument = token.partition(":")
        if qualifier in {"head", "base"}:
            _branch(argument, qualifier)
        elif qualifier == "author" and _REVIEWER.fullmatch(argument) is not None:
            continue
        elif qualifier == "label" and 0 < len(argument) <= 50 and argument.isprintable() and ":" not in argument:
            continue
        else:
            raise ToolPolicyRefusal("search contains an unsupported qualifier")
    return value


def _issue_number(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ToolPolicyRefusal("issue must be a positive integer; for example, issue=220")
    return value


def resolve_issue_comment_target(repository: str, issue: int) -> IssueTarget:
    """Fetch and validate the exact configured issue used as the IFC sink."""
    repo = _repository(repository)
    number = _issue_number(issue)
    from mimir.access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal(
            "issue comment rejected: repository is not configured in GITHUB_REPOS"
        )
    return _call(lambda: _client_for_repository(repo).get_open_issue_target(repo, number))


def _path(value: str) -> str:
    path = Path(value)
    if (
        not value
        or len(value.encode("utf-8")) > _PATH_MAX_BYTES
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise ToolPolicyRefusal(
            "path must be a relative repository path without '..' or control characters, "
            "at most 4096 UTF-8 bytes; for example, path='src/app.py'"
        )
    return value


def _publish_scoped_error(runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope | None) -> None:
    """Attest server error text only after an exact own-repository PR is known."""
    if scope is None:
        return
    from mimir.access_control import _github_repo_from_remote

    context = getattr(runtime, "context", None)
    author = scope.pull_request_author
    if (
        context is None or context.ifc_state is None or not author
        or scope.head_repo != scope.canonical_repo
        or scope.head_remote != "origin"
        or (_github_repo_from_remote(scope.canonical_origin) or "").casefold() != scope.canonical_repo.casefold()
        or scope.checkout_ref not in (None, f"refs/pull/{scope.pr_number}/head")
    ):
        return
    try:
        if _author_verdict(context, scope, author, _client(scope)) is True:
            _publish_author_attestation(runtime, scope, (author,), "forge_error")
    except ForgeError:
        return


def _call(operation: Any, *, runtime: ToolRuntime[AuthContext] | None = None,
          scope: RepoPRActionScope | None = None) -> Any:
    try:
        return operation()
    except ForgeError as exc:
        if isinstance(exc, ForgeReadUnavailable):
            # A typed, fixed diagnostic contains no third-party content. Do
            # not extend this exemption to arbitrary adapter error strings.
            raise ToolPolicyRefusal(str(exc)) from exc
        if isinstance(exc, GitHubIdentityVerificationError):
            _latch_github_identity_degraded(exc)
            raise ToolException(
                f"coding capability disabled: GitHub identity verification failed: {exc}"
            ) from exc
        # The adapter may have contacted the forge before failing, so this is a
        # fault rather than a proven pre-execution policy refusal.
        _publish_scoped_error(runtime, scope)
        raise ToolException(str(exc)) from exc


def _author_verdict(context: AuthContext, repo: str | RepoPRActionScope, author: str, client: Any) -> bool | None:
    from mimir.config import trusted_github_bot_logins

    repository = repo.canonical_repo if isinstance(repo, RepoPRActionScope) else repo
    if not isinstance(author, str) or not author or context.ifc_state is None:
        return None
    attest = getattr(client, "author_is_trusted", None)
    if _BOT_LOGIN.fullmatch(author):
        # A bot is trusted only by exact operator designation; never ask the
        # collaborator API to upgrade an unlisted app identity.
        return context.ifc_state.repository_author_trust.resolve(
            repository, author,
            lambda: author.casefold() in trusted_github_bot_logins(),
        )
    if not callable(attest):
        return None
    return context.ifc_state.repository_author_trust.resolve(
        repository, author,
        lambda: attest(repository, author),
    )


def _withheld_event(tool: str, kind: str, reason: str, count: int = 1) -> None:
    from mimir.event_logger import log_event_sync

    try:
        log_event_sync("github_content_withheld", tool=tool, kind=kind, count=count, reason=reason)
    except RuntimeError:
        pass


def _withhold_thread(runtime: ToolRuntime[AuthContext], scope: RepoPRActionScope,
                     items: Any, tool: str) -> list[dict[str, Any]]:
    client = _client(scope)
    context = runtime.context
    projected = [dict(asdict(item), kind=(
        "review" if tool == "pr_reviews" else
        "review_comment" if item.path is not None else "comment"
    ), repository=scope.canonical_repo) for item in items]
    from . import github_trust

    kept, withheld = github_trust.partition(
        projected, lambda item: item["author"],
        lambda author: _author_verdict(context, scope, author, client),
    )
    _publish_author_attestation(runtime, scope, tuple(item["author"] for item in kept), tool)
    for item in withheld:
        _withheld_event(tool, item.kind, item.reason)
    result = [{key: value for key, value in item.items() if key not in {"kind", "repository"}}
              for item in kept]
    if len(withheld) > 20:
        result.append(github_trust.summarise(withheld))
    else:
        result.extend(github_trust.placeholder(item) for item in withheld)
    return result


def _publish_trusted_projection(runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope) -> None:
    """Independently attest a validated server-owned projection at its scope.

    Never depend on another parallel read warming the author cache. These
    bounded projections belong to the authorized scope, not a later live head;
    _pr_content_authors still rejects an unrelated live-head change.
    """
    _attest_scoped_read(runtime, scope, _client(scope), "forge_projection", scoped_head=True)


def _publish_write_projection(runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope) -> None:
    """Best-effort attestation must not turn a completed write into a retry.

    On adapter/read failure publish no trusted provenance: result classification
    retains its default untrusted label. Never reuse cached trust as a fallback.
    """
    try:
        _publish_trusted_projection(runtime, scope)
    except (ForgeError, ToolException):
        # Do not expose arbitrary adapter error text after the write succeeded.
        log.warning("forge write completed; result attestation unavailable")


def _check_url_matches_scope(url: str, scope: RepoPRActionScope) -> bool:
    from urllib.parse import unquote, urlsplit

    if not url.startswith("https://github.com/"):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    parts = parsed.path.split("/")
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or len(parts) < 4 or parts[0] != ""
            or "/".join(parts[1:3]).casefold() != scope.canonical_repo.casefold()):
        return False
    for part in parts[1:]:
        # A browser can decode path components before resolving dot segments.
        # Never attest a URL that could escape the checked repository prefix.
        while True:
            decoded = unquote(part)
            if decoded in {".", ".."} or (decoded != part and ("/" in decoded or "\\" in decoded)):
                return False
            if decoded == part:
                break
            part = decoded
    return True


def _safe_check_projection(check: Any, scope: RepoPRActionScope, index: int) -> dict[str, Any]:
    from mimir.event_logger import log_event_sync

    result = asdict(check)

    def withhold(field: str, replacement: Any) -> None:
        result[field] = replacement
        try:
            log_event_sync("pr_checks_field_withheld", field=field, check_index=index)
        except RuntimeError:
            pass

    if not isinstance(check.name, str) or _CHECK_NAME.fullmatch(check.name) is None:
        withhold("name", "<withheld-check-name>")
    if not isinstance(check.status, str) or check.status not in _CHECK_STATUSES:
        withhold("status", "unknown")
    if check.conclusion is not None and (
        not isinstance(check.conclusion, str) or check.conclusion not in _CHECK_CONCLUSIONS
    ):
        withhold("conclusion", "unknown")
    if check.details_url is not None and (
        not isinstance(check.details_url, str) or not _check_url_matches_scope(check.details_url, scope)
    ):
        withhold("details_url", None)
    return result


def _publish_author_attestation(
    runtime: ToolRuntime[AuthContext] | None,
    scope: RepoPRActionScope,
    authors: tuple[str, ...],
    tool: str,
    head_sha: str | None = None,
) -> None:
    """Publish provenance only for authors obtained from native forge projections.

    Missing actors/adapters and unavailable attestation fail closed for this
    result. Only definitive verdicts enter the turn-local cache; no PR-level
    verdict is persisted. Contained ``repo_test`` output is a function of the
    attested checked-in checkout and inherits its lease attestation. Scoped CI
    job logs inherit the PR author verdict; checks and forge mutation output
    have separate provenance rules.
    """
    from mimir.access_control import publish_protected_result
    from mimir.models import SourceLabel

    context = getattr(runtime, "context", None)
    if context is None:
        return
    client = _client(scope)
    if not callable(getattr(client, "author_is_trusted", None)) and not any(
        isinstance(author, str) and _BOT_LOGIN.fullmatch(author) for author in authors
    ):
        return
    trusted = True
    failed_authors = []
    unavailable_authors = []
    for author in dict.fromkeys(authors):
        if not isinstance(author, str) or not author or author == "<head-mismatch>":
            trusted = False
            failed_authors.append(
                "<head-mismatch>" if author == "<head-mismatch>" else "<missing-author>"
            )
            continue
        verdict = _author_verdict(context, scope, author, client)
        trusted = trusted and verdict is True
        # Only identifiers, never arbitrary adapter strings, enter diagnostics.
        diagnostic_author = author if _DIAGNOSTIC_LOGIN.fullmatch(author) else "<invalid-author>"
        if verdict is not True:
            failed_authors.append(diagnostic_author)
        if verdict is None:
            unavailable_authors.append(diagnostic_author)
            context.ifc_state.record_author_attestation_unavailable()
    principal = context.canonical_principal
    if context.is_service and principal:
        principal = f"service:{principal}"
    resource_id = f"{scope.canonical_repo}#pull/{scope.pr_number}@{head_sha or scope.observed_head_sha}"
    publish_protected_result((SourceLabel(
        principal=principal, domain="repository",
        resource_id=resource_id,
        bridge_instance="forge", sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool", integrity="trusted" if trusted else "untrusted",
        integrity_effect="active_ingest",
    ),))
    if not trusted:
        try:
            from mimir._context import get_current_turn
            from mimir.event_logger import log_event_sync

            turn = get_current_turn()
            log_event_sync(
                "forge_author_attestation_downgraded",
                session_id=context.channel_id,
                turn_id=turn.turn_id if turn is not None else None,
                repository=scope.canonical_repo,
                resource_id=resource_id,
                tool=tool,
                integrity="untrusted", integrity_effect="active_ingest",
                failed_authors=failed_authors,
                unavailable_authors=unavailable_authors,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must not alter the read
            # Observability must not turn a successful read into a tool failure.
            log.warning(
                "forge_author_attestation_downgraded: repository=%s resource_id=%s "
                "tool=%s failed_authors=%s unavailable_authors=%s "
                "(event logging failed: %s)",
                scope.canonical_repo,
                resource_id,
                tool, failed_authors, unavailable_authors, exc,
            )


def _pr_attestation(
    metadata: PullRequestProjection,
    scope: RepoPRActionScope,
    runtime: ToolRuntime[AuthContext] | None,
) -> tuple[tuple[str, ...], str]:
    """Bind PR-owned text to the observed head or an exact verified own push."""
    if metadata.number != scope.pr_number:
        return ("",), scope.observed_head_sha
    if metadata.head_sha != scope.observed_head_sha:
        # Runtime-less callers (notably checkout) retain observed-head-only
        # trust; accepting a new head requires per-turn lineage recording.
        if runtime is None:
            return ("<head-mismatch>",), scope.observed_head_sha
        from .repository import was_verified_push

        if not was_verified_push(
            scope.canonical_repo, scope.pr_number, scope.observed_head_sha, metadata.head_sha,
        ):
            return ("<head-mismatch>",), scope.observed_head_sha
        context = getattr(runtime, "context", None)
        if context is not None and context.ifc_state is not None:
            context.ifc_state.record_own_push(
                scope.canonical_repo, scope.pr_number, scope.observed_head_sha,
            )
    return (metadata.author,), metadata.head_sha


def _pr_content_authors(
    client: ForgeClient, scope: RepoPRActionScope,
    runtime: ToolRuntime[AuthContext] | None = None,
) -> tuple[tuple[str, ...], str]:
    """Bind PR-owned diff/file text to API authorship at its verified head."""
    return _pr_attestation(client.get_pull_request(scope), scope, runtime)


def _attest_scoped_read(
    runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope,
    client: ForgeClient, tool: str, *, scoped_head: bool = False,
) -> tuple[tuple[str, ...], str] | None:
    if not callable(getattr(client, "author_is_trusted", None)):
        return None
    if (scope.head_repo == scope.canonical_repo
            or scope.head_remote == "origin" and scope.checkout_ref is None) and (
        scope.head_repo != scope.canonical_repo
        or scope.head_remote != "origin"
        or not _github_origin_matches(scope)
        or scope.checkout_ref not in (None, f"refs/pull/{scope.pr_number}/head")
    ):
        return None
    authors, head_sha = _call(
        lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope,
    )
    _publish_author_attestation(
        runtime, scope, authors, tool,
        head_sha=scope.observed_head_sha if scoped_head else head_sha,
    )
    return authors, head_sha


def _verify_scoped_read_head(
    runtime: ToolRuntime[AuthContext] | None, scope: RepoPRActionScope,
    client: ForgeClient, tool: str, before: tuple[tuple[str, ...], str] | None,
) -> None:
    if before is None:
        return
    try:
        after = _call(
            lambda: _pr_content_authors(client, scope, runtime), runtime=runtime, scope=scope,
        )
    except (ToolException, ToolPolicyRefusal):
        after = None
    if after != before:
        _publish_author_attestation(runtime, scope, ("<head-mismatch>",), tool)


@tool
def pr_list(
    repository: str,
    state: str = "open",
    author: str | None = None,
    base: str | None = None,
    head: str | None = None,
    merged_since: str | None = None,
    limit: StrictInt = 30,
    search: str | None = None,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded, untrusted PRs in a configured repository. Search results omit
    head_ref, base_ref and head_sha (empty strings); use pr_metadata for those fields.
    """
    repo = _repository(repository)
    from mimir.access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("pull-request list rejected: repository is not configured in GITHUB_REPOS")
    if state not in {"open", "closed", "merged", "all"}:
        raise ToolPolicyRefusal("state must be open, closed, merged, or all")
    if author is not None and (not isinstance(author, str) or _REVIEWER.fullmatch(author) is None):
        raise ToolPolicyRefusal("author must be a valid GitHub login")
    if base is not None:
        _branch(base, "base")
    if head is not None:
        _branch(head, "head", qualified=True)
    since = _merged_since(merged_since) if merged_since is not None else None
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolPolicyRefusal("limit must be an integer from 1 through 100")
    query = _pr_search(search) if search is not None else None
    client = _client_for_repository(repo)
    if query is None:
        items = _call(lambda: client.list_pull_requests(
            repo, state=state, base=base, head=head, limit=limit,
            author=author, merged_since=since,
        ))
    else:
        items = _call(lambda: client.search_pull_requests(
            repo, query=query, state=state, base=base, head=head, limit=limit,
            author=author, merged_since=since,
        ))
    context = getattr(runtime, "context", None)
    if context is None or context.ifc_state is None:
        # Direct invocations have no turn cache; still attest before projecting.
        def verdict_for(author: str) -> bool | None:
            from mimir.config import trusted_github_bot_logins

            if not isinstance(author, str) or not author:
                return None
            if _BOT_LOGIN.fullmatch(author):
                return author.casefold() in trusted_github_bot_logins()
            attest = getattr(client, "author_is_trusted", None)
            return attest(repo, author) if callable(attest) else None
    else:
        verdict_for = lambda author: _author_verdict(context, repo, author, client)
    result = []
    trusted = True
    for item in items:
        verdict = verdict_for(item.author)
        if verdict is True:
            result.append(asdict(item))
            continue
        trusted = False
        reason = ("attestation_unavailable" if verdict is None else
                  "bot_not_allowlisted" if item.author.endswith("[bot]") else "non_collaborator")
        result.append({"number": item.number, **placeholder(WithheldItem(
            "pull_request", sanitize_login(item.author), None,
            sanitize_url(item.url, repo), reason,
        ))})
        _withheld_event("pr_list", "pull_request", reason)
    if trusted and context is not None:
        from mimir.access_control import publish_protected_result
        from mimir.models import SourceLabel

        principal = context.canonical_principal
        if context.is_service and principal:
            principal = f"service:{principal}"
        publish_protected_result((SourceLabel(
            principal=principal, domain="repository", resource_id=f"{repo.lower()}#pulls",
            bridge_instance="forge", sensitivity="internal",
            authorized_principals=frozenset({principal}) if principal else frozenset(),
            source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
        ),))
    return result


@tool
def pr_metadata(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read metadata for an exact pull request authorized by this turn."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "pr_metadata")
    try:
        metadata = _call(lambda: client.get_pull_request(scope), runtime=runtime, scope=scope)
    except (ToolException, ToolPolicyRefusal):
        _verify_scoped_read_head(runtime, scope, client, "pr_metadata", before)
        raise
    authors, head_sha = _pr_attestation(metadata, scope, runtime)
    if before is None or (authors, head_sha) != before:
        _publish_author_attestation(runtime, scope, authors, "pr_metadata", head_sha=head_sha)
    return asdict(metadata)


def _issue_bound_to_pr(home: Path, repository: str, pull_request: int) -> int | None:
    """Resolve an issue only from a unique server-recorded PR evidence URL."""
    evidence = home / "state" / "worklink" / "evidence"
    if not evidence.is_dir():
        return None
    target = f"https://github.com/{repository.lower()}/pull/{pull_request}"
    matched: set[int] = set()
    for path in evidence.glob("*.json"):
        issue = re.fullmatch(r"([1-9][0-9]*)-[^/]+\.json", path.name)
        if issue is None:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or not isinstance(record.get("pr_url"), str):
            continue
        url = record["pr_url"].strip().rstrip("/")
        match = re.match(
            r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
            url, flags=re.IGNORECASE,
        )
        if match is not None:
            owner, repo, number = match.groups()
            if f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{number}" == target:
                matched.add(int(issue.group(1)))
                if len(matched) > 1:
                    return None
    return next(iter(matched)) if matched else None


@tool
def pr_spec(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read the armed Chainlink spec of record bound by Worklink evidence to this PR."""
    scope = _scope(runtime, repository, pull_request)
    from mimir.access_control import publish_protected_result
    from mimir.models import SourceLabel

    context = getattr(runtime, "context", None)
    if context is not None:
        principal = context.canonical_principal
        if context.is_service and principal:
            principal = f"service:{principal}"
        publish_protected_result((SourceLabel(
            principal=principal, domain="repository",
            resource_id=f"{scope.canonical_repo}#pull/{scope.pr_number}@{scope.observed_head_sha}",
            bridge_instance="forge", sensitivity="internal",
            authorized_principals=frozenset({principal}) if principal else frozenset(),
            source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
        ),))

    home = os.environ.get("MIMIR_HOME", "").strip()
    issue_id = _issue_bound_to_pr(Path(home), scope.canonical_repo, scope.pr_number) if home else None
    if issue_id is None:
        return {"error": "no_bound_spec"}
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [os.environ.get("CHAINLINK_BIN") or "chainlink", "issue", "show", str(issue_id), "--json"],
                cwd=home, stdout=output, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            if result.returncode != 0 or output.tell() > 131_072:
                return {"error": "spec_read_failed"}
            output.seek(0)
            payload = json.load(output)
        if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] != issue_id:
            return {"error": "spec_read_failed"}
        labels = payload.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            return {"error": "spec_read_failed"}
        if not any(label.startswith("worklink:") for label in labels):
            return {"error": "spec_not_armed"}
        if not all(isinstance(payload.get(key), str) for key in ("title", "status", "description")):
            return {"error": "spec_read_failed"}
        spec = {key: payload[key] for key in ("id", "title", "status", "labels")}
        spec["description"] = strip_external_record(payload["description"])
        return spec
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return {"error": "spec_read_failed"}


@tool
def pr_files(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded file projections for the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "pr_files")
    try:
        items = _call(lambda: client.list_files(scope), runtime=runtime, scope=scope)
    finally:
        _verify_scoped_read_head(runtime, scope, client, "pr_files", before)
    return [asdict(item) for item in items]


@tool
def pr_diff(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read the bounded unified diff for the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "pr_diff")
    try:
        diff = _call(lambda: client.get_diff(scope), runtime=runtime, scope=scope)
    finally:
        _verify_scoped_read_head(runtime, scope, client, "pr_diff", before)
    return diff


@tool
def pr_file_content(
    repository: str,
    pull_request: int,
    path: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read one regular file at the pull request's verified head."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "pr_file_content")
    try:
        content = _call(lambda: client.get_file_content(scope, path), runtime=runtime, scope=scope)
    finally:
        _verify_scoped_read_head(runtime, scope, client, "pr_file_content", before)
    return content


@tool
def pr_checks(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded check projections for the bound pull request head."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "forge_projection", scoped_head=True)
    try:
        checks = _call(lambda: client.list_checks(scope), runtime=runtime, scope=scope)
    finally:
        _verify_scoped_read_head(runtime, scope, client, "forge_projection", before)
    return [_safe_check_projection(item, scope, index) for index, item in enumerate(checks)]


@tool
def pr_job_log(
    repository: str,
    pull_request: StrictInt,
    job_id: StrictInt,
    run_id: StrictInt | None = None,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> str:
    """Read a redacted bounded CI job excerpt; trusted only for an attested PR."""
    _repository(repository)
    for name, value in (("pull_request", pull_request), ("job_id", job_id), ("run_id", run_id)):
        if name == "run_id" and value is None:
            continue
        if type(value) is not int or value < 1:
            raise ToolPolicyRefusal(f"{name} must be a positive integer")
    context = getattr(runtime, "context", None)
    state = None
    for inventory in (
        getattr(context, "server_discovered_pr_states", None),
        getattr(context, "repo_pr_scope_registry", None),
    ):
        if isinstance(inventory, (ServerDiscoveredPRStates, RepoPRScopeRegistry)):
            state = inventory.resolve(repository, pull_request)
            if state is not None:
                break
    if state is None:
        raise ToolPolicyRefusal("job log rejected: pull request is outside this turn's scope")
    from mimir.access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    if heartbeat_git_authority_enabled(get_trusted_service_from_auth_context(context)) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        state = resolve_review_state_for_context(context, repository, pull_request)
    scope = state.action_scope
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "pr_job_log", scoped_head=True)
    try:
        excerpt = _call(lambda: client.get_job_log(scope, job_id, run_id), runtime=runtime, scope=scope)
    finally:
        _verify_scoped_read_head(runtime, scope, client, "pr_job_log", before)
    return excerpt


def _attest_ci_runs(runtime: ToolRuntime[AuthContext] | None, repository: str,
                    runs: list[dict[str, Any]], resource_id: str) -> None:
    """Publish only when every returned run belongs to a protected head."""
    from mimir.access_control import publish_protected_result
    from mimir.models import SourceLabel

    context = getattr(runtime, "context", None)
    if context is None or not runs or not all(
        _trusted_ci_head(context, repository, run) for run in runs
    ):
        return
    principal = context.canonical_principal
    if context.is_service and principal:
        principal = f"service:{principal}"
    publish_protected_result((SourceLabel(
        principal=principal, domain="repository", resource_id=resource_id,
        bridge_instance="forge", sensitivity="internal",
        authorized_principals=frozenset({principal}) if principal else frozenset(),
        source_kind="protected_tool", integrity="trusted", integrity_effect="active_ingest",
    ),))


def _trusted_ci_head(context: AuthContext, repository: str, run: dict[str, Any]) -> bool:
    from mimir.plugins.core import selected_policy_registry

    if not isinstance(run, dict):
        return False
    branch, sha = run.get("head_branch"), run.get("head_sha")
    if (
        not isinstance(run.get("head_repository"), str)
        or run["head_repository"].lower() != repository.lower()
        or not isinstance(sha, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", sha) is None
    ):
        return False
    hooks = selected_policy_registry().policy_consumer_hooks
    if (hooks is not None and hooks.owner == "cultivate"
            and isinstance(branch, str) and f"refs/heads/{branch}" in hooks.protected_refs):
        return True
    for inventory in (context.server_discovered_pr_states, context.repo_pr_scope_registry):
        for state in getattr(inventory, "review_states", ()):
            scope = state.action_scope
            if (
                scope.canonical_repo == repository.lower()
                and scope.head_repo == repository.lower()
                and scope.head_remote == "origin"
                and _github_origin_matches(scope)
                and scope.head_ref == branch and scope.observed_head_sha == sha
                and context.ifc_state is not None
                and context.ifc_state.pr_checkout_author_trust.get(scope.scope_id) is True
            ):
                return True
    return False


def _github_origin_matches(scope: RepoPRActionScope) -> bool:
    from mimir.access_control import _github_repo_from_remote

    return (_github_repo_from_remote(scope.canonical_origin) or "").casefold() == scope.canonical_repo.casefold()


@tool
def ci_run_jobs(
    repository: str,
    run_id: StrictInt,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Read bounded jobs and failed steps for a configured, poller-named CI run."""
    repo = _repository(repository)
    from mimir.access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("CI run rejected: repository is not configured in GITHUB_REPOS")
    context = getattr(runtime, "context", None)
    if type(run_id) is not int or run_id < 1 or (repo.lower(), run_id) not in getattr(
        context, "ci_run_targets", frozenset(),
    ):
        raise ToolPolicyRefusal("CI run rejected: run is outside this turn's poller scope")
    client = _client_for_repository(repo)
    run = _call(lambda: client.get_run(repo, run_id))
    if isinstance(run, dict) and run.get("id") == run_id:
        _attest_ci_runs(runtime, repo, [run], f"{repo.lower()}#actions/run/{run_id}/jobs")
    return _call(lambda: client.list_run_jobs(repo, run_id))


@tool
def ci_run(
    repository: str,
    run_id: StrictInt,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Read bounded metadata for a configured, poller-named CI run."""
    repo = _repository(repository)
    from mimir.access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("CI run rejected: repository is not configured in GITHUB_REPOS")
    context = getattr(runtime, "context", None)
    if type(run_id) is not int or run_id < 1 or (repo.lower(), run_id) not in getattr(
        context, "ci_run_targets", frozenset(),
    ):
        raise ToolPolicyRefusal("CI run rejected: run is outside this turn's poller scope")
    run = _call(lambda: _client_for_repository(repo).get_run(repo, run_id))
    if isinstance(run, dict) and run.get("id") == run_id:
        _attest_ci_runs(runtime, repo, [run], f"{repo.lower()}#actions/run/{run_id}")
    return run


@tool
def ci_recent_runs(
    repository: str,
    branch: str,
    workflow_id: StrictInt | None = None,
    limit: StrictInt = 10,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Read bounded recent CI runs on the poller-named branch."""
    repo = _repository(repository)
    from mimir.access_control import is_configured_github_repo

    if not is_configured_github_repo(repo):
        raise ToolPolicyRefusal("CI runs rejected: repository is not configured in GITHUB_REPOS")
    context = getattr(runtime, "context", None)
    targets = getattr(context, "ci_branch_targets", frozenset())
    if (
        not isinstance(branch, str) or not branch
        or (workflow_id is not None and (type(workflow_id) is not int or workflow_id < 1))
        or not any(
            target_repo == repo.lower() and target_branch == branch
            and (workflow_id is None or workflow_id == target_workflow)
            for target_repo, target_branch, target_workflow in targets
        )
    ):
        raise ToolPolicyRefusal("CI runs rejected: branch or workflow is outside this turn's poller scope")
    if type(limit) is not int:
        raise ToolPolicyRefusal("CI runs rejected: limit must be an integer")
    bounded_limit = max(1, min(20, limit))
    runs = _call(lambda: _client_for_repository(repo).list_runs(repo, branch, workflow_id, bounded_limit))
    if isinstance(runs, list) and all(
        isinstance(run, dict) and run.get("head_branch") == branch
        and (workflow_id is None or run.get("workflow_id") == workflow_id)
        for run in runs
    ):
        _attest_ci_runs(runtime, repo, runs, f"{repo.lower()}#actions/runs?branch={branch}"
                        + (f"&workflow={workflow_id}" if workflow_id is not None else ""))
    return runs


@tool
def pr_reviews(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded submitted-review projections for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    items = _call(lambda: _client(scope).list_reviews(scope), runtime=runtime, scope=scope)
    return _withhold_thread(runtime, scope, items, "pr_reviews")


@tool
def pr_comments(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded conversation and inline comments for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    items = _call(lambda: _client(scope).list_comments(scope), runtime=runtime, scope=scope)
    return _withhold_thread(runtime, scope, items, "pr_comments")


@tool
def pr_review_requests(
    repository: str,
    pull_request: int,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """List bounded pending review requests for the bound pull request."""
    scope = _scope(runtime, repository, pull_request)
    client = _client(scope)
    before = _attest_scoped_read(runtime, scope, client, "forge_projection", scoped_head=True)
    try:
        items = _call(lambda: client.list_review_requests(scope), runtime=runtime, scope=scope)
    finally:
        _verify_scoped_read_head(runtime, scope, client, "forge_projection", before)
    if not all(
        item.kind in {"user", "team"} and isinstance(item.reviewer, str)
        and _REVIEWER.fullmatch(item.reviewer) is not None for item in items
    ):
        _publish_author_attestation(runtime, scope, ("",), "pr_review_requests")
    return [asdict(item) for item in items]


@tool
def pr_submit_review(
    repository: str,
    pull_request: int,
    verdict: ReviewVerdict,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Submit one approve, comment, or request-changes review on the bound PR."""
    scope = _scope(runtime, repository, pull_request)
    safe_body = _body(body)
    result = asdict(_call(lambda: _client(scope).submit_review(scope, verdict, safe_body)))
    _publish_write_projection(runtime, scope)
    return result


@tool
def pr_inline_review_comment(
    repository: str,
    pull_request: int,
    path: str,
    line: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one inline review comment to a right-side line on the bound PR head."""
    scope = _scope(runtime, repository, pull_request)
    if isinstance(line, bool) or not isinstance(line, int) or line < 1 or line > 10_000_000:
        raise ToolPolicyRefusal(
            "line must be an integer from 1 through 10000000; for example, line=42"
        )
    safe_path = _path(path)
    safe_body = _body(body)
    result = asdict(_call(lambda: _client(scope).add_inline_review_comment(
        scope, path=safe_path, line=line, body=safe_body,
    )))
    _publish_write_projection(runtime, scope)
    return result


@tool
def pr_comment(
    repository: str,
    pull_request: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one conversation comment to the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    safe_body = _body(body)
    result = asdict(_call(lambda: _client(scope).add_pull_request_comment(scope, safe_body)))
    _publish_write_projection(runtime, scope)
    return result


@tool
def pr_edit_body(
    repository: str,
    pull_request: int,
    body: str,
    allow_shrink: bool = False,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Overwrite the ENTIRE bound PR description. Read pr_metadata first and pass
    the full body including existing sections and Closes lines. A body under 50%
    of the current body's length is refused unless allow_shrink=true.
    """
    scope = _scope(runtime, repository, pull_request)
    safe_body = _body(body)
    client = _client(scope)
    current = _call(lambda: client.get_pull_request(scope))
    if not allow_shrink and len(safe_body.encode("utf-8")) * 2 < len(current.body.encode("utf-8")):
        raise ToolPolicyRefusal(
            "pr_edit_body refused: new body is under 50% of the current description; "
            "read pr_metadata and pass the full body, or set allow_shrink=true "
            "for a deliberate full rewrite"
        )
    _call(lambda: client.edit_pull_request_body(scope, safe_body))
    _publish_write_projection(runtime, scope)
    return {"status": "body_updated"}


@tool
def issue_comment(
    repository: str,
    issue: int,
    body: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add one comment to a server-resolved open issue in a configured repository."""
    safe_body = _body(body)
    target = resolve_issue_comment_target(repository, issue)
    return asdict(_call(lambda: _client_for_repository(target.canonical_repo).add_issue_comment(
        target.canonical_repo, target.issue_number, safe_body,
    )))


@tool
def pr_rerequest_review(
    repository: str,
    pull_request: int,
    reviewer: str,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Re-request one reviewer on the pull request bound to this turn."""
    scope = _scope(runtime, repository, pull_request)
    if _REVIEWER.fullmatch(reviewer) is None:
        raise ToolPolicyRefusal(
            "reviewer must be a 1-39 character GitHub login containing letters, digits, "
            "or hyphens; for example, reviewer='octocat'"
        )
    _call(lambda: _client(scope).rerequest_review(scope, reviewer))
    return {"status": "review_rerequested", "reviewer": reviewer}


def _escalation_state_path() -> Path:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        raise ToolPolicyRefusal("unsupported operation escalation requires MIMIR_HOME")
    return Path(home).resolve() / "state" / "unsupported_operations.json"


def _bounded_escalation_text(value: Any, *, fallback: str, max_bytes: int) -> str:
    """Normalize untrusted prose into bounded, single-line operator-visible text."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value[: max_bytes * 2]
    else:
        text = str(value)[: max_bytes * 2]
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    text = redact_text(" ".join(text.split())) or fallback
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return text or fallback


def _normalize_attempts(value: Any) -> list[str]:
    if value is None:
        candidates: list[Any] = []
    elif isinstance(value, (list, tuple, set, frozenset)):
        candidates = list(value)[:_ESCALATION_MAX_ATTEMPTS]
    else:
        candidates = [value]
    return [
        _bounded_escalation_text(
            candidate,
            fallback="unspecified attempt",
            max_bytes=_ESCALATION_ATTEMPT_MAX_BYTES,
        )
        for candidate in candidates
    ]


def _operation_key(description: str) -> str:
    words = re.findall(r"[a-z0-9]+", description.lower())
    prefix = "_".join(words[:6])[:64].strip("_") or "unspecified_operation"
    digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _emit_unsupported(
    scope: RepoPRActionScope,
    operation: str,
    description: str,
    attempted_operations: list[str],
) -> bool:
    key = f"{scope.canonical_repo}#{scope.pr_number}"
    fingerprint = f"{scope.observed_head_sha}:{operation}"
    state_path = _escalation_state_path()
    with _escalation_lock:
        try:
            stored = json.loads(state_path.read_text(encoding="utf-8"))
            known = stored if isinstance(stored, dict) else {}
        except FileNotFoundError:
            known = {}
        except (OSError, ValueError, TypeError) as exc:
            raise ToolException("unsupported operation escalation state is unreadable") from exc
        if known.get(key) == fingerprint:
            return False

        from mimir.event_logger import log_durable_event_sync

        try:
            log_durable_event_sync(
                "unsupported_operation",
                repository=scope.canonical_repo,
                pull_request=scope.pr_number,
                operation=operation,
                description=description,
                attempted_operations=attempted_operations,
                scope_id=scope.scope_id,
                operator_visible=True,
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({**known, key: fingerprint}, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(state_path)
            directory_fd = os.open(state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException("unsupported operation escalation could not be persisted") from exc
    return True


@tool
def unsupported_operation(
    repository: str,
    pull_request: int,
    description: Any = None,
    attempted_operations: Any = None,
    runtime: ToolRuntime[AuthContext] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Escalate a bound-PR need in prose, including operations already attempted."""
    context = getattr(runtime, "context", None)
    cache = getattr(context, "server_discovered_pr_states", None)
    state = cache.resolve_for_tool("unsupported_operation", repository, pull_request) if isinstance(
        cache, ServerDiscoveredPRStates,
    ) else None
    from mimir.access_control import (
        get_trusted_service_from_auth_context, heartbeat_git_authority_enabled,
    )

    if heartbeat_git_authority_enabled(get_trusted_service_from_auth_context(context)) or heartbeat_git_authority_enabled(
        getattr(context, "service_authority", None),
    ):
        state = resolve_review_state_for_context(context, repository, pull_request)
    scope = state.action_scope if state is not None else _scope(runtime, repository, pull_request)
    safe_description = _bounded_escalation_text(
        description,
        fallback="The caller did not provide a description of the unsupported operation.",
        max_bytes=_ESCALATION_DESCRIPTION_MAX_BYTES,
    )
    safe_attempts = _normalize_attempts(attempted_operations)
    operation = _operation_key(safe_description)
    emitted = _emit_unsupported(scope, operation, safe_description, safe_attempts)
    return {
        "status": "unsupported_operation",
        "escalated": emitted,
        "repository": scope.canonical_repo,
        "pull_request": scope.pr_number,
        "operation": operation,
        "description": safe_description,
        "attempted_operations": safe_attempts,
    }


def _bind_injected_runtime(forge_tool: StructuredTool) -> StructuredTool:
    """Bind runtime before LangChain caches its raw callable annotation."""
    if forge_tool.func is None:
        raise RuntimeError(f"forge tool {forge_tool.name!r} has no sync callable")
    forge_tool.func.__annotations__["runtime"] = ToolRuntime
    sync_function = forge_tool.func

    @wraps(sync_function)
    async def off_loop(*args: Any, **kwargs: Any) -> Any:
        # Explicitly offload both the forge transport and author attestation.
        # to_thread propagates the exact call's protected-provenance capture.
        return await asyncio.to_thread(sync_function, *args, **kwargs)

    forge_tool.coroutine = off_loop
    forge_tool.args_schema = create_schema_from_function(
        forge_tool.name,
        forge_tool.func,
        filter_args=(),
        include_injected=True,
    )
    forge_tool.__dict__.pop("_injected_args_keys", None)
    return forge_tool


FORGE_TOOLS = tuple(_bind_injected_runtime(forge_tool) for forge_tool in (
    pr_list,
    pr_metadata,
    pr_spec,
    pr_files,
    pr_diff,
    pr_file_content,
    pr_checks,
    pr_job_log,
    ci_run_jobs,
    ci_run,
    ci_recent_runs,
    pr_reviews,
    pr_comments,
    pr_review_requests,
    pr_submit_review,
    pr_inline_review_comment,
    pr_comment,
    pr_edit_body,
    issue_comment,
    pr_rerequest_review,
    unsupported_operation,
))
