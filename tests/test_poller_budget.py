from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.poller_budget import aggregate_poller_turn_usage


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_aggregate_poller_turn_usage_attributes_by_poller_channel(tmp_path: Path):
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "logs" / "turns.jsonl"
    _write_jsonl(
        turns,
        [
            {
                "ts": (now - timedelta(hours=25)).isoformat(),
                "channel_id": "poller:github-activity",
                "total_cost_usd": 99.0,
            },
            {
                "ts": (now - timedelta(hours=2)).isoformat(),
                "channel_id": "poller:github-activity",
                "total_cost_usd": 0.2,
            },
            {
                "ts": (now - timedelta(minutes=30)).isoformat(),
                "channel_id": "poller:github-activity",
                "total_cost_usd": 0.1,
            },
            {
                "ts": (now - timedelta(minutes=20)).isoformat(),
                "channel_id": "scheduler:heartbeat",
                "total_cost_usd": 3.0,
            },
            {
                "ts": (now - timedelta(hours=23)).isoformat(),
                "channel_id": "poller:worklink-ready-queue",
                "total_cost_usd": None,
            },
        ],
    )

    usage = aggregate_poller_turn_usage(turns, now=now)

    github = usage["github-activity"].windows
    assert github["1h"].agent_turns == 1
    assert github["1h"].total_cost_usd == 0.1
    assert github["24h"].agent_turns == 2
    assert github["24h"].total_cost_usd == pytest.approx(0.3)

    worklink = usage["worklink-ready-queue"].windows
    assert worklink["1h"].agent_turns == 0
    assert worklink["1h"].to_dict()["total_cost_usd"] == 0.0
    assert worklink["24h"].agent_turns == 1
    assert worklink["24h"].total_cost_usd is None
    assert worklink["24h"].to_dict()["total_cost_usd"] is None
    assert "scheduler:heartbeat" not in usage


def test_aggregate_poller_turn_usage_distinguishes_none_from_zero_cost(tmp_path: Path):
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "logs" / "turns.jsonl"
    _write_jsonl(
        turns,
        [
            {
                "ts": (now - timedelta(minutes=30)).isoformat(),
                "channel_id": "poller:no-cost-recorded",
                "total_cost_usd": None,
            },
            {
                "ts": (now - timedelta(minutes=30)).isoformat(),
                "channel_id": "poller:genuinely-zero",
                "total_cost_usd": 0.0,
            },
        ],
    )

    usage = aggregate_poller_turn_usage(turns, now=now)

    no_cost = usage["no-cost-recorded"].windows["1h"].to_dict()
    assert no_cost["agent_turns"] == 1
    assert no_cost["total_cost_usd"] is None

    zero_cost = usage["genuinely-zero"].windows["1h"].to_dict()
    assert zero_cost["agent_turns"] == 1
    assert zero_cost["total_cost_usd"] == 0.0


def test_aggregate_poller_turn_usage_missing_log_is_empty(tmp_path: Path):
    assert aggregate_poller_turn_usage(tmp_path / "missing.jsonl") == {}
