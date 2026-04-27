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
