"""Tests for the pending-update flag flow (mimir/update_on_start.py).

Covers:
- No flag → no-op (the common path)
- Flag present + install succeeds → exec called with re-exec argv,
  flag deleted, mimir_update_applied event logged
- Flag present + install fails → flag deleted (no loop), exec NOT
  called, mimir_update_failed event logged, function returns
- Flag with target_version → pip spec includes ``==<version>``
- Flag with include_prereleases → pip argv carries ``--pre``
- Malformed JSON in flag → treated as empty defaults, doesn't crash
- Empty file → same (bare ``touch`` works as approval signal)
- write_flag round-trip → the operator-side tool produces a file
  that the startup-side reader parses correctly
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir.update_on_start import (
    PendingUpdate,
    _install_spec,
    _read_flag,
    apply_pending_update,
    flag_path,
    write_flag,
)


# ─── flag_path / write_flag ─────────────────────────────────────────


def test_flag_path_under_dotmimir(tmp_path: Path) -> None:
    """The flag lives under ``<home>/.mimir/`` so it shares the
    home volume's persistence with the saga DB + metrics."""
    p = flag_path(tmp_path)
    assert p == tmp_path / ".mimir" / "pending-update.flag"


def test_write_flag_creates_parent_dir(tmp_path: Path) -> None:
    """A fresh home doesn't have ``.mimir/`` yet; write_flag creates
    it. Otherwise the operator approval would error on a new
    deployment."""
    assert not (tmp_path / ".mimir").exists()
    write_flag(tmp_path)
    assert (tmp_path / ".mimir" / "pending-update.flag").is_file()


def test_write_flag_roundtrip_defaults(tmp_path: Path) -> None:
    """Empty-args write produces a flag the reader parses with
    sensible defaults (target_version='' → latest, no --pre)."""
    write_flag(tmp_path)
    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == ""
    assert parsed.include_prereleases is False
    assert parsed.approved_at is not None


def test_write_flag_roundtrip_pinned_prerelease(tmp_path: Path) -> None:
    """Explicit version + pre-release flag carry through write → read."""
    write_flag(tmp_path, target_version="0.2.0rc1", include_prereleases=True)
    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == "0.2.0rc1"
    assert parsed.include_prereleases is True


def test_read_flag_tolerates_empty_file(tmp_path: Path) -> None:
    """Bare ``touch <flag>`` is a valid operator approval — no JSON
    payload required."""
    path = flag_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    parsed = _read_flag(path)
    assert parsed.target_version == ""
    assert parsed.include_prereleases is False


def test_read_flag_tolerates_malformed_json(tmp_path: Path) -> None:
    """Garbage payload defaults to empty + logs a warning, doesn't
    crash startup."""
    path = flag_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json {")
    parsed = _read_flag(path)
    assert parsed.target_version == ""


# ─── _install_spec ──────────────────────────────────────────────────


def test_install_spec_empty_target_returns_bare_pkg() -> None:
    parsed = PendingUpdate(target_version="", include_prereleases=False, approved_at=None)
    assert _install_spec("mimir-agent", parsed) == "mimir-agent"


def test_install_spec_pinned_target_uses_equality() -> None:
    parsed = PendingUpdate(target_version="0.2.0", include_prereleases=False, approved_at=None)
    assert _install_spec("mimir-agent", parsed) == "mimir-agent==0.2.0"


# ─── apply_pending_update — no flag ─────────────────────────────────


def test_apply_no_flag_returns_false(tmp_path: Path) -> None:
    """Common path: no flag → no-op, returns False. Startup proceeds
    normally."""
    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))
    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    assert result is False
    assert events == []
    assert exec_called == []
    assert not flag_path(tmp_path).exists()


# ─── apply_pending_update — happy path ──────────────────────────────


def test_apply_happy_path_runs_pip_then_execs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag present, pip install succeeds → flag deleted,
    mimir_update_applied logged, exec called with re-exec argv."""
    write_flag(tmp_path, target_version="0.2.0")

    captured_argv: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        captured_argv.append(argv)
        # subprocess.CompletedProcess shape
        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))

    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    # The function attempted an install (returned True even though our
    # stubbed exec doesn't actually replace the process).
    assert result is True

    # pip spec was right — pinned to 0.2.0, no --pre.
    assert captured_argv, "pip install was not invoked"
    argv = captured_argv[0]
    assert argv[:5] == [sys.executable, "-m", "pip", "install", "--upgrade"]
    assert "--pre" not in argv
    assert argv[-1] == "mimir-agent==0.2.0"

    # Events: starting + applied. failed should NOT have been logged.
    kinds = [e[0] for e in events]
    assert "mimir_update_starting" in kinds
    assert "mimir_update_applied" in kinds
    assert "mimir_update_failed" not in kinds

    # Flag was deleted post-install.
    assert not flag_path(tmp_path).exists()

    # Exec was called with the re-exec argv shape: [python, *original argv].
    assert len(exec_called) == 1
    executable, exec_argv = exec_called[0]
    assert executable == sys.executable
    assert exec_argv[0] == sys.executable


# ─── apply_pending_update — install failure ─────────────────────────


def test_apply_install_failure_clears_flag_no_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip install fails → flag deleted (no loop), no exec, function
    returns True (we did attempt) but startup proceeds on OLD code."""
    write_flag(tmp_path)

    def _fake_run(argv, **kwargs):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "ERROR: No matching distribution found for mimir-agent"
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))

    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    assert result is True  # an attempt happened
    assert not flag_path(tmp_path).exists()  # flag cleared, no loop
    assert exec_called == []  # no re-exec on failure

    kinds = [e[0] for e in events]
    assert "mimir_update_failed" in kinds
    assert "mimir_update_applied" not in kinds


def test_apply_install_timeout_clears_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip install hangs past _PIP_TIMEOUT_S → flag deleted, failed
    event logged, no exec."""
    write_flag(tmp_path)

    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=300)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))

    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    assert result is True
    assert not flag_path(tmp_path).exists()
    assert exec_called == []
    assert "mimir_update_failed" in [e[0] for e in events]


# ─── --pre flag propagation ──────────────────────────────────────────


def test_apply_includes_pre_flag_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag with include_prereleases=True passes --pre to pip."""
    write_flag(tmp_path, target_version="0.2.0rc1", include_prereleases=True)

    captured_argv: list[list[str]] = []
    def _fake_run(argv, **kwargs):
        captured_argv.append(argv)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    assert captured_argv
    argv = captured_argv[0]
    assert "--pre" in argv
    assert argv[-1] == "mimir-agent==0.2.0rc1"


# ─── env-var override for package name ───────────────────────────────


def test_apply_honors_pypi_package_name_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MIMIR_PYPI_PACKAGE_NAME env overrides the default ``mimir-agent``
    — for forks / pre-release channels."""
    write_flag(tmp_path)
    monkeypatch.setenv("MIMIR_PYPI_PACKAGE_NAME", "mimir-fork")

    captured_argv: list[list[str]] = []
    def _fake_run(argv, **kwargs):
        captured_argv.append(argv)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    assert captured_argv[0][-1] == "mimir-fork"
