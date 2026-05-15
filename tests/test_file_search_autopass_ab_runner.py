"""Runner-wiring tests for chainlink #140 (Sub B).

We don't drive a full mimir boot here — that's what the end-to-end
run is for. These tests cover the deterministic helpers: probe→event
shape, channel-id stability, the turns.jsonl tail reader, and the
live-bridge token suppression block.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from benchmarks.file_search_autopass_ab.route import (
    channel_id_for,
    probe_to_event,
)
from benchmarks.file_search_autopass_ab.runner import (
    _configure_arm_env,
    _suppress_live_bridges,
    _tail_turn_for_channel,
)


def test_probe_to_event_shape():
    probe = {
        "_index": 7,
        "text": "fixture 'aiohttp_server' not found",
        "expected_target": "memory/issues/pytest-aiohttp-dev-extras.md",
        "shape": "fingerprinted-error",
    }
    body = probe_to_event(probe, channel_id="bench-fsap-on-007")
    assert body["trigger"] == "user_message"
    assert body["channel_id"] == "bench-fsap-on-007"
    assert body["content"] == probe["text"]
    assert body["extra"]["probe_index"] == 7
    assert body["extra"]["shape"] == "fingerprinted-error"
    assert body["extra"]["expected_target"] == probe["expected_target"]


def test_channel_id_for_stable_per_arm_and_index():
    assert channel_id_for("on", 1) == "bench-fsap-on-001"
    assert channel_id_for("off", 1) == "bench-fsap-off-001"
    assert channel_id_for("on", 30) == "bench-fsap-on-030"
    # Distinct per arm so per-channel chat buffers don't cross-pollinate.
    assert channel_id_for("on", 5) != channel_id_for("off", 5)


def test_tail_turn_for_channel_returns_most_recent_match(tmp_path: Path):
    log = tmp_path / "turns.jsonl"
    lines = [
        {"channel_id": "bench-fsap-on-001", "duration_ms": 100, "output": "old"},
        {"channel_id": "bench-fsap-off-001", "duration_ms": 200, "output": "other"},
        {"channel_id": "bench-fsap-on-001", "duration_ms": 300, "output": "new"},
    ]
    log.write_text("\n".join(json.dumps(d) for d in lines) + "\n")
    rec = _tail_turn_for_channel(log, "bench-fsap-on-001", 0)
    assert rec is not None
    assert rec["output"] == "new"
    assert rec["duration_ms"] == 300


def test_tail_turn_for_channel_respects_byte_offset(tmp_path: Path):
    log = tmp_path / "turns.jsonl"
    first = json.dumps({"channel_id": "bench-fsap-on-001", "output": "stale"}) + "\n"
    log.write_text(first)
    offset = log.stat().st_size
    log.open("a", encoding="utf-8").write(
        json.dumps({"channel_id": "bench-fsap-on-001", "output": "fresh"}) + "\n"
    )
    rec = _tail_turn_for_channel(log, "bench-fsap-on-001", offset)
    assert rec is not None
    assert rec["output"] == "fresh"


def test_tail_turn_for_channel_returns_none_when_no_match(tmp_path: Path):
    log = tmp_path / "turns.jsonl"
    log.write_text(json.dumps({"channel_id": "other"}) + "\n")
    assert _tail_turn_for_channel(log, "bench-fsap-on-001", 0) is None


def test_tail_turn_for_channel_skips_malformed_lines(tmp_path: Path):
    log = tmp_path / "turns.jsonl"
    log.write_text(
        "not-json\n"
        + json.dumps({"channel_id": "bench-fsap-on-001", "output": "good"}) + "\n"
    )
    rec = _tail_turn_for_channel(log, "bench-fsap-on-001", 0)
    assert rec is not None
    assert rec["output"] == "good"


def test_configure_arm_env_flips_env_var(monkeypatch):
    monkeypatch.delenv("MIMIR_FILE_SEARCH_AUTOPASS_ENABLED", raising=False)
    _configure_arm_env("on")
    assert os.environ["MIMIR_FILE_SEARCH_AUTOPASS_ENABLED"] == "1"
    _configure_arm_env("off")
    assert os.environ["MIMIR_FILE_SEARCH_AUTOPASS_ENABLED"] == "0"


def test_suppress_live_bridges_clears_tokens(monkeypatch):
    """memory/issues/bench-runner-live-bridge-leak.md — bench runs must
    not connect to real Discord/Slack via inherited tokens. Also
    clears MIMIR_API_KEY so the in-process /event POST isn't 401'd
    by the auth middleware."""
    monkeypatch.setenv("DISCORD_TOKEN", "live-token-do-not-use")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-real")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-real")
    monkeypatch.setenv("MIMIR_API_KEY", "secret-key")
    _suppress_live_bridges()
    assert os.environ["DISCORD_TOKEN"] == ""
    assert os.environ["SLACK_BOT_TOKEN"] == ""
    assert os.environ["SLACK_APP_TOKEN"] == ""
    assert os.environ["MIMIR_API_KEY"] == ""
