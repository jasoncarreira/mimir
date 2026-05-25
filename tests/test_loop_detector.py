"""Loop-detection circuit breaker (SPEC §7.2.4)."""

from __future__ import annotations

from mimir.loop_detector import BreakerVerdict, LoopDetector


def test_first_send_is_ok():
    d = LoopDetector(soft_limit=3, hard_limit=5, similarity_threshold=0.9)
    decision = d.check("hello world")
    assert decision.verdict == BreakerVerdict.OK
    assert decision.streak == 1
    assert decision.similarity == 0.0


def test_dissimilar_sends_keep_streak_at_one():
    d = LoopDetector(soft_limit=3, hard_limit=5, similarity_threshold=0.9)
    d.check("foo")
    decision = d.check("totally different content here, nothing alike")
    assert decision.streak == 1
    assert decision.verdict == BreakerVerdict.OK


def test_near_duplicate_sends_increment_streak():
    d = LoopDetector(soft_limit=3, hard_limit=5, similarity_threshold=0.9)
    text = "Sure, I can help with that."
    streaks = []
    for _ in range(4):
        streaks.append(d.check(text).streak)
    assert streaks == [1, 2, 3, 4]


def test_soft_warn_at_soft_limit():
    d = LoopDetector(soft_limit=3, hard_limit=10, similarity_threshold=0.9)
    text = "I'll get on it now."
    for _ in range(2):
        assert d.check(text).verdict == BreakerVerdict.OK
    decision = d.check(text)
    assert decision.verdict == BreakerVerdict.SOFT_WARN
    assert decision.streak == 3


def test_hard_stop_at_hard_limit():
    d = LoopDetector(soft_limit=3, hard_limit=5, similarity_threshold=0.9)
    text = "Working on it."
    for i in range(4):
        d.check(text)
    decision = d.check(text)  # 5th
    assert decision.verdict == BreakerVerdict.HARD_STOP
    assert decision.streak == 5


def test_streak_resets_on_dissimilar():
    d = LoopDetector(soft_limit=3, hard_limit=5, similarity_threshold=0.9)
    d.check("xx")
    d.check("xx")
    assert d.check("totally different topic now, the cat is on the mat").streak == 1


def test_normalization_treats_whitespace_and_case_as_equivalent():
    d = LoopDetector(soft_limit=3, hard_limit=5, similarity_threshold=0.99)
    d.check("Hello there")
    decision = d.check("HELLO  THERE")  # different case + extra space
    assert decision.streak == 2
    assert decision.similarity > 0.99


def test_warning_emitted_only_once():
    d = LoopDetector(soft_limit=3, hard_limit=10, similarity_threshold=0.9)
    assert d.mark_warning_emitted() is True
    assert d.mark_warning_emitted() is False


# ---------------------------------------------------------------------------
# Sliding-window (chainlink #183) — A,B,A,B,... alternation detection
# ---------------------------------------------------------------------------


def test_alternating_sends_trigger_streak():
    """A,B,A,B alternation should increment the streak via sliding window."""
    d = LoopDetector(soft_limit=6, hard_limit=10, similarity_threshold=0.9, window_size=5)
    a = "Here is a summary of the latest findings from my analysis."
    b = "Let me process that request for you right now, one moment."
    # A: no history — streak 1
    assert d.check(a).streak == 1
    # B: not similar to A — streak 1
    assert d.check(b).streak == 1
    # Second A: similar to A in window — streak 2
    assert d.check(a).streak == 2
    # Second B: similar to B in window — streak 3
    assert d.check(b).streak == 3
    # Third A: similar to A in window — streak 4
    assert d.check(a).streak == 4


def test_window_size_limits_lookback():
    """Messages older than window_size should NOT trigger a match."""
    d = LoopDetector(soft_limit=5, hard_limit=10, similarity_threshold=0.9, window_size=2)
    a = "This is message alpha, quite distinctive from everything else here."
    # Fill the window past capacity so 'a' is evicted.
    d.check(a)                               # history: [a]
    d.check("unique filler message number 1")  # history: [a, filler1]
    d.check("unique filler message number 2")  # history: [filler1, filler2] — a evicted
    # Re-sending a should not find it in the window → streak 1 (no match).
    decision = d.check(a)
    assert decision.streak == 1
