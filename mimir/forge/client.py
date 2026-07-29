"""Closed provider-neutral protocol for pull-request operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..models import RepoPRActionScope


class ForgeError(RuntimeError):
    """A normalized forge operation failure safe to expose to a tool caller."""


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
class ReviewRequestProjection:
    reviewer: str
    kind: str


class ForgeClient(Protocol):
    """Narrow adapter contract; every target comes from an immutable scope."""

    def get_pull_request(self, scope: RepoPRActionScope) -> PullRequestProjection: ...

    def list_files(self, scope: RepoPRActionScope) -> tuple[FileProjection, ...]: ...

    def get_diff(self, scope: RepoPRActionScope) -> str: ...

    def list_checks(self, scope: RepoPRActionScope) -> tuple[CheckProjection, ...]: ...

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

    def rerequest_review(self, scope: RepoPRActionScope, reviewer: str) -> None: ...
