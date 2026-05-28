"""Algedonic-surfacing data models.

Polarity type + frozen dataclasses shared across feedback sub-modules.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


Polarity = Literal["negative", "positive"]


# ---------------------------------------------------------------------------
# Alg-2 temporal run detection — valence groups, runs, annotated runs
# (spec: state/spec/alg2-temporal-runs-spec.md)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValenceGroup:
    """A set of related kinds that form a logical success/failure space.

    Transitions are detected *within* a group only — an oauth recovery
    doesn't label a git push degradation. New groups can be added to
    ``_VALENCE_GROUPS`` without any other code changes.
    """
    label: str                       # human-readable: "git push"
    positive_kinds: frozenset[str]   # rule-kind tags (short tags like "git_push_ok")
    negative_kinds: frozenset[str]
    verb_map: dict[str, str]         # kind → short verb ("git_push_ok" → "succeeded")


@dataclass(frozen=True)
class Run:
    """A maximal contiguous subsequence of events with the same kind within
    a logical valence group, in chronological order.

    "Contiguous" means no event from the *same* group intervenes —
    events from other groups do not break a run.
    """
    group_key: str
    kind: str
    polarity: Polarity
    count: int
    start_ts: str
    end_ts: str


@dataclass(frozen=True)
class AnnotatedRun:
    """A Run tagged with the transition that opened it (if any).

    ``transition_type`` is None for the first run in a group or when the
    run continues the same polarity as the previous run.
    """
    run: Run
    transition_type: Literal["recovery", "degradation"] | None


@dataclass(frozen=True)
class FeedbackSignal:
    ts: str
    polarity: Polarity
    kind: str  # short tag: "tool_denied", "error", "saga_feedback", ...
    channel_id: str | None
    content: str  # one-line rendered description
    count: int = 1  # total occurrences of this kind in the algedonic window;
                    # > 1 means "pattern, not a one-off" — Beer arousal filter
                    # signal. Populated by the pre-pass in FeedbackLog.recent().

