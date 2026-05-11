"""Commitment data model.

A commitment is a durable record of a future obligation the agent has
accepted. JSONL events ("commitment_added" / "_delivered" / "_completed"
/ ...) are the canonical storage; ``CommitmentRecord`` is the replayed
in-memory current state.

Why split events vs. record? Append-only event log fits mimir's
existing patterns (events.jsonl, turns.jsonl, session_boundaries.jsonl)
and gives us free audit history. The record is a denormalized view
useful for prompt rendering / CLI display.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum


class CommitmentKind(str, Enum):
    """What kind of obligation this is.

    AGENT_PROMISE  — "I'll review the PR Thursday"
    USER_REQUEST   — "let me know how the deploy goes"
    DEADLINE_CHECK — explicit time-anchored check
    OPEN_LOOP      — unresolved topic to revisit (no specific promise,
                     but worth surfacing at session boundaries)
    """

    AGENT_PROMISE = "agent_promise"
    USER_REQUEST = "user_request"
    DEADLINE_CHECK = "deadline_check"
    OPEN_LOOP = "open_loop"


class CommitmentSensitivity(str, Enum):
    """Tone tier for delivery / surfacing.

    ROUTINE  — work / ops tracking ("review PR #111")
    PERSONAL — individual-but-mundane ("send Bob the draft")
    CARE     — wellbeing follow-ups (friend's hard week, health item)
    """

    ROUTINE = "routine"
    PERSONAL = "personal"
    CARE = "care"


class CommitmentStatus(str, Enum):
    """Lifecycle states. Replay-to-state derives the current value from
    the most recent lifecycle event for the commitment id.

    PENDING   — created, not yet delivered
    DELIVERED — reminder fired (may re-fire on attempts)
    COMPLETED — agent followed through; terminal
    DISMISSED — agent dropped it as no longer relevant; terminal
    SNOOZED   — pushed out to a later time
    EXPIRED   — due_window_end passed without resolution; terminal
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset({
    CommitmentStatus.COMPLETED.value,
    CommitmentStatus.DISMISSED.value,
    CommitmentStatus.EXPIRED.value,
})


# Adjacency map for status transitions. Replay consults this when
# applying a lifecycle event to a known record — any transition not
# listed here is rejected (warning + skip) so the lifecycle invariant
# lives in code, not just prose. PR #120 review finding #1.
#
# Self-transitions are allowed for non-terminal states to model
# "re-deliver" (each attempt bumps ``attempts``; status stays the
# same) and "re-snooze" (push out further). Terminal states allow
# no transitions; once completed/dismissed/expired, a record is
# frozen.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    CommitmentStatus.PENDING.value: frozenset({
        CommitmentStatus.DELIVERED.value,
        CommitmentStatus.COMPLETED.value,
        CommitmentStatus.DISMISSED.value,
        CommitmentStatus.SNOOZED.value,
        CommitmentStatus.EXPIRED.value,
    }),
    CommitmentStatus.DELIVERED.value: frozenset({
        CommitmentStatus.DELIVERED.value,  # re-deliver (attempt bump)
        CommitmentStatus.COMPLETED.value,
        CommitmentStatus.DISMISSED.value,
        CommitmentStatus.SNOOZED.value,
        CommitmentStatus.EXPIRED.value,
    }),
    CommitmentStatus.SNOOZED.value: frozenset({
        CommitmentStatus.DELIVERED.value,
        CommitmentStatus.COMPLETED.value,
        CommitmentStatus.DISMISSED.value,
        CommitmentStatus.SNOOZED.value,  # re-snooze (push out further)
        CommitmentStatus.EXPIRED.value,
    }),
    CommitmentStatus.COMPLETED.value: frozenset(),
    CommitmentStatus.DISMISSED.value: frozenset(),
    CommitmentStatus.EXPIRED.value: frozenset(),
}

# Event-type → target status. Used by replay + the transition guard
# to map an incoming lifecycle event to the status it would produce.
EVENT_TO_TARGET_STATUS: dict[str, str] = {
    "commitment_delivered": CommitmentStatus.DELIVERED.value,
    "commitment_completed": CommitmentStatus.COMPLETED.value,
    "commitment_snoozed": CommitmentStatus.SNOOZED.value,
    "commitment_dismissed": CommitmentStatus.DISMISSED.value,
    "commitment_expired": CommitmentStatus.EXPIRED.value,
}


