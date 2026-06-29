from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.poller_budget import (
    aggregate_poller_turn_usage,
    validate_poller_usage_signal,
)


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



def test_aggregate_poller_usage_includes_external_usage_signals(tmp_path: Path):
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    turns = tmp_path / "logs" / "turns.jsonl"
    events = tmp_path / "logs" / "events.jsonl"
    _write_jsonl(turns, [])
    _write_jsonl(
        events,
        [
            {
                "ts": (now - timedelta(hours=25)).isoformat(),
                "type": "poller_usage",
                "poller": "github-activity",
                "api_calls": 99,
                "api_bytes": 99,
                "estimated_cost_usd": 99.0,
            },
            {
                "ts": (now - timedelta(minutes=45)).isoformat(),
                "type": "poller_usage",
                "poller": "github-activity",
                "api_calls": 4,
                "api_bytes": 1200,
                "estimated_cost_usd": 0.02,
            },
            {
                "ts": (now - timedelta(hours=2)).isoformat(),
                "type": "poller_usage",
                "poller": "github-activity",
                "api_calls": 3,
                "api_bytes": 800,
                "estimated_cost_usd": 0.01,
            },
            {
                "ts": (now - timedelta(minutes=20)).isoformat(),
                "type": "other",
                "poller": "github-activity",
                "api_calls": 100,
            },
        ],
    )

    usage = aggregate_poller_turn_usage(turns, events_path=events, now=now)

    github = usage["github-activity"].windows
    assert github["1h"].api_calls == 4
    assert github["1h"].api_bytes == 1200
    assert github["1h"].estimated_external_cost_usd == pytest.approx(0.02)
    assert github["24h"].api_calls == 7
    assert github["24h"].api_bytes == 2000
    assert github["24h"].estimated_external_cost_usd == pytest.approx(0.03)


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


def test_parse_poller_budget_config_accepts_window_caps(tmp_path: Path):
    from mimir.poller_budget import parse_poller_budget_config

    budget = parse_poller_budget_config(
        {
            "windows": {
                "1h": {
                    "max_agent_turns": 2,
                    "max_agent_usd": "0.5",
                    "max_api_calls": 30,
                    "max_api_bytes": 2_000_000,
                    "max_external_usd": 0.25,
                },
                "24h": {"max_agent_turns": 12},
            },
            "on_exceed": "suppress",
        },
        source=tmp_path / "pollers.json",
        poller_name="github-activity",
    )

    assert budget is not None
    assert budget.to_dict() == {
        "on_exceed": "suppress",
        "windows": {
            "1h": {
                "max_agent_turns": 2,
                "max_agent_usd": 0.5,
                "max_api_calls": 30,
                "max_api_bytes": 2_000_000,
                "max_external_usd": 0.25,
            },
            "24h": {"max_agent_turns": 12},
        },
    }


def test_parse_poller_budget_config_warns_and_drops_unsupported_windows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    from mimir.poller_budget import parse_poller_budget_config

    with caplog.at_level(logging.WARNING, logger="mimir.poller_budget"):
        budget = parse_poller_budget_config(
            {
                "windows": {
                    "1h": {"max_agent_turns": 2},
                    "6h": {"max_agent_turns": 6},
                    "7d": {"max_external_usd": 0.25},
                    "24h": {"max_api_calls": 200},
                },
                "on_exceed": "suppress",
            },
            source=tmp_path / "pollers.json",
            poller_name="github-activity",
        )

    assert budget is not None
    assert budget.to_dict() == {
        "on_exceed": "suppress",
        "windows": {
            "1h": {"max_agent_turns": 2},
            "24h": {"max_api_calls": 200},
        },
    }
    assert "poller_budget_invalid" in caplog.text
    assert "windows.6h is unsupported" in caplog.text
    assert "windows.7d is unsupported" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"windows": {}},
        {"windows": {"1h": {}}},
        {"windows": {"1h": {"max_agent_turns": -1}}},
        {"windows": {"1h": {"max_agent_turns": True}}},
        {"windows": {"1h": {"unknown": 1}}},
        {"windows": {"6h": {"max_agent_turns": 2}}},
        {"windows": {"7d": {"max_external_usd": 0.25}}},
        {"windows": {"1h": {"max_agent_usd": "NaN"}}},
        {"windows": {"1h": {"max_agent_turns": 1}}, "on_exceed": "warn"},
    ],
)
def test_parse_poller_budget_config_rejects_invalid_shapes_fail_open(
    tmp_path: Path, raw: object, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    from mimir.poller_budget import parse_poller_budget_config

    with caplog.at_level(logging.WARNING, logger="mimir.poller_budget"):
        budget = parse_poller_budget_config(
            raw,
            source=tmp_path / "pollers.json",
            poller_name="github-activity",
        )

    assert budget is None
    assert "poller_budget_invalid" in caplog.text


def test_validate_poller_usage_signal_accepts_and_defaults_metrics():
    payload, reason = validate_poller_usage_signal(
        {
            "poller": "github-activity",
            "signal": "poller_usage",
            "api_calls": "4",
            "api_bytes": 183242,
            "estimated_cost_usd": 0.0,
            "source": "github-rest",
        },
        poller_name="github-activity",
    )

    assert reason is None
    assert payload == {
        "api_calls": 4,
        "api_bytes": 183242,
        "estimated_cost_usd": 0.0,
        "source": "github-rest",
    }

    payload, reason = validate_poller_usage_signal(
        {"poller": "github-activity", "signal": "poller_usage"},
        poller_name="github-activity",
    )
    assert reason is None
    assert payload == {"api_calls": 0, "api_bytes": 0, "estimated_cost_usd": 0}


def test_validate_poller_usage_signal_rejects_mismatch_and_bad_metrics():
    payload, reason = validate_poller_usage_signal(
        {"poller": "other", "signal": "poller_usage", "api_calls": 1},
        poller_name="github-activity",
    )
    assert payload is None
    assert reason == "poller_mismatch"

    for metric in ("api_calls", "api_bytes", "estimated_cost_usd"):
        payload, reason = validate_poller_usage_signal(
            {"poller": "github-activity", "signal": "poller_usage", metric: -1},
            poller_name="github-activity",
        )
        assert payload is None
        assert reason == f"invalid_{metric}"

    payload, reason = validate_poller_usage_signal(
        {"poller": "github-activity", "signal": "poller_usage", "api_calls": True},
        poller_name="github-activity",
    )
    assert payload is None
    assert reason == "invalid_api_calls"
