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
from dataclasses import dataclass
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


@dataclass
class LoopDetector:
    """One instance per TurnContext. Not async-safe — relies on the per-turn
    serialization of tool calls (the SDK runs them sequentially)."""

    soft_limit: int = 5
    hard_limit: int = 10
    similarity_threshold: float = 0.9

    # Internal state — bumped by ``check``.
    _last_normalized: str | None = None
    _streak: int = 0
    _warning_reaction_emitted: bool = False

    def check(self, text: str) -> BreakerDecision:
        normalized = _normalize(text)
        previous = self._last_normalized
        if previous is None:
            ratio = 0.0
            streak = 1
        else:
            ratio = SequenceMatcher(a=previous, b=normalized).ratio()
            if ratio >= self.similarity_threshold:
                streak = self._streak + 1
            else:
                streak = 1
        self._last_normalized = normalized
        self._streak = streak

        if streak >= self.hard_limit:
            verdict = BreakerVerdict.HARD_STOP
        elif streak >= self.soft_limit:
            verdict = BreakerVerdict.SOFT_WARN
        else:
            verdict = BreakerVerdict.OK
        return BreakerDecision(verdict=verdict, streak=streak, similarity=ratio)

    def mark_warning_emitted(self) -> bool:
        """Returns True the first time it's called for this turn (caller emits
        the ``⚠️`` reaction once). Subsequent calls return False."""
        if self._warning_reaction_emitted:
            return False
        self._warning_reaction_emitted = True
        return True
