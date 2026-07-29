"""GitHub adapter for the closed :class:`mimir.forge.ForgeClient` protocol."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

import requests

from ..models import RepoPRActionScope
from .client import (
    CheckProjection,
    CommentProjection,
    FileProjection,
    ForgeError,
    ForgeResponseTooLarge,
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


class GitHubForgeClient:
    """Construct and execute bounded GitHub REST requests inside the adapter."""

    def __init__(
        self,
        *,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._timeout = timeout

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        token = self._token if self._token is not None else os.environ.get("GITHUB_TOKEN", "")
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mimir-forge",
        }
        if token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
        return headers

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
            raise ForgeError(f"forge transport failed: {type(exc).__name__}") from exc
        raw = response.content
        if len(raw) > max_bytes:
            raise ForgeResponseTooLarge("forge response exceeded size limit")
        if response.status_code >= 400:
            reasons = {
                401: "authentication failed",
                403: "operation forbidden",
                404: "pull request not found",
                409: "operation conflicted",
                422: "operation rejected",
                429: "rate limited",
            }
            raise ForgeError(reasons.get(response.status_code, "forge request failed"))
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
            max_bytes=_MAX_DIFF_BYTES,
        )
        if not isinstance(data, str):
            raise ForgeError("forge returned an invalid diff")
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
        data = self._request(
            "POST", f"/repos/{repository}/issues/{number}/comments", body={"body": body},
        )
        if not isinstance(data, Mapping):
            raise ForgeError("forge returned invalid comment result")
        return self._comment(data)

    def rerequest_review(self, scope: RepoPRActionScope, reviewer: str) -> None:
        repository, number = self._target(scope)
        if _REVIEWER.fullmatch(reviewer) is None:
            raise ForgeError("invalid reviewer")
        self._request(
            "POST",
            f"/repos/{repository}/pulls/{number}/requested_reviewers",
            body={"reviewers": [reviewer]},
        )
