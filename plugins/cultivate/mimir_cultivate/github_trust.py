"""GitHub author withholding and credential attestation for Cultivate."""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Literal, Mapping, TypeVar

import requests

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


def probe_github_token() -> tuple[bool, str]:
    """Check the process token without inheriting proxy or netrc credentials."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token.strip():
        return False, "GITHUB_TOKEN not set"
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code != 200:
                return False, f"GitHub HTTP {response.status_code}"
            data = response.json()
            login = data.get("login") if isinstance(data, dict) else None
            if not isinstance(login, str) or not login.strip():
                return False, "GitHub response missing login"
            return True, f"GitHub authenticated as {login}"
    except Exception:
        return False, "GitHub probe failed"


def sanitize_login(login: object) -> str:
    """Keep only a complete, mention-safe GitHub login."""
    return login if isinstance(login, str) and _LOGIN.fullmatch(login) else "<invalid-login>"


def sanitize_url(url: object, repo: object) -> str | None:
    """Keep only a canonical issue/PR URL under the exact expected repository."""
    if not isinstance(url, str) or not isinstance(repo, str) or not _REPO.fullmatch(repo):
        return None
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/[0-9]+(?:\#[A-Za-z0-9_-]{1,64})?",
        url, re.ASCII,
    )
    return url if match is not None and match[1].casefold() == repo.casefold() else None


def sanitize_timestamp(value: object) -> str | None:
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
    return {
        "kind": item.kind if item.kind in _KINDS else "search_result",
        "author": sanitize_login(item.author),
        "created_at": sanitize_timestamp(item.created_at),
        "html_url": item.html_url if isinstance(item.html_url, str) and re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[0-9]+(?:\#[A-Za-z0-9_-]{1,64})?",
            item.html_url, re.ASCII,
        ) else None,
        "reason": item.reason if item.reason in _REASONS else "attestation_unavailable",
        "withheld": True,
    }


def summarise(items: Iterable[WithheldItem]) -> dict[str, object]:
    counts = Counter(placeholder(item)["kind"] for item in items)
    return {"withheld": sum(counts.values()), "by_kind": dict(counts)}


T = TypeVar("T", bound=Mapping[str, object])


def partition(
    items: Iterable[T], author_of: Callable[[T], str],
    verdict_for: Callable[[str], bool | None],
) -> tuple[list[T], list[WithheldItem]]:
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
