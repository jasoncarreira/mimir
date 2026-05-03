"""Tests for the predictions skill CLI (mimir/skills/predictions/script.py)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.skills.predictions import script as predictions


NOW = datetime(2026, 5, 2, 22, 0, tzinfo=timezone.utc)


def _ns(**overrides) -> argparse.Namespace:
    """Build a Namespace with the script's expected fields, defaulted."""
    base = dict(
        predictions_action=None,
        home=None,
        json=False,
        # add
        claim="", kind="binary", horizon_hours=24,
        verifiable_by=None, rationale="", by="agent",
        target=None, target_tool=None, tolerance=None,
        # list / review
        status=None, horizon_elapsed_only=False,
        # mark
        id=None, actual=None, lesson=None,
        # stats
        days=30,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ─── add ───────────────────────────────────────────────────────────────


def test_add_writes_jsonl_record(tmp_path: Path):
    args = _ns(
        predictions_action="add", home=tmp_path,
        claim="Tim will reply within 24h",
        kind="binary", horizon_hours=24,
        rationale="Past replies are quick",
    )
    rc = predictions.cmd_add(args)
    assert rc == 0
    records = _read_jsonl(tmp_path / "state" / "predictions.jsonl")
    assert len(records) == 1
    rec = records[0]
    assert rec["claim"] == "Tim will reply within 24h"
    assert rec["kind"] == "binary"
    assert rec["status"] == "pending"
    assert rec["verifiable_by"] == "operator_review"  # default for binary
    assert rec["id"].startswith("pred-")


def test_add_defaults_verifiable_by_per_kind(tmp_path: Path):
    rc = predictions.cmd_add(_ns(
        predictions_action="add", home=tmp_path,
        claim="Read invoked >= 5 times in 7d",
        kind="tool_freq", horizon_hours=168,
        target=5.0, target_tool="Read",
    ))
    assert rc == 0
    rec = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[0]
    assert rec["verifiable_by"] == "turns_jsonl"


def test_add_rejects_tool_freq_without_target(tmp_path: Path, capsys):
    rc = predictions.cmd_add(_ns(
        predictions_action="add", home=tmp_path,
        claim="x", kind="tool_freq", horizon_hours=24,
    ))
    assert rc == 1
    err = capsys.readouterr().err
    assert "tool_freq requires" in err


def test_add_rejects_invalid_horizon(tmp_path: Path, capsys):
    rc = predictions.cmd_add(_ns(
        predictions_action="add", home=tmp_path,
        claim="x", kind="binary", horizon_hours=0,
    ))
    assert rc == 1


# ─── list ──────────────────────────────────────────────────────────────


def test_list_empty(tmp_path: Path, capsys):
    rc = predictions.cmd_list(_ns(predictions_action="list", home=tmp_path))
    assert rc == 0
    assert "(no predictions)" in capsys.readouterr().out


def test_list_filters_by_status(tmp_path: Path, capsys):
    predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                            claim="A", kind="binary", horizon_hours=1))
    predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                            claim="B", kind="binary", horizon_hours=1))
    # Mark one wrong.
    rec = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[0]
    predictions.cmd_mark(_ns(
        predictions_action="mark", home=tmp_path,
        id=rec["id"], status="wrong", lesson="wrong assumption",
    ))
    rc = predictions.cmd_list(_ns(
        predictions_action="list", home=tmp_path, status="wrong",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "A" in out
    assert "B" not in out


# ─── mark ──────────────────────────────────────────────────────────────


def test_mark_requires_lesson_when_wrong(tmp_path: Path, capsys):
    predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                            claim="A", kind="binary", horizon_hours=1))
    pred_id = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[0]["id"]
    rc = predictions.cmd_mark(_ns(
        predictions_action="mark", home=tmp_path,
        id=pred_id, status="wrong",
    ))
    assert rc == 1
    assert "--lesson required" in capsys.readouterr().err


def test_mark_correct_persists(tmp_path: Path):
    predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                            claim="A", kind="binary", horizon_hours=1))
    pred_id = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[0]["id"]
    rc = predictions.cmd_mark(_ns(
        predictions_action="mark", home=tmp_path,
        id=pred_id, status="correct", actual="Tim replied at 18:42",
    ))
    assert rc == 0
    rec = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[0]
    assert rec["status"] == "correct"
    assert rec["actual"] == "Tim replied at 18:42"
    assert rec["reviewed_at"] is not None


def test_mark_accepts_id_substring(tmp_path: Path):
    predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                            claim="A", kind="binary", horizon_hours=1))
    pred_id = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[0]["id"]
    # Use a 4-char substring of the uuid suffix.
    short = pred_id[-4:]
    rc = predictions.cmd_mark(_ns(
        predictions_action="mark", home=tmp_path,
        id=short, status="partial",
    ))
    assert rc == 0


# ─── auto-verify ────────────────────────────────────────────────────────


