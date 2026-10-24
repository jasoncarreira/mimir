"""CLI report against TurnLogger-shaped retained fixtures."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cache_report.py"
HOME = Path(__file__).parent / "fixtures" / "cache_report_home"


def run_report(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(home), *args],
        capture_output=True, text=True, check=False,
    )


def test_exact_report_and_read_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # Copy only committed log data; never inherit an ignored local .env.
    shutil.copytree(HOME / "logs", home / "logs")
    (home / ".env").write_text("MIMIR_MODEL_SPEC=anthropic:claude-sonnet-4-6\n", encoding="utf-8")
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    result = run_report(home, "--from-date", "2026-05-23", "--to-date", "2026-05-25")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        "Cache-hit report (UTC 2026-05-23 through 2026-05-25)\n"
        "Configured agent model (current home/.env): anthropic:claude-sonnet-4-6\n"
        "Per-turn model attribution unavailable in turns.jsonl.\n"
        "Input token semantics: inclusive (selected for the entire window).\n"
        "Shares use read + write + uncached input tokens from measured turns only.\n"
        "Total: turns=6 measured=4 read=155 (62.0%) write=30 (12.0%) uncached=65 (26.0%)\n"
        "Day:\n"
        "  2026-05-23: turns=2 measured=2 read=60 (42.9%) write=30 (21.4%) uncached=50 (35.7%)\n"
        "  2026-05-24: turns=2 measured=1 read=90 (90.0%) write=0 (0.0%) uncached=10 (10.0%)\n"
        "  2026-05-25: turns=2 measured=1 read=5 (50.0%) write=0 (0.0%) uncached=5 (50.0%)\n"
        "Trigger:\n"
        "  poller: turns=2 measured=1 read=60 (60.0%) write=20 (20.0%) uncached=20 (20.0%)\n"
        "  scheduled_tick: turns=1 measured=1 read=90 (90.0%) write=0 (0.0%) uncached=10 (10.0%)\n"
        "  user_message: turns=3 measured=2 read=5 (10.0%) write=10 (20.0%) uncached=35 (70.0%)\n"
        "Session kind:\n"
        "  channel: turns=3 measured=2 read=5 (10.0%) write=10 (20.0%) uncached=35 (70.0%)\n"
        "  poller: turns=2 measured=1 read=60 (60.0%) write=20 (20.0%) uncached=20 (20.0%)\n"
        "  scheduler: turns=1 measured=1 read=90 (90.0%) write=0 (0.0%) uncached=10 (10.0%)\n"
        "Skipped malformed/undated lines: 1\n"
        "Turns without complete cache usage: 2\n"
    )
    assert before == {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}


def test_window_and_missing_config(tmp_path: Path) -> None:
    shutil.copytree(HOME / "logs", tmp_path / "logs")
    assert not (tmp_path / ".env").exists()
    result = run_report(tmp_path, "--from-date", "2026-05-26", "--to-date", "2026-05-26")
    assert result.returncode == 0
    assert "Configured agent model (current home/.env): unknown (no MIMIR_MODEL_SPEC in home/.env)\n" in result.stdout
    assert "Total: turns=1 measured=1 read=0 (0.0%) write=0 (0.0%) uncached=500 (100.0%)\n" in result.stdout


def test_empty_home_and_invalid_window(tmp_path: Path) -> None:
    result = run_report(tmp_path, "--from-date", "2026-05-23", "--to-date", "2026-05-25")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"cache report: no turn log at {tmp_path / 'logs' / 'turns.jsonl'}\n"
    result = run_report(HOME, "--from-date", "2026-05-26", "--to-date", "2026-05-23")
    assert result.returncode == 2
    assert "--from-date must not be after --to-date" in result.stderr


def test_partial_usage_and_missing_config_are_explicit(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "turns.jsonl").write_text(
        '{"ts":"2026-05-23T00:00:00Z","trigger":"poller","session_id":"poller:a",'
        '"usage":{"input_tokens":100,"cache_read_input_tokens":50}}\n', encoding="utf-8",
    )
    result = run_report(tmp_path, "--from-date", "2026-05-23", "--to-date", "2026-05-23")
    assert result.returncode == 0
    assert result.stdout == (
        "Cache-hit report (UTC 2026-05-23 through 2026-05-23)\n"
        "Configured agent model (current home/.env): unknown (no MIMIR_MODEL_SPEC in home/.env)\n"
        "Per-turn model attribution unavailable in turns.jsonl.\n"
        "Input token semantics: inclusive (selected for the entire window).\n"
        "Shares use read + write + uncached input tokens from measured turns only.\n"
        "Total: turns=1 measured=0 read=0 (n/a) write=0 (n/a) uncached=0 (n/a)\n"
        "Day:\n"
        "  2026-05-23: turns=1 measured=0 read=0 (n/a) write=0 (n/a) uncached=0 (n/a)\n"
        "Trigger:\n"
        "  poller: turns=1 measured=0 read=0 (n/a) write=0 (n/a) uncached=0 (n/a)\n"
        "Session kind:\n"
        "  poller: turns=1 measured=0 read=0 (n/a) write=0 (n/a) uncached=0 (n/a)\n"
        "Skipped malformed/undated lines: 0\n"
        "Turns without complete cache usage: 1\n"
    )


@pytest.mark.parametrize("exclusive", [False, True])
def test_report_consumes_real_logger_usage(tmp_path: Path, exclusive: bool) -> None:
    from langchain_core.messages import AIMessage
    from mimir.turn_logger import derive_result_fields

    if exclusive:
        # The logger's Claude Code fallback copies raw SDK usage unchanged.
        message = AIMessage(content="done", response_metadata={"usage": {
            "input_tokens": 20, "output_tokens": 3,
            "cache_read_input_tokens": 60, "cache_creation_input_tokens": 20,
        }})
    else:
        message = AIMessage(content="done", usage_metadata={
            "input_tokens": 100, "output_tokens": 3, "total_tokens": 103,
            "input_token_details": {"cache_read": 60, "cache_creation": 20},
        })
    usage = derive_result_fields([message])["usage"]
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "turns.jsonl").write_text(json.dumps({
        "ts": "2026-05-23T00:00:00Z", "trigger": "poller",
        "session_id": "poller:a", "usage": usage,
    }) + "\n", encoding="utf-8")
    options = ("--input-semantics", "exclusive") if exclusive else ()
    result = run_report(tmp_path, "--from-date", "2026-05-23", "--to-date", "2026-05-23", *options)
    assert result.returncode == 0, result.stderr
    assert "Total: turns=1 measured=1 read=60 (60.0%) write=20 (20.0%) uncached=20 (20.0%)\n" in result.stdout
    assert "Input token semantics: " + ("exclusive" if exclusive else "inclusive") in result.stdout


@pytest.mark.parametrize("prompt,reads,writes,semantics,expected", [
    (100, 60, 40, "inclusive", "read=60 (60.0%) write=40 (40.0%) uncached=0 (0.0%)"),
    (0, 0, 0, "inclusive", "read=0 (n/a) write=0 (n/a) uncached=0 (n/a)"),
    (100, 60, 20, "exclusive", "read=60 (33.3%) write=20 (11.1%) uncached=100 (55.6%)"),
])
def test_zero_uncached_zero_total_and_explicit_exclusive(
    tmp_path: Path, prompt: int, reads: int, writes: int, semantics: str, expected: str,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "turns.jsonl").write_text(json.dumps({
        "ts": "2026-05-23T00:00:00Z",
        "usage": {"input_tokens": prompt, "cache_read_input_tokens": reads,
                  "cache_creation_input_tokens": writes},
    }) + "\n", encoding="utf-8")
    result = run_report(tmp_path, "--from-date", "2026-05-23", "--to-date", "2026-05-23",
                        "--input-semantics", semantics)
    assert result.returncode == 0, result.stderr
    assert "Total: turns=1 measured=1 " + expected + "\n" in result.stdout
    assert "Turns excluded" not in result.stdout


@pytest.mark.parametrize("reads,writes", [(101, 0), (0, 101), (60, 41)])
def test_inconsistent_cache_details_are_flagged_and_excluded(tmp_path: Path, reads: int, writes: int) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    def turn(read: int, write: int) -> str:
        return json.dumps({
            "ts": "2026-05-23T00:00:00Z", "trigger": "poller", "session_id": "poller:a",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": write},
        }) + "\n"
    # Include a valid measured turn: invalid details must not taint its totals.
    (logs / "turns.jsonl").write_text(turn(reads, writes) + turn(60, 20), encoding="utf-8")
    result = run_report(tmp_path, "--from-date", "2026-05-23", "--to-date", "2026-05-23")
    assert result.returncode == 0, result.stderr
    expected = "turns=2 measured=1 read=60 (60.0%) write=20 (20.0%) uncached=20 (20.0%)\n"
    assert "Total: " + expected in result.stdout
    for label in ("2026-05-23", "poller"):
        assert "  " + label + ": " + expected in result.stdout
    assert "Turns excluded because cache details exceed total input: 1\n" in result.stdout
    assert "Turns without complete cache usage:" not in result.stdout


def test_negative_usage_cannot_inflate_or_reverse_shares(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "turns.jsonl").write_text(
        '{"ts":"2026-05-23T00:00:00Z","trigger":"poller","session_id":"poller:a",'
        '"usage":{"input_tokens":100,"cache_read_input_tokens":-50,'
        '"cache_creation_input_tokens":0}}\n', encoding="utf-8",
    )
    result = run_report(tmp_path, "--from-date", "2026-05-23", "--to-date", "2026-05-23")
    assert result.returncode == 0
    assert "Total: turns=1 measured=0 read=0 (n/a) write=0 (n/a) uncached=0 (n/a)\n" in result.stdout
