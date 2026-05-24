"""S2-2: cross-turn send_message loop detection.

Tests for:
1. ``_detect_cross_turn_send_loops`` — detects (channel_id × content_hash)
   pairs that appear 3+ times in the 24h window.
2. 24h dedup — a pair that already has a ``cross_turn_send_duplicate`` event
   in the window is not flagged again.
3. Threshold — 2 sends is below threshold; no signal.
4. ``send_message`` tool emits ``send_message_sent`` with content_hash on
   successful delivery.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from mimir.feedback import FeedbackSignal, _detect_cross_turn_send_loops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _write_events(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _norm_hash(text: str) -> str:
    """Mirror the normalization in send_message to produce the expected hash."""
    norm = re.sub(r"\s+", " ", text.strip()).lower()[:500]
    return hashlib.md5(norm.encode()).hexdigest()[:16]


def _cutoff_iso() -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=24)).isoformat()


# ---------------------------------------------------------------------------
# _detect_cross_turn_send_loops — unit tests
# ---------------------------------------------------------------------------

class TestDetectCrossTurnSendLoops:
    def test_three_sends_triggers_signal(self, tmp_path: Path) -> None:
        """3 send_message_sent events for the same (channel, hash) → 1 signal."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("alert: disk full")
        _write_events(events, [
            {"type": "send_message_sent", "timestamp": _ts(0.5), "channel_id": "op", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(1.0), "channel_id": "op", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(2.0), "channel_id": "op", "content_hash": ch},
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert len(signals) == 1
        sig = signals[0]
        assert sig.polarity == "negative"
        assert sig.kind == "cross_turn_loop"
        assert sig.count == 3
        assert "op" in sig.content

    def test_two_sends_no_signal(self, tmp_path: Path) -> None:
        """2 sends is below the threshold of 3 — no signal."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("status: ok")
        _write_events(events, [
            {"type": "send_message_sent", "timestamp": _ts(1), "channel_id": "op", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(2), "channel_id": "op", "content_hash": ch},
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert signals == []

    def test_different_channels_not_merged(self, tmp_path: Path) -> None:
        """Same content_hash to different channels counts separately."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("hello world")
        _write_events(events, [
            {"type": "send_message_sent", "timestamp": _ts(0.5), "channel_id": "ch-A", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(1.0), "channel_id": "ch-B", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(1.5), "channel_id": "ch-A", "content_hash": ch},
            # ch-A: 2 sends — below threshold; ch-B: 1 send — below threshold
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert signals == []

    def test_different_hashes_not_merged(self, tmp_path: Path) -> None:
        """Different content hashes to the same channel count separately."""
        events = tmp_path / "events.jsonl"
        ch_a = _norm_hash("message A")
        ch_b = _norm_hash("message B")
        _write_events(events, [
            {"type": "send_message_sent", "timestamp": _ts(0.5), "channel_id": "op", "content_hash": ch_a},
            {"type": "send_message_sent", "timestamp": _ts(1.0), "channel_id": "op", "content_hash": ch_b},
            {"type": "send_message_sent", "timestamp": _ts(1.5), "channel_id": "op", "content_hash": ch_a},
            # ch_a: 2 sends; ch_b: 1 send — both below threshold
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert signals == []

    def test_24h_dedup_suppresses_reflag(self, tmp_path: Path) -> None:
        """A pair already in ``already_flagged`` (prior cross_turn_send_duplicate
        event within the window) is not re-signalled."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("repeated alert")
        _write_events(events, [
            # 3 sends — would normally trigger
            {"type": "send_message_sent", "timestamp": _ts(3), "channel_id": "op", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(4), "channel_id": "op", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(5), "channel_id": "op", "content_hash": ch},
            # Already flagged in this window
            {"type": "cross_turn_send_duplicate", "timestamp": _ts(2), "channel_id": "op", "content_hash": ch, "count": 3},
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert signals == []

    def test_outside_window_ignored(self, tmp_path: Path) -> None:
        """Events older than 24h are not counted."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("old message")
        _write_events(events, [
            # Only one event inside the window
            {"type": "send_message_sent", "timestamp": _ts(1), "channel_id": "op", "content_hash": ch},
            # Two events outside the window
            {"type": "send_message_sent", "timestamp": _ts(25), "channel_id": "op", "content_hash": ch},
            {"type": "send_message_sent", "timestamp": _ts(26), "channel_id": "op", "content_hash": ch},
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert signals == []

    def test_empty_events_file(self, tmp_path: Path) -> None:
        """Empty / missing events file returns empty list — no crash."""
        events = tmp_path / "events.jsonl"
        # File doesn't exist
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert signals == []

    def test_count_in_signal_is_accurate(self, tmp_path: Path) -> None:
        """FeedbackSignal.count reflects the actual send count."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("flood message")
        _write_events(events, [
            {"type": "send_message_sent", "timestamp": _ts(i * 0.5), "channel_id": "op", "content_hash": ch}
            for i in range(7)  # 7 sends
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso())
        assert len(signals) == 1
        assert signals[0].count == 7

    def test_custom_threshold(self, tmp_path: Path) -> None:
        """threshold=5 requires 5 sends before triggering."""
        events = tmp_path / "events.jsonl"
        ch = _norm_hash("borderline message")
        _write_events(events, [
            {"type": "send_message_sent", "timestamp": _ts(i * 0.5), "channel_id": "op", "content_hash": ch}
            for i in range(4)  # 4 sends — below threshold=5
        ])
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso(), threshold=5)
        assert signals == []
        # Add one more send (5 total) → triggers
        with events.open("a") as f:
            f.write(json.dumps({
                "type": "send_message_sent", "timestamp": _ts(2.5), "channel_id": "op", "content_hash": ch,
            }) + "\n")
        signals = _detect_cross_turn_send_loops(None, events, _cutoff_iso(), threshold=5)
        assert len(signals) == 1


# ---------------------------------------------------------------------------
# send_message tool — emits send_message_sent on successful delivery
# ---------------------------------------------------------------------------

class _FakeBridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._counter = 0

    async def send(self, channel_id: str, text: str) -> str:
        self.sent.append((channel_id, text))
        self._counter += 1
        return f"msg-{self._counter}"

    async def react(self, channel_id: str, message_id: str | None, emoji: str) -> None:
        pass


class _FakeRegistry:
    def __init__(self, bridge: _FakeBridge) -> None:
        self._bridge = bridge

    def find(self, channel_id: str):
        return self._bridge


@pytest.fixture
def fake_bridge(monkeypatch: pytest.MonkeyPatch) -> _FakeBridge:
    from mimir.tools import registry as tool_registry
    bridge = _FakeBridge()
    reg = _FakeRegistry(bridge)
    prev = tool_registry._STATE.get("channel_registry")
    tool_registry._STATE["channel_registry"] = reg
    yield bridge
    tool_registry._STATE["channel_registry"] = prev


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        events.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", _capture)
    return events


@pytest.fixture(autouse=True)
def _reset_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools import registry as tool_registry
    yield
    tool_registry.set_current_channel_id(None)


@pytest.mark.asyncio
async def test_send_message_emits_send_message_sent(
    fake_bridge: _FakeBridge,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Successful send_message emits a ``send_message_sent`` event with
    channel_id and a content_hash field."""
    from mimir.tools import registry as tool_registry
    from mimir.tools.registry import send_message

    tool_registry.set_current_channel_id("op-channel")
    result = await send_message.ainvoke({"text": "hello operator", "channel_id": "op-channel"})
    assert "ok" in result

    sent_kinds = [k for k, _ in captured_events]
    assert "send_message_sent" in sent_kinds, f"Expected send_message_sent in {sent_kinds}"

    sent_ev = next(kw for k, kw in captured_events if k == "send_message_sent")
    assert sent_ev["channel_id"] == "op-channel"
    assert "content_hash" in sent_ev
    # Verify the hash is a 16-char hex string
    assert isinstance(sent_ev["content_hash"], str)
    assert len(sent_ev["content_hash"]) == 16
    assert all(c in "0123456789abcdef" for c in sent_ev["content_hash"])


@pytest.mark.asyncio
async def test_send_message_content_hash_is_reproducible(
    fake_bridge: _FakeBridge,
    captured_events: list[tuple[str, dict]],
) -> None:
    """Sending the same text twice produces the same content_hash,
    enabling the cross-turn dedup to match."""
    from mimir.tools import registry as tool_registry
    from mimir.tools.registry import send_message

    tool_registry.set_current_channel_id("op-channel")
    text = "Operator: wiki health check complete."
    await send_message.ainvoke({"text": text, "channel_id": "op-channel"})
    await send_message.ainvoke({"text": text, "channel_id": "op-channel"})

    sent_events = [(k, kw) for k, kw in captured_events if k == "send_message_sent"]
    assert len(sent_events) == 2
    assert sent_events[0][1]["content_hash"] == sent_events[1][1]["content_hash"]


@pytest.mark.asyncio
async def test_send_message_no_sent_event_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict]],
) -> None:
    """When no channel registry is configured, send_message fails and does
    NOT emit send_message_sent (nothing was delivered)."""
    from mimir.tools import registry as tool_registry
    from mimir.tools.registry import send_message

    prev = tool_registry._STATE.get("channel_registry")
    tool_registry._STATE["channel_registry"] = None
    try:
        result = await send_message.ainvoke({"text": "test", "channel_id": "x"})
        assert "failed" in result
    finally:
        tool_registry._STATE["channel_registry"] = prev
        tool_registry.set_current_channel_id(None)

    sent_kinds = [k for k, _ in captured_events]
    assert "send_message_sent" not in sent_kinds
