"""Content-free projections for GitHub items whose authors are not attested.

Callers supply their existing per-turn author verdicts.  Candidate mappings have
``kind``, ``created_at``, ``html_url`` and ``repository`` (owner/name); the
repository is used only to validate the URL and is never included in output.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Literal, Mapping, TypeVar

Kind = Literal[
    "comment", "review", "review_comment", "issue", "pull_request",
    "pr_title", "pr_body", "search_result",
]
Reason = Literal["non_collaborator", "bot_not_allowlisted", "attestation_unavailable"]

_KINDS = frozenset(Kind.__args__)
_REASONS = frozenset(Reason.__args__)
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?", re.ASCII)
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)


def sanitize_login(login: object) -> str:
    """Keep only a complete, mention-safe GitHub login."""
    return login if isinstance(login, str) and _LOGIN.fullmatch(login) else "<invalid-login>"


def sanitize_url(url: object, repo: object) -> str | None:
    """Keep only a canonical issue/PR URL under the exact expected repository."""
    if not isinstance(url, str) or not isinstance(repo, str) or not _REPO.fullmatch(repo):
        return None
    pattern = rf"https://github\.com/{re.escape(repo)}/(?:issues|pull)/[0-9]+(?:\#[A-Za-z0-9_-]{{1,64}})?"
    return url if re.fullmatch(pattern, url, re.ASCII) else None


def sanitize_timestamp(value: object) -> str | None:
    """Keep only the fixed UTC timestamp form (no user-controlled suffixes)."""
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return value


@dataclass(frozen=True)
class WithheldItem:
    kind: Kind
    author: str
    created_at: str | None
    html_url: str | None
    reason: Reason


def placeholder(item: WithheldItem) -> dict[str, object]:
    """Project an item onto exactly the content-free, fixed-schema fields."""
    return {
        "kind": item.kind if item.kind in _KINDS else "search_result",
        "author": sanitize_login(item.author),
        "created_at": sanitize_timestamp(item.created_at),
        # partition validates the repository; direct callers must sanitize_url
        # against the expected repo before constructing a WithheldItem.
        "html_url": item.html_url if isinstance(item.html_url, str) and re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[0-9]+(?:\#[A-Za-z0-9_-]{1,64})?",
            item.html_url, re.ASCII,
        ) else None,
        "reason": item.reason if item.reason in _REASONS else "attestation_unavailable",
        "withheld": True,
    }


def summarise(items: Iterable[WithheldItem]) -> dict[str, object]:
    """Count withheld items without incorporating any author-controlled text."""
    counts = Counter(placeholder(item)["kind"] for item in items)
    return {"withheld": sum(counts.values()), "by_kind": dict(counts)}


T = TypeVar("T", bound=Mapping[str, object])


def partition(
    items: Iterable[T], author_of: Callable[[T], str],
    verdict_for: Callable[[str], bool | None],
) -> tuple[list[T], list[WithheldItem]]:
    """Keep only attested content; a missing attestation stays retryable.

    ``verdict_for`` must use the existing caller-owned trust cache. This
    function never caches an unavailable verdict or consults GitHub itself.
    """
    kept: list[T] = []
    withheld: list[WithheldItem] = []
    for item in items:
        author = author_of(item)
        verdict = verdict_for(author)
        if verdict is True:
            kept.append(item)
            continue
        reason: Reason = (
            "attestation_unavailable" if verdict is None else
            "bot_not_allowlisted" if isinstance(author, str) and author.endswith("[bot]") else
            "non_collaborator"
        )
        kind = item.get("kind")
        withheld.append(WithheldItem(
            kind=kind if isinstance(kind, str) and kind in _KINDS else "search_result",
            author=sanitize_login(author),
            created_at=sanitize_timestamp(item.get("created_at")),
            html_url=sanitize_url(item.get("html_url"), item.get("repository")),
            reason=reason,
        ))
    return kept, withheld
