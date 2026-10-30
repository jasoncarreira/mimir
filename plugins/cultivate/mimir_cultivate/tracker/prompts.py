"""Deterministic prompt rendering for tracker poller turns.

The prompt is rendered twice, once by the poller and once by the attestor, and
the host accepts the item only when the two are byte-identical after redaction.
Rendering therefore depends on nothing but the issue: authors appear as actor
ids, never display names, and every cap is a fixed constant.
"""

from __future__ import annotations

import re
from datetime import UTC

from mimir.poller_tracker import safe_actor

from ..worklink.planning import EPIC_TEMPLATE_MARKDOWN, LEAF_TEMPLATE_MARKDOWN
from .thread import Comment, TrackerIssue

BEGIN_MARKER = "--- BEGIN ATTESTED TRACKER THREAD ---"
END_MARKER = "--- END ATTESTED TRACKER THREAD ---"
BODY_CHARS = 4000
COMMENT_CHARS = 2000
THREAD_CHARS = 10_000
TITLE_CHARS = 200

_MARKER_RE = re.compile(r"--- (?:BEGIN|END) ATTESTED TRACKER THREAD ---")
_ACTOR_FALLBACK = "unknown"

_COMMON_RULES = (
    "Use only the tracker_file_work, tracker_comment and tracker_set_state tools for "
    "this issue. Never put internal tracker ids or work-queue ids in a comment."
)

TRIAGE_HEADER = f"""Tracker triage: {{adapter}} {{ref}} ({{url}})
External ref: {{external_ref}}

A trusted person assigned this tracker issue to you. Decide exactly one outcome:
1. No work is needed: reply with tracker_comment explaining why.
2. It needs clarification: ask on the issue with tracker_comment.
3. One leaf: call tracker_file_work with kind "leaf" and a description in this template.
4. One epic: call tracker_file_work with kind "epic" and a description in this template.
Never file child leaves; an epic is a single issue.

{_COMMON_RULES}

Leaf template:
{LEAF_TEMPLATE_MARKDOWN}

Epic template:
{EPIC_TEMPLATE_MARKDOWN}"""

UPDATE_HEADER = f"""Tracker update: {{adapter}} {{ref}} ({{url}})
External ref: {{external_ref}}

The work for this tracker issue is already filed. Do not file new work. You may reply
once with tracker_comment if the new activity needs an answer.

{_COMMON_RULES}"""

_HEADERS = {"triage": TRIAGE_HEADER, "update": UPDATE_HEADER}


def _flat(text: str, limit: int) -> str:
    return " ".join(_MARKER_RE.sub("[marker removed]", text).split())[:limit]


def _text(text: str, limit: int) -> str:
    cleaned = _MARKER_RE.sub("[marker removed]", text).strip()
    return cleaned if len(cleaned) <= limit else cleaned[:limit].rstrip() + " [truncated]"


def _actor(actor_id: str | None) -> str:
    return safe_actor(actor_id) or _ACTOR_FALLBACK


def _stamp(comment: Comment) -> str:
    return comment.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _comment_block(comment: Comment) -> str:
    header = f"Comment by {_actor(comment.author_id)} at {_stamp(comment)}:"
    return f"{header}\n{_text(comment.body, COMMENT_CHARS)}"


def render_prompt(kind: str, issue: TrackerIssue) -> str:
    """Render the turn prompt for ``kind`` (``triage`` or ``update``)."""

    header = _HEADERS[kind].format(
        adapter=issue.adapter,
        ref=issue.ref,
        url=issue.url,
        external_ref=f"{issue.adapter}:{issue.ref}",
    )
    lead = "\n".join([
        f"Issue {issue.ref}: {_flat(issue.title, TITLE_CHARS)}",
        f"Opened by {_actor(issue.author_id)}",
        "",
        _text(issue.body, BODY_CHARS),
    ]).rstrip()

    comments = sorted(issue.comments, key=lambda c: (c.at, c.id))
    blocks = [_comment_block(c) for c in comments]
    kept: list[str] = []
    used = len(lead)
    for block in reversed(blocks):
        omitted_line = f"[{len(blocks) - len(kept) - 1} earlier comments omitted]"
        reserve = len(omitted_line) + 2 if len(kept) + 1 < len(blocks) else 0
        if used + 2 + len(block) + reserve > THREAD_CHARS:
            break
        kept.append(block)
        used += 2 + len(block)
    kept.reverse()

    parts = [lead]
    if len(kept) < len(blocks):
        parts.append(f"[{len(blocks) - len(kept)} earlier comments omitted]")
    parts.extend(kept)
    thread = "\n\n".join(parts)
    return "\n".join([header, "", BEGIN_MARKER, thread, END_MARKER]).strip()