def _write_turn_with_tool(path: Path, ts: datetime, tool_name: str, n: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": ts.isoformat(), "turn_id": "t", "session_id": "s",
        "saga_session_id": None,
        "trigger": "user_message", "channel_id": "c", "input": "",
        "events": [
            {"type": "tool_call", "id": f"u{i}", "name": tool_name, "args": {}}
            for i in range(n)
        ],
        "duration_ms": 100, "error": None,
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _write_event(path: Path, ts: datetime, type: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({
            "timestamp": ts.isoformat(), "type": type, "session_id": "s",
        }) + "\n")


def test_auto_verify_tool_freq_correct_when_threshold_met(tmp_path: Path):
    """tool_freq predicts ≥N invocations of a tool in the horizon."""
    made = NOW - timedelta(hours=25)  # made 25h ago, horizon 24h → past
    pred = predictions.Prediction(
        id="pred-x", made_at=made.isoformat(), by="agent",
        claim="Read >=3 in next day", kind="tool_freq",
        horizon_hours=24, verifiable_by="turns_jsonl",
        rationale="", review_after=(made + timedelta(hours=24)).isoformat(),
        target=3.0, target_tool="Read",
    )
    turns = tmp_path / "logs" / "turns.jsonl"
    # 4 Read calls within the horizon window → meets threshold.
    for i in range(4):
        _write_turn_with_tool(turns, made + timedelta(hours=i + 1), "Read")
    status, actual = predictions._auto_verify(pred, tmp_path, now=NOW)
    assert status == "correct"
    assert "Read invoked 4 times" in actual


def test_auto_verify_tool_freq_wrong_when_threshold_missed(tmp_path: Path):
    made = NOW - timedelta(hours=25)
    pred = predictions.Prediction(
        id="pred-x", made_at=made.isoformat(), by="agent",
        claim="Read >=10", kind="tool_freq", horizon_hours=24,
        verifiable_by="turns_jsonl", rationale="",
        review_after=(made + timedelta(hours=24)).isoformat(),
        target=10.0, target_tool="Read",
    )
    turns = tmp_path / "logs" / "turns.jsonl"
    _write_turn_with_tool(turns, made + timedelta(hours=1), "Read", n=2)
    status, actual = predictions._auto_verify(pred, tmp_path, now=NOW)
    assert status == "wrong"


def test_auto_verify_pending_before_horizon(tmp_path: Path):
    """If the horizon hasn't elapsed yet, status stays pending."""
    made = NOW - timedelta(hours=2)
    pred = predictions.Prediction(
        id="pred-x", made_at=made.isoformat(), by="agent",
        claim="x", kind="tool_freq", horizon_hours=24,
        verifiable_by="turns_jsonl", rationale="",
        review_after=(made + timedelta(hours=24)).isoformat(),
        target=1.0, target_tool="Read",
    )
    status, _ = predictions._auto_verify(pred, tmp_path, now=NOW)
    assert status == "pending"


def test_auto_verify_error_rate_uses_before_after_windows(tmp_path: Path):
    made = NOW - timedelta(hours=25)
    pred = predictions.Prediction(
        id="pred-x", made_at=made.isoformat(), by="agent",
        claim="errors halved", kind="error_rate", horizon_hours=24,
        verifiable_by="events_jsonl", rationale="",
        review_after=(made + timedelta(hours=24)).isoformat(),
        target=0.5,  # ratio threshold
    )
    events = tmp_path / "logs" / "events.jsonl"
    # 10 errors in the 24h before "made"
    for i in range(10):
        _write_event(events, made - timedelta(hours=i + 1), "tool_call_denied")
    # 3 errors in the 24h after — ratio 0.3 ≤ 0.5 → correct
    for i in range(3):
        _write_event(events, made + timedelta(hours=i + 1), "tool_call_denied")
    status, actual = predictions._auto_verify(pred, tmp_path, now=NOW)
    assert status == "correct"
    assert "ratio 0.30" in actual


# ─── stats ─────────────────────────────────────────────────────────────


def test_stats_computes_accuracy(tmp_path: Path, capsys):
    # 3 correct, 1 wrong, 1 pending → accuracy 75% over decided
    for i, status in enumerate(["correct", "correct", "correct", "wrong"]):
        predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                                claim=f"c{i}", kind="binary",
                                horizon_hours=1))
        pred_id = _read_jsonl(tmp_path / "state" / "predictions.jsonl")[i]["id"]
        kw = {"id": pred_id, "status": status}
        if status == "wrong":
            kw["lesson"] = "wrong"
        predictions.cmd_mark(_ns(predictions_action="mark", home=tmp_path, **kw))
    predictions.cmd_add(_ns(predictions_action="add", home=tmp_path,
                            claim="pending one", kind="binary",
                            horizon_hours=1))
    capsys.readouterr()  # discard add/mark stdout noise
    rc = predictions.cmd_stats(_ns(
        predictions_action="stats", home=tmp_path, days=30, json=True,
    ))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decided"] == 4
    assert out["accuracy"] == pytest.approx(0.75)
    assert out["by_status"]["correct"] == 3
    assert out["by_status"]["wrong"] == 1
    assert out["by_status"]["pending"] == 1
