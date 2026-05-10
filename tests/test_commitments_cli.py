"""``mimir commitments`` CLI smoke tests — wire-level integration.

We invoke the CLI through ``mimir.cli.main()`` so the argparse routing
gets exercised end-to-end. Heavy lifting lives in
``test_commitments_store.py``; this file pins the dispatch + flag
shape.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mimir.cli import main as cli_main


def _run(args: list[str], home: Path) -> int:
    """Invoke the CLI via SystemExit-catching. Returns exit code."""
    os.environ["MIMIR_HOME"] = str(home)
    try:
        cli_main(args)
        return 0
    except SystemExit as exc:
        code = exc.code
        return int(code) if code is not None else 0


def _commitments_file(home: Path) -> Path:
    return home / ".mimir" / "commitments.jsonl"


def _read_events(home: Path) -> list[dict]:
    p = _commitments_file(home)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean MIMIR_HOME with just enough for Config.from_env."""
    (tmp_path / "logs").mkdir()
    (tmp_path / ".mimir").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    return tmp_path


def test_add_then_list(home: Path, capsys: pytest.CaptureFixture):
    """`add` creates a record; `list` prints it."""
    rc = _run(
        ["commitments", "add",
         "--channel", "chan-1",
         "--text", "Review PR #111",
         "--kind", "agent_promise"],
        home,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "added c-" in out
    events = _read_events(home)
    assert len(events) == 1
    assert events[0]["type"] == "commitment_added"
    rec = events[0]["record"]
    assert rec["text"] == "Review PR #111"
    assert rec["kind"] == "agent_promise"

    # list shows it
    rc = _run(["commitments", "list"], home)
    out = capsys.readouterr().out
    assert "Review PR #111" in out
    assert "pending" in out
    assert "chan-1" in out


def test_complete_marks_terminal(home: Path, capsys: pytest.CaptureFixture):
    _run(["commitments", "add",
          "--channel", "c1", "--text", "X"], home)
    capsys.readouterr()  # discard add output
    # Extract the id from the file.
    events = _read_events(home)
    cid = events[0]["id"]

    rc = _run(["commitments", "complete", cid], home)
    assert rc == 0
    assert f"completed {cid}" in capsys.readouterr().out

    rc = _run(["commitments", "list", "--status", "completed"], home)
    out = capsys.readouterr().out
    assert cid in out


def test_list_empty_when_no_commitments(home: Path, capsys: pytest.CaptureFixture):
    rc = _run(["commitments", "list"], home)
    assert rc == 0
    assert "(no commitments match)" in capsys.readouterr().out


def test_complete_unknown_id_errors(home: Path, capsys: pytest.CaptureFixture):
    rc = _run(["commitments", "complete", "c-nonexistent"], home)
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_snooze_records_until(home: Path, capsys: pytest.CaptureFixture):
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    capsys.readouterr()
    events = _read_events(home)
    cid = events[0]["id"]

    rc = _run(
        ["commitments", "snooze", cid,
         "--until-iso", "2026-06-15T00:00:00Z",
         "--reason", "next sprint"],
        home,
    )
    assert rc == 0
    assert "snoozed" in capsys.readouterr().out

    events = _read_events(home)
    snoozed = [e for e in events if e["type"] == "commitment_snoozed"]
    assert len(snoozed) == 1
    assert snoozed[0]["reason"] == "next sprint"


def test_dismiss_with_reason(home: Path, capsys: pytest.CaptureFixture):
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    capsys.readouterr()
    events = _read_events(home)
    cid = events[0]["id"]

    rc = _run(
        ["commitments", "dismiss", cid, "--reason", "no longer needed"],
        home,
    )
    assert rc == 0
    events = _read_events(home)
    dismissed = [e for e in events if e["type"] == "commitment_dismissed"]
    assert dismissed[0]["reason"] == "no longer needed"


def test_add_with_due_window(home: Path, capsys: pytest.CaptureFixture):
    """--due-iso default-extends end by 7 days when --due-end-iso omitted."""
    rc = _run(
        ["commitments", "add",
         "--channel", "c1", "--text", "X",
         "--due-iso", "2026-05-15T10:00:00Z"],
        home,
    )
    assert rc == 0
    events = _read_events(home)
    rec = events[0]["record"]
    assert rec["due_window_start_unix"] is not None
    assert rec["due_window_end_unix"] is not None
    # End should be ~7 days later.
    diff_days = (rec["due_window_end_unix"] - rec["due_window_start_unix"]) / 86400
    assert 6.9 < diff_days < 7.1


def test_trim_subcommand_runs(home: Path, capsys: pytest.CaptureFixture):
    rc = _run(["commitments", "trim"], home)
    assert rc == 0
    assert "trimmed 0" in capsys.readouterr().out
