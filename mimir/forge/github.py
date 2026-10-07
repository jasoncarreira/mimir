"""GitHub adapter for the closed :class:`mimir.forge.ForgeClient` protocol."""

from __future__ import annotations

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

from ..models import NormalizedPullRequestSnapshot, RepoPRActionScope
from .client import (
    CheckProjection,
    CommentProjection,
    FileProjection,
    ForgeError,
    ForgeResponseTooLarge,
    IssueTarget,
    PullRequestProjection,
    PullRequestSummary,
    ReviewProjection,
    ReviewRequestProjection,
    ReviewVerdict,
)

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
        max_bytes: int = _MAX_RESPONSE_BYTES,
        not_found: str = "pull request not found",
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
            raise _GitHubRequestError(
                f"forge transport failed: {type(exc).__name__}", retryable=True,
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
        for page in range(1, _MAX_PAGES + 1):
            payload = self._request("GET", f"{endpoint}{separator}per_page=50&page={page}")
            if collection_key is not None:
                payload = payload.get(collection_key) if isinstance(payload, Mapping) else None
            if not isinstance(payload, list):
                raise ForgeError("forge returned an invalid collection")
            page_items = [
                item for item in payload
                if isinstance(item, Mapping)
                and (not merged_only or item.get("merged_at") is not None)
                and (predicate is None or predicate(item))
            ]
            items.extend(page_items)
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if len(items) > _MAX_ITEMS:
                raise ForgeResponseTooLarge("forge collection exceeded item limit")
            if len(payload) < 50:
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
        from ..pollers import _github_author_is_trusted

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
            limit=limit, merged_only=state == "merged", predicate=matches,
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
            if len(matches) != 1:
                raise ForgeError("file content refused: path is not a regular file at scoped head")
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
            raise ForgeError("run is still in progress; retry after completion")
        if job.get("status") != "completed" or job.get("conclusion") not in {
            "failure", "timed_out", "startup_failure", "action_required", "cancelled",
        }:
            raise ForgeError("job is not a completed failing job")
        from ..ci_logs import LOG_EXCERPT_BYTES, capture_job_log

        text, error = capture_job_log(
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
