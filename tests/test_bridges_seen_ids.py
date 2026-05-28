"""Tests for mimir.bridges._seen_ids:SeenIdCache (chainlink #232).

The cache is a bounded LRU of inbound ``source_id`` values used by each
bridge to drop redelivered messages. These tests cover the helper in
isolation; bridge integration is covered in
``test_discord_bridge.py`` and ``test_slack_bridge.py``.
"""
from __future__ import annotations

import pytest

from mimir.bridges._seen_ids import SeenIdCache


class TestSeenIdCacheBasic:
    def test_first_call_returns_true_and_records(self) -> None:
        cache = SeenIdCache()
        assert cache.add_if_new("abc") is True
        assert "abc" in cache

    def test_second_call_returns_false(self) -> None:
        cache = SeenIdCache()
        cache.add_if_new("abc")
        assert cache.add_if_new("abc") is False

    def test_distinct_ids_both_record(self) -> None:
        cache = SeenIdCache()
        assert cache.add_if_new("a") is True
        assert cache.add_if_new("b") is True
        assert len(cache) == 2

    def test_empty_string_treated_as_new(self) -> None:
        """A bridge without a source_id must still forward the message —
        we can't dedup what we can't identify. The empty key is also NOT
        stored, so a stream of unidentified messages can't fill the cache."""
        cache = SeenIdCache()
        assert cache.add_if_new("") is True
        assert cache.add_if_new("") is True  # not recorded, still True
        assert len(cache) == 0
        assert "" not in cache


class TestSeenIdCacheEviction:
    def test_oldest_evicted_when_maxlen_exceeded(self) -> None:
        cache = SeenIdCache(maxlen=3)
        cache.add_if_new("a")
        cache.add_if_new("b")
        cache.add_if_new("c")
        cache.add_if_new("d")  # Pushes 'a' out
        assert "a" not in cache
        assert "b" in cache
        assert "c" in cache
        assert "d" in cache

    def test_hit_moves_to_end_so_hot_dup_stays_remembered(self) -> None:
        """A hot duplicate (redelivery loop) gets refreshed in LRU order
        so it doesn't age out and silently start passing again."""
        cache = SeenIdCache(maxlen=3)
        cache.add_if_new("a")
        cache.add_if_new("b")
        cache.add_if_new("c")
        # Touch 'a' — should now be the most-recently-seen.
        assert cache.add_if_new("a") is False
        # Add a new entry; the LRU should evict 'b', not 'a'.
        cache.add_if_new("d")
        assert "a" in cache
        assert "b" not in cache
        assert "c" in cache
        assert "d" in cache

    def test_maxlen_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            SeenIdCache(maxlen=0)

    def test_negative_maxlen_rejected(self) -> None:
        with pytest.raises(ValueError):
            SeenIdCache(maxlen=-1)


class TestSeenIdCacheRedeliveryScenario:
    """Models a Slack Socket-Mode ACK-loss redelivery: same ``ts`` arrives
    twice within milliseconds. The bridge must enqueue exactly once."""

    def test_double_delivery_under_a_second(self) -> None:
        cache = SeenIdCache()
        ts = "1700000000.000001"
        delivered = 0
        for _ in range(2):
            if cache.add_if_new(ts):
                delivered += 1
        assert delivered == 1

    def test_quintuple_redelivery_still_one_pass(self) -> None:
        cache = SeenIdCache()
        ts = "1700000000.000001"
        delivered = sum(1 for _ in range(5) if cache.add_if_new(ts))
        assert delivered == 1