# Default window length applied when ``snooze`` slides ``start`` past
# the existing ``end`` (or when end is None). Matches the CLI's
# "default end = start + 7d" convention so snooze + add stay
# consistent. PR #120 review finding #3.
DEFAULT_SNOOZE_WINDOW_SECS = 7 * 86400


@dataclass
class CommitmentRecord:
    """Replayed current state of a commitment. The store's append-only
    JSONL holds the events; this dataclass is the denormalized view
    after replay.

    Constructing one directly (for ``store.add(...)``) sets the initial
    fields; lifecycle methods on the store update them via appended
    events that the replay then materializes.

    Fields:
    - ``id``: unique handle, ``c-<10 hex>`` for visual disambiguation
      from turn_ids (which are 12 hex).
    - ``channel_id``: where the commitment was made — also the default
      delivery channel (where made = where delivered, common case).
      ``None`` for channel-agnostic / agent-internal commitments —
      surfaced cross-channel under a per-prompt budget.
    - ``recipient_identity``: canonical identity (e.g., a display name
      or platform-resolved handle) the commitment is for. ``None`` for
      agent-internal commitments. Distinct from ``channel_id`` because
      a single channel can have multiple participants — when a reminder
      fires, the surfacing layer @-mentions ``recipient_identity`` so
      the right person sees it. Resolved via ``identities.py`` at
      extraction time.
    - ``text``: natural-language description of the obligation,
      ≤120 chars by convention.
    - ``suggested_reminder``: what to say at delivery, ≤200 chars.
    - ``due_window_start_unix`` / ``due_window_end_unix``: range over
      which delivery is allowed. ``None`` = no time anchor (surface at
      every session boundary until resolved).
    - ``dedupe_key``: hash of (channel_id, normalized_text, due-day) so
      re-extraction of the same commitment is idempotent.
    - ``confidence``: extractor confidence 0-1 (1.0 for manually-added
      commitments).
    - ``attempts``: number of delivery attempts (incremented by deliver).
    """

    id: str
    channel_id: str | None
    text: str
    kind: str = CommitmentKind.OPEN_LOOP.value
    sensitivity: str = CommitmentSensitivity.ROUTINE.value
    recipient_identity: str | None = None
    suggested_reminder: str = ""
    due_window_start_unix: float | None = None
    due_window_end_unix: float | None = None
    status: str = CommitmentStatus.PENDING.value
    created_at_unix: float = 0.0
    delivered_at_unix: float | None = None
    completed_at_unix: float | None = None
    dismissed_at_unix: float | None = None
    snoozed_until_unix: float | None = None
    expired_at_unix: float | None = None
    attempts: int = 0
    confidence: float = 1.0
    dedupe_key: str = ""
    source_turn_id: str | None = None
    saga_session_id: str | None = None
    completion_message_id: str | None = None
    dismiss_reason: str | None = None
    snooze_reason: str | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict:
        """For JSONL serialization on the initial ``commitment_added``
        event. Includes all fields (even None) so the replay's
        ``CommitmentRecord(**rec_data)`` reconstruction has every
        keyword argument the dataclass expects."""
        return dict(self.__dict__)


def make_commitment_id() -> str:
    """``c-<10 hex>``. Distinct shape from turn_id (12 hex) so the
    operator can tell them apart at a glance in logs / CLI output."""
    return "c-" + uuid.uuid4().hex[:10]


_WHITESPACE_RE = re.compile(r"\s+")


def make_dedupe_key(
    *,
    channel_id: str | None,
    text: str,
    due_window_start_unix: float | None,
    recipient_identity: str | None = None,
) -> str:
    """Stable hash used to detect when the same commitment is extracted
    twice. Same channel + recipient + normalized text + same due-day →
    same key.

    Including ``recipient_identity`` means "remind Alice about X" and
    "remind Bob about X" in the same channel are distinct commitments
    — without it they'd dedupe together and one would lose its
    addressee on re-extraction.

    Normalization: lowercase, collapse internal whitespace, strip.
    Due-day: floor(start / 86400). ``None`` due window hashes as the
    literal string ``"none"`` (so all no-deadline commitments with the
    same channel+recipient+text dedupe together).
    """
    text_norm = _WHITESPACE_RE.sub(" ", (text or "").lower()).strip()
    if due_window_start_unix is None:
        day_bucket = "none"
    else:
        day_bucket = str(int(due_window_start_unix // 86400))
    payload = (
        f"{channel_id or ''}|{recipient_identity or ''}|"
        f"{text_norm}|{day_bucket}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
