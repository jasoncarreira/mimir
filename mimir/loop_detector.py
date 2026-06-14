"""Per-turn ``send_message`` loop-detection circuit breaker (SPEC §7.2.4).

Verbatim port of the algorithm in ``open_strix/tools.py:282-453``:
- Track normalized text of the last send_message call within the turn.
- Compute ``difflib.SequenceMatcher.ratio`` against the previous text.
- If similarity >= threshold, increment a streak; otherwise reset to 1.
- At ``soft_limit`` consecutive near-duplicates, emit a warning and arm
  the breaker (subsequent sends still go through but the model is told
  to stop).
- At ``hard_limit``, refuse the send entirely. The warning reaction is
  emitted once via the bridge so a human watching can see the stop.

Mimir's twist on open-strix: a fresh ``LoopDetector`` per ``TurnContext``
(one per ``run_turn``), so two channels' breakers don't interfere. Open-strix
keeps state on the App and resets between turns; we just allocate fresh.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum

WARNING_REACTION = "⚠️"
ERROR_REACTION = "❌"


class BreakerVerdict(Enum):
    OK = "ok"               # below soft limit; just send
    SOFT_WARN = "soft_warn"  # at or above soft, below hard; send with warning
    HARD_STOP = "hard_stop"  # at or above hard; refuse send


@dataclass
class BreakerDecision:
    verdict: BreakerVerdict
    streak: int
    similarity: float


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


# VSM: S2 — per-channel anti-oscillation; refuses duplicate-or-near-
#          duplicate sends past a soft threshold, hard-stops past a
#          higher one. Runs synchronously inside the send_message tool
#          wrapper so the agent gets a permission-denied result and
#          adjusts within-turn.
# loop_id: 1.3
@dataclass
class LoopDetector:
    """One instance per TurnContext. Not async-safe — relies on the per-turn
    serialization of tool calls (the SDK runs them sequentially).

    Sliding-window detection: checks the current message against the last
    ``window_size`` messages, not just the immediate predecessor.  This
    catches A,B,A,B,... alternation patterns that bypassed the original
    one-back design.
    """

    soft_limit: int = 5
    hard_limit: int = 10
    similarity_threshold: float = 0.9
    window_size: int = 10

    # Internal state — bumped by ``check``.
    _streak: int = 0
    _warning_reaction_emitted: bool = False
    # Delivery-failure backstop state. This intentionally lives outside
    # ``snapshot``/``restore``: undelivered attempts roll back the delivered-send
    # streak/history, but repeated identical undelivered attempts still need a
    # separate bound so a bridge failure cannot loop forever.
    _undelivered_streak: int = 0
    _last_undelivered_text: str | None = None
    # Last window_size normalized texts; bounded deque created in __post_init__.
    _history: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        # default_factory produces an unbounded deque; bind it to window_size.
        self._history = deque(self._history, maxlen=self.window_size)

    def snapshot(self) -> tuple[int, bool, tuple[str, ...]]:
        """Capture mutable state before a speculative ``check``.

        ``send_message`` runs the breaker before bridge delivery so it can
        refuse hard loops early, but bridge lookup / delivery can still fail.
        A failed delivery must not poison the next deliverable send, so callers
        snapshot before ``check`` and restore if the message was not delivered.
        """
        return (
            self._streak,
            self._warning_reaction_emitted,
            tuple(self._history),
        )

    def restore(self, state: tuple[int, bool, tuple[str, ...]]) -> None:
        """Restore a state captured by ``snapshot``."""
        streak, warning_emitted, history = state
        self._streak = streak
        self._warning_reaction_emitted = warning_emitted
        self._history = deque(history, maxlen=self.window_size)

    def check(self, text: str) -> BreakerDecision:
        normalized = _normalize(text)

        if not self._history:
            ratio = 0.0
            streak = 1
        else:
            # Sliding window: match against ANY message in history, not just
            # the most recent.  Catches A,B,A,B,... alternation loops.
            ratio = max(
                SequenceMatcher(a=prev, b=normalized).ratio()
                for prev in self._history
            )
            if ratio >= self.similarity_threshold:
                streak = self._streak + 1
            else:
                streak = 1

        self._history.append(normalized)
        self._streak = streak

        if streak >= self.hard_limit:
            verdict = BreakerVerdict.HARD_STOP
        elif streak >= self.soft_limit:
            verdict = BreakerVerdict.SOFT_WARN
        else:
            verdict = BreakerVerdict.OK
        return BreakerDecision(verdict=verdict, streak=streak, similarity=ratio)

    def check_undelivered_backstop(self, text: str) -> BreakerDecision | None:
        """Refuse another identical retry after prior undelivered hard-stop.

        The Nth failed attempt is allowed to reach the bridge so a transient
        failure can still recover; if it also fails, ``record_undelivered_attempt``
        returns HARD_STOP. Later identical attempts are refused before delivery.
        """
        normalized = _normalize(text)
        if (
            normalized
            and normalized == self._last_undelivered_text
            and self._undelivered_streak >= self.hard_limit
        ):
            return BreakerDecision(
                verdict=BreakerVerdict.HARD_STOP,
                streak=self._undelivered_streak,
                similarity=1.0,
            )
        return None

    def record_undelivered_attempt(self, text: str) -> BreakerDecision:
        """Track repeated identical attempts that failed before delivery.

        Callers use this after restoring the delivered-send state for a bridge
        exception / ``sent=False`` result / directive-only no-op. It preserves
        PR #676's core behavior (undelivered sends don't poison the delivered
        streak/history) while still bounding pathological retry loops when the
        agent keeps trying the same failed send.
        """
        normalized = _normalize(text)
        if normalized and normalized == self._last_undelivered_text:
            streak = self._undelivered_streak + 1
        else:
            streak = 1
        self._last_undelivered_text = normalized
        self._undelivered_streak = streak

        if streak >= self.hard_limit:
            verdict = BreakerVerdict.HARD_STOP
        elif streak >= self.soft_limit:
            verdict = BreakerVerdict.SOFT_WARN
        else:
            verdict = BreakerVerdict.OK
        # Identical comparison by construction; first attempt has no prior
        # undelivered attempt to compare against.
        similarity = 1.0 if streak > 1 else 0.0
        return BreakerDecision(verdict=verdict, streak=streak, similarity=similarity)

    def clear_undelivered_attempts(self) -> None:
        """Reset the failure backstop after a delivered-send check starts."""
        self._undelivered_streak = 0
        self._last_undelivered_text = None

    def mark_warning_emitted(self) -> bool:
        """Returns True the first time it's called for this turn (caller emits
        the ``⚠️`` reaction once). Subsequent calls return False."""
        if self._warning_reaction_emitted:
            return False
        self._warning_reaction_emitted = True
        return True
