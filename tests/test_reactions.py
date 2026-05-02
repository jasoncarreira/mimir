"""Inbound-reaction → algedonic classification (mimir.reactions) +
the per-event polarity override path in mimir.feedback that consumes it."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


# ─── classifier ──────────────────────────────────────────────────


def test_classify_unicode_glyph_positive():
    from mimir.reactions import classify_reaction
    assert classify_reaction("👍") == "positive"
    assert classify_reaction("❤️") == "positive"
    assert classify_reaction("✅") == "positive"
    assert classify_reaction("💯") == "positive"


def test_classify_unicode_glyph_negative():
    from mimir.reactions import classify_reaction
    assert classify_reaction("👎") == "negative"
    assert classify_reaction("❌") == "negative"
    assert classify_reaction("💔") == "negative"


def test_classify_neutral_for_unknown():
    from mimir.reactions import classify_reaction
    assert classify_reaction("🍕") == "neutral"
    assert classify_reaction("🐍") == "neutral"
    assert classify_reaction("") == "neutral"


def test_slack_alias_normalized():
    from mimir.reactions import classify_reaction, normalize_emoji
    # Slack sends alias names (no colons in the event payload).
    assert classify_reaction("thumbsup") == "positive"
    assert classify_reaction("thumbsdown") == "negative"
    assert classify_reaction("white_check_mark") == "positive"
    # Wrapped in colons should also work.
    assert classify_reaction(":thumbsup:") == "positive"
    # Skin-tone modifier shouldn't break classification.
    assert classify_reaction("thumbsup::skin-tone-3") == "positive"
    # Unknown alias passes through unchanged → neutral.
    assert normalize_emoji("party_parrot") == "party_parrot"
    assert classify_reaction("party_parrot") == "neutral"


# ─── algedonic surfacing — per-event polarity override ───────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _ts_iso(minutes_ago: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_react_received_negative_lands_in_negatives_bucket(tmp_path):
    """A react_received event with polarity='negative' must be classified
    as a negative algedonic signal, even though the default rule is
    'positive'."""
    from mimir.feedback import FeedbackLog
    events = tmp_path / "events.jsonl"
    turns = tmp_path / "turns.jsonl"
    _write_jsonl(events, [
        {
            "timestamp": _ts_iso(2),
            "type": "react_received",
            "polarity": "negative",
            "emoji": "👎",
            "author": "discord-99",
            "channel_id": "discord-eng",
            "target_age_minutes": 1.5,
        },
    ])
    turns.write_text("")

    log = FeedbackLog(events_path=events, turns_path=turns)
    negs, pos = log.recent()
    assert len(negs) == 1
    assert "👎" in negs[0].content
    assert "discord-99" in negs[0].content
    assert "just-sent message" in negs[0].content or "1m ago" in negs[0].content
    assert pos == []


def test_react_received_positive_lands_in_positives_bucket(tmp_path):
    from mimir.feedback import FeedbackLog
    events = tmp_path / "events.jsonl"
    turns = tmp_path / "turns.jsonl"
    _write_jsonl(events, [
        {
            "timestamp": _ts_iso(5),
            "type": "react_received",
            "polarity": "positive",
            "emoji": "👍",
            "author": "slack-U05ALICE",
            "channel_id": "slack-eng",
            "target_age_minutes": 4.0,
        },
    ])
    turns.write_text("")

    log = FeedbackLog(events_path=events, turns_path=turns)
    negs, pos = log.recent()
    assert negs == []
    assert len(pos) == 1
    assert "👍" in pos[0].content
    assert "4m ago" in pos[0].content


def test_react_received_neutral_dropped(tmp_path):
    """Neutral reactions don't count as pain or pleasure — the algedonic
    block is reserved for reactions whose polarity is unambiguous."""
    from mimir.feedback import FeedbackLog
    events = tmp_path / "events.jsonl"
    turns = tmp_path / "turns.jsonl"
    _write_jsonl(events, [
        {
            "timestamp": _ts_iso(2),
            "type": "react_received",
            "polarity": "neutral",
            "emoji": "🍕",
            "author": "discord-99",
            "channel_id": "discord-eng",
        },
    ])
    turns.write_text("")

    log = FeedbackLog(events_path=events, turns_path=turns)
    negs, pos = log.recent()
    assert negs == []
    assert pos == []


def test_react_received_outside_window_dropped(tmp_path):
    """Reactions older than the window are time-gated out (default 24h)."""
    from mimir.feedback import FeedbackLog
    events = tmp_path / "events.jsonl"
    turns = tmp_path / "turns.jsonl"
    _write_jsonl(events, [
        {
            "timestamp": _ts_iso(60 * 48),  # 2 days ago
            "type": "react_received",
            "polarity": "positive",
            "emoji": "👍",
            "author": "discord-99",
            "channel_id": "discord-eng",
        },
    ])
    turns.write_text("")

    log = FeedbackLog(events_path=events, turns_path=turns)
    negs, pos = log.recent(window_hours=24)
    assert negs == []
    assert pos == []


def test_react_received_target_age_renderer(tmp_path):
    """Renderer formats target_age_minutes as humans-readable."""
    from mimir.feedback import FeedbackLog
    events = tmp_path / "events.jsonl"
    turns = tmp_path / "turns.jsonl"
    cases = [
        (0.5, "just-sent message"),
        (15, "15m ago"),
        (130, "2h ago"),
        (60 * 24 * 3, "3d ago"),
    ]
    for age, expected in cases:
        _write_jsonl(events, [
            {
                "timestamp": _ts_iso(2),
                "type": "react_received",
                "polarity": "positive",
                "emoji": "👍",
                "author": "discord-99",
                "channel_id": "discord-eng",
                "target_age_minutes": age,
            },
        ])
        turns.write_text("")
        log = FeedbackLog(events_path=events, turns_path=turns)
        _, pos = log.recent()
        assert len(pos) == 1, f"failed for age={age}"
        assert expected in pos[0].content, f"failed for age={age}: {pos[0].content!r}"
