"""The tracker thread model and the delivery rule shared by poller and attestor.

``delivery_decision`` decides whether an issue's current thread may be handed to
the agent. It walks every activity by someone other than the bot, newest first:
an untrusted activity newer than the last trusted assignment or comment (the
cutoff) withholds the issue, and a trusted edit never becomes a cutoff, so it
cannot hide an earlier untrusted edit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from typing import Callable, Literal

from .transport import TrackerUnavailable

ActivityKind = Literal["assigned", "comment", "comment_edit", "body_edit", "title_edit"]
ATTESTING_KINDS = frozenset({"assigned", "comment"})


@dataclass(frozen=True)
class Activity:
    kind: ActivityKind
    id: str
    at: datetime
    actor_id: str | None


@dataclass(frozen=True)
class Comment:
    id: str
    author_id: str | None
    at: datetime
    body: str


@dataclass(frozen=True)
class TrackerIssue:
    """One issue as read from a tracker.

    ``bot_id`` is the adapter's own identity in the same normalized form as the
    ``actor_id`` values; activities by it are skipped by the delivery rule.
    """

    adapter: str
    stable_id: str
    ref: str
    url: str
    project: str | None
    title: str
    body: str
    author_id: str | None
    is_open: bool
    assigned_to_bot: bool
    activities: tuple[Activity, ...]
    comments: tuple[Comment, ...]
    truncated: bool
    bot_id: str


@dataclass(frozen=True)
class Decision:
    kind: Literal["skip", "unavailable", "withhold", "deliver"]
    reason: str | None = None
    cutoff: Activity | None = None


Trust = Callable[[str, TrackerIssue], bool | None]


def activity_id(adapter: str, raw_id: str) -> str:
    """Return the stable activity id for a tracker-native ``raw_id``."""

    return "ev" + hashlib.sha256(f"{adapter}:{raw_id}".encode()).hexdigest()[:24]


def parse_timestamp(value: object) -> datetime:
    """Parse an ISO 8601 timestamp with an offset into an aware UTC datetime."""

    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no offset")
    return parsed.astimezone(UTC)


def delivery_decision(issue: TrackerIssue, trust: Trust) -> Decision:
    """Apply the delivery rule to ``issue``; ``trust`` returns None when unknown."""

    if not issue.is_open or not issue.assigned_to_bot:
        return Decision("skip")
    if issue.truncated:
        return Decision("withhold", "thread_too_long")

    verdicts: dict[str, bool | None] = {}

    def verdict(actor_id: str | None) -> bool | None:
        if actor_id is None:
            return False
        if actor_id not in verdicts:
            try:
                verdicts[actor_id] = trust(actor_id, issue)
            except TrackerUnavailable:
                verdicts[actor_id] = None
        return verdicts[actor_id]

    others = sorted(
        (a for a in issue.activities if a.actor_id is None or a.actor_id != issue.bot_id),
        key=lambda a: a.at,
        reverse=True,
    )
    for _, members in groupby(others, key=lambda a: a.at):
        group = list(members)
        verdicts_in_group = [verdict(a.actor_id) for a in group]
        if any(v is False for v in verdicts_in_group):
            return Decision("withhold", "untrusted_latest_activity")
        if any(v is None for v in verdicts_in_group):
            return Decision("unavailable")
        attesting = [a for a in group if a.kind in ATTESTING_KINDS]
        if attesting:
            return Decision("deliver", cutoff=max(attesting, key=lambda a: a.id))
    return Decision("withhold", "no_trusted_attestation")
