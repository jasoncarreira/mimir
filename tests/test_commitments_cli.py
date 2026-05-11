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


def test_add_with_recipient(home: Path, capsys: pytest.CaptureFixture):
    """--recipient persists to the record and shows in `list`."""
    rc = _run(
        ["commitments", "add",
         "--channel", "chan-1",
         "--recipient", "alice",
         "--text", "Send the deploy summary"],
        home,
    )
    assert rc == 0
    capsys.readouterr()

    events = _read_events(home)
    assert events[0]["record"]["recipient_identity"] == "alice"

    _run(["commitments", "list"], home)
    out = capsys.readouterr().out
    assert "@alice" in out


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


def test_trim_default_is_dry_run(home: Path, capsys: pytest.CaptureFixture):
    """PR #120 review #4b: trim defaults to dry-run; never modifies the
    store without --apply."""
    rc = _run(["commitments", "trim"], home)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(dry-run)" in out
    assert "Nothing to trim" in out


def test_trim_apply_writes(home: Path, capsys: pytest.CaptureFixture):
    """``--apply`` actually rewrites; the dry-run path doesn't."""
    rc = _run(["commitments", "trim", "--apply"], home)
    assert rc == 0
    assert "trimmed 0" in capsys.readouterr().out


def test_trim_dry_run_lists_candidates(
    home: Path, capsys: pytest.CaptureFixture,
):
    """When there's something to drop, dry-run prints the id list."""
    # Add + complete a commitment, then backdate its terminal event so
    # trim sees it as old. Re-using the manipulation pattern from
    # test_commitments_store.test_trim_drops_terminal_records_*.
    import json as _json
    import time as _time
    _run(["commitments", "add", "--channel", "c1", "--text", "old"], home)
    events = _read_events(home)
    cid = events[0]["id"]
    _run(["commitments", "complete", cid], home)
    capsys.readouterr()

    path = _commitments_file(home)
    now = _time.time()
    lines = []
    for line in path.read_text().splitlines():
        d = _json.loads(line)
        if d.get("type") == "commitment_completed" and d.get("id") == cid:
            d["at_unix"] = now - 40 * 86400  # 40 days old
        lines.append(_json.dumps(d))
    path.write_text("\n".join(lines) + "\n")

    rc = _run(["commitments", "trim"], home)
    assert rc == 0
    out = capsys.readouterr().out
    assert "would drop 1 terminal records" in out
    assert cid in out
    assert "--apply" in out  # instruction to actually apply
    # File NOT modified.
    assert cid in path.read_text()


def test_add_with_phase2_forward_compat_flags(
    home: Path, capsys: pytest.CaptureFixture,
):
    """PR #120 review #5: --dedupe-key / --source-turn-id /
    --saga-session-id round-trip through the JSONL store for
    operator backfill from a failed extraction."""
    rc = _run(
        ["commitments", "add",
         "--channel", "c1",
         "--text", "X",
         "--dedupe-key", "manual-dk-12",
         "--source-turn-id", "t-abc",
         "--saga-session-id", "s-xyz"],
        home,
    )
    assert rc == 0
    events = _read_events(home)
    rec = events[0]["record"]
    assert rec["dedupe_key"] == "manual-dk-12"
    assert rec["source_turn_id"] == "t-abc"
    assert rec["saga_session_id"] == "s-xyz"


def test_snooze_for_days_relative(
    home: Path, capsys: pytest.CaptureFixture,
):
    """PR #120 review nit: --for-days as ergonomic alternative to
    --until-iso."""
    import time as _time
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    capsys.readouterr()
    events = _read_events(home)
    cid = events[0]["id"]
    rc = _run(
        ["commitments", "snooze", cid, "--for-days", "7"],
        home,
    )
    assert rc == 0
    events = _read_events(home)
    snoozed = [e for e in events if e["type"] == "commitment_snoozed"]
    assert len(snoozed) == 1
    # until_unix ≈ now + 7 days; check within 60s tolerance.
    expected = _time.time() + 7 * 86400
    assert abs(snoozed[0]["until_unix"] - expected) < 60


def test_snooze_until_iso_and_for_days_are_mutex(home: Path):
    """argparse mutex group must reject both flags together."""
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    events = _read_events(home)
    cid = events[0]["id"]
    rc = _run(
        ["commitments", "snooze", cid,
         "--for-days", "7",
         "--until-iso", "2026-06-01T00:00:00Z"],
        home,
    )
    assert rc != 0  # argparse exits non-zero on mutex violation


def test_snooze_requires_at_least_one_target(home: Path):
    """argparse mutex group with required=True must reject neither."""
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    events = _read_events(home)
    cid = events[0]["id"]
    rc = _run(["commitments", "snooze", cid], home)
    assert rc != 0


def test_complete_already_completed_errors_cleanly(
    home: Path, capsys: pytest.CaptureFixture,
):
    """PR #120 re-review N2: completing an already-completed record
    surfaces a clear error rather than silently succeeding (which it
    used to do — the no-op event would land in JSONL and the CLI
    would print 'completed' regardless)."""
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    events = _read_events(home)
    cid = events[0]["id"]
    _run(["commitments", "complete", cid], home)
    capsys.readouterr()  # discard
    # Second complete on the now-terminal record must error.
    rc = _run(["commitments", "complete", cid], home)
    assert rc == 2
    err = capsys.readouterr().err
    assert "already completed" in err
    assert f"cannot complete {cid}" in err
    # JSONL still has only one commitment_completed event.
    events_after = _read_events(home)
    completed = [e for e in events_after if e["type"] == "commitment_completed"]
    assert len(completed) == 1


def test_dismiss_already_dismissed_errors_cleanly(
    home: Path, capsys: pytest.CaptureFixture,
):
    """Same shape as complete: dismiss-after-dismiss is an error."""
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    events = _read_events(home)
    cid = events[0]["id"]
    _run(["commitments", "dismiss", cid], home)
    capsys.readouterr()
    rc = _run(["commitments", "dismiss", cid], home)
    assert rc == 2
    err = capsys.readouterr().err
    assert "already dismissed" in err


def test_snooze_after_dismissed_errors_cleanly(
    home: Path, capsys: pytest.CaptureFixture,
):
    """Cross-verb: snoozing a dismissed record is an error too."""
    _run(["commitments", "add", "--channel", "c1", "--text", "X"], home)
    events = _read_events(home)
    cid = events[0]["id"]
    _run(["commitments", "dismiss", cid], home)
    capsys.readouterr()
    rc = _run(
        ["commitments", "snooze", cid, "--for-days", "7"], home,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "already dismissed" in err
