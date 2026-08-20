"""Tests for ``cap_check.py`` — the archive/ledger cap cross-check.

Deliberately minimal. These cover the incident this script exists to prevent
and the shape of its output; they are not an exhaustive audit of every way a
ledger or archive could be malformed.

The incident: a stray root-level ``count.py`` was removed from an install, so
``cap_check``'s delegate lookup failed. The failure surfaced as ``ledger=0``
with exit 0, meaning the guard silently became archive-only and reported the
full daily cap as available.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def fresh_cap_check():
    sys.modules.pop("cap_check", None)
    return importlib.import_module("cap_check")


def _fake_run(*, returncode=0, stdout="", raises=None):
    def run(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=returncode,
            stdout=stdout, stderr="boom",
        )
    return run


def _write_archive(home: Path, name: str, doc) -> Path:
    d = home / "state" / "pollers" / "social-cli-bsky" / "outbox_archive"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


_TODAY = "2026-08-20T10-00-00-000Z_outbox-bsky.yaml"


# --- the delegate ---------------------------------------------------------


def test_count_delegate_sits_beside_this_script() -> None:
    """The lookup that broke. ``count.py`` must be resolvable from here."""
    module = fresh_cap_check()
    beside = Path(module.__file__).resolve().parent / "count.py"
    assert beside.is_file(), f"count.py must sit beside cap_check.py, looked at {beside}"


def test_a_missing_delegate_is_unavailable_not_zero(monkeypatch, tmp_path) -> None:
    """The incident: a failed read must not be reported as a count."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(raises=FileNotFoundError("gone")))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_a_nonzero_exit_is_unavailable(monkeypatch, tmp_path) -> None:
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_non_integer_output_is_unavailable(monkeypatch, tmp_path) -> None:
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="Traceback"))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_a_genuine_zero_is_still_zero(monkeypatch, tmp_path) -> None:
    """The fix must not turn a real quiet day into `unavailable`."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="0\n"))
    assert module.count_ledger(tmp_path, "2026-08-20") == 0


def test_the_delegate_count_is_returned(monkeypatch, tmp_path) -> None:
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="3\n"))
    assert module.count_ledger(tmp_path, "2026-08-20") == 3


# --- what the agent reads -------------------------------------------------


def _patch(monkeypatch, module, *, archive=0, unreadable=0, ledger=0):
    monkeypatch.setattr(module, "count_archive", lambda home, today: (archive, unreadable))
    monkeypatch.setattr(module, "count_ledger", lambda home, today: ledger)
    monkeypatch.setattr(module, "_home", lambda: Path("/nonexistent"))


def test_main_fails_closed_when_the_ledger_is_unavailable(monkeypatch, capsys) -> None:
    """No number, and a nonzero exit — an outage must not read as headroom."""
    module = fresh_cap_check()
    _patch(monkeypatch, module, archive=0, ledger=None)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.LEDGER_UNAVAILABLE
    assert "effective=unknown" in out
    assert "effective=0" not in out


def test_main_reports_the_max_as_effective(monkeypatch, capsys) -> None:
    module = fresh_cap_check()
    _patch(monkeypatch, module, archive=1, ledger=2)
    assert module.main() == 0
    assert "archive=1 ledger=2 effective=2" in capsys.readouterr().out


def test_main_flags_divergence_over_one(monkeypatch, capsys) -> None:
    module = fresh_cap_check()
    _patch(monkeypatch, module, archive=4, ledger=1)
    assert module.main() == 2


# --- archive counting -----------------------------------------------------


def test_archive_counts_post_class_actions(tmp_path) -> None:
    module = fresh_cap_check()
    _write_archive(tmp_path, _TODAY, {"dispatch": [{"post": {"text": "a"}}, {"reply": {"text": "b"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (2, 0)


def test_archive_excludes_dry_runs_and_non_post_actions(tmp_path) -> None:
    module = fresh_cap_check()
    _write_archive(tmp_path, _TODAY, {"dispatch": [
        {"post": {"text": "a", "dryRun": True}},
        {"like": {"uri": "x"}},
        {"post": {"text": "b"}},
    ]})
    assert module.count_archive(tmp_path, "2026-08-20") == (1, 0)


def test_archive_ignores_other_days(tmp_path) -> None:
    module = fresh_cap_check()
    _write_archive(tmp_path, "2026-08-19T10-00-00-000Z_outbox-bsky.yaml",
                   {"dispatch": [{"post": {"text": "yesterday"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 0)


def test_main_fails_closed_when_an_archive_file_is_unreadable(monkeypatch, capsys) -> None:
    """The sibling of the ledger case: a file we could not read is not a zero.

    The archive exists precisely because a ledger write can lag or be skipped,
    so a readable ledger does not stand in for an archive that would not parse.
    """
    module = fresh_cap_check()
    _patch(monkeypatch, module, archive=0, unreadable=1, ledger=0)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.ARCHIVE_UNREADABLE
    assert "effective=unknown" in out
    assert "effective=0" not in out
    assert "archive_unreadable=1" in out


def test_an_unparseable_archive_file_is_reported(tmp_path) -> None:
    module = fresh_cap_check()
    d = tmp_path / "state" / "pollers" / "social-cli-bsky" / "outbox_archive"
    d.mkdir(parents=True)
    (d / _TODAY).write_text("{[ not: valid: yaml", encoding="utf-8")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 1)
