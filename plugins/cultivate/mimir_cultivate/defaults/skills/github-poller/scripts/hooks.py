"""Recovery hooks for the GitHub activity poller."""

from __future__ import annotations

import urllib.parse

from mimir.background_io import run_in_pool
from mimir.models import AgentEvent


def recovery_relevance_check(token: str):
    """Build a per-poll-cycle, per-PR-cached actionability predicate."""
    # Imported lazily to avoid coupling poller framework import order to this
    # optional skill. The shared helpers also serve GitHub ingress attestation.
    from mimir import pollers

    cache: dict[tuple[str, int], bool | None] = {}

    async def check(event: AgentEvent) -> bool | None:
        items = event.extra.get("items") if isinstance(event.extra, dict) else None
        if not isinstance(items, list) or not items:
            return None
        saw_pr = False
        saw_unknown = False
        for item in items:
            if not isinstance(item, dict):
                saw_unknown = True
                continue
            event_type = item.get("event_type")
            url = item.get("url")
            is_pr = (
                item.get("subject_type") == "pull_request"
                or event_type in pollers._GITHUB_PR_EVENT_TYPES
                or (
                    event_type == "issue_comment"
                    and isinstance(url, str)
                    and "/pull/" in urllib.parse.urlsplit(url).path
                )
            )
            if not is_pr:
                saw_unknown = True
                continue
            repo = item.get("repo")
            number = item.get("number")
            if isinstance(number, str) and number.isdigit():
                number = int(number)
            parts = repo.split("/") if isinstance(repo, str) else []
            if (
                len(parts) != 2
                or not all(parts)
                or not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
            ):
                saw_unknown = True
                continue
            saw_pr = True
            key = (repo, number)
            if key not in cache:
                escaped_repo = "/".join(
                    urllib.parse.quote(value, safe="") for value in parts
                )
                attestation = await run_in_pool(
                    pollers._ATTESTATION_POOL,
                    pollers._github_api_attestation,
                    f"repos/{escaped_repo}/pulls/{number}",
                    token,
                )
                if (
                    attestation is None
                    or attestation[0] != 200
                    or not isinstance(attestation[1], dict)
                    or attestation[1].get("state") not in {"open", "closed"}
                ):
                    cache[key] = None
                else:
                    cache[key] = attestation[1]["state"] == "open"
            verdict = cache[key]
            if verdict is True:
                return True
            if verdict is None:
                saw_unknown = True
        if saw_unknown or not saw_pr:
            return None
        return False

    return check
