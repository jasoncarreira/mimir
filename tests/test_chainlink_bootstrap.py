"""Tests for the best-effort Chainlink store auto-init at startup."""

from __future__ import annotations

import subprocess

import pytest

from mimir import chainlink_bootstrap
from mimir.chainlink_bootstrap import ensure_chainlink_initialized


@pytest.fixture(autouse=True)
def _clear_optout(monkeypatch):
    monkeypatch.delenv("MIMIR_CHAINLINK_AUTOINIT", raising=False)


def _track_run(monkeypatch, *, returncode=0):
    calls: list[tuple] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")

    monkeypatch.setattr(chainlink_bootstrap.subprocess, "run", fake_run)
    return calls


def test_noop_when_home_none(monkeypatch):
    calls = _track_run(monkeypatch)
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: "/usr/bin/chainlink")
    ensure_chainlink_initialized(None)
    assert calls == []


def test_noop_when_store_exists(tmp_path, monkeypatch):
    (tmp_path / ".chainlink").mkdir()
    calls = _track_run(monkeypatch)
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: "/usr/bin/chainlink")
    ensure_chainlink_initialized(tmp_path)
    assert calls == []


def test_noop_when_cli_missing(tmp_path, monkeypatch):
    calls = _track_run(monkeypatch)
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: None)
    ensure_chainlink_initialized(tmp_path)
    assert calls == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_noop_when_disabled(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MIMIR_CHAINLINK_AUTOINIT", value)
    calls = _track_run(monkeypatch)
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: "/usr/bin/chainlink")
    ensure_chainlink_initialized(tmp_path)
    assert calls == []


def test_runs_init_when_store_missing(tmp_path, monkeypatch):
    calls = _track_run(monkeypatch)
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: "/usr/bin/chainlink")
    ensure_chainlink_initialized(tmp_path)
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[0] == "/usr/bin/chainlink"
    assert cmd[1] == "init"
    assert kwargs["cwd"] == str(tmp_path)


def test_never_raises_on_subprocess_error(tmp_path, monkeypatch):
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: "/usr/bin/chainlink")

    def boom(*_a, **_k):
        raise OSError("chainlink blew up")

    monkeypatch.setattr(chainlink_bootstrap.subprocess, "run", boom)
    # Must not raise — a failed init can never take down startup.
    ensure_chainlink_initialized(tmp_path)


def test_nonzero_exit_does_not_raise(tmp_path, monkeypatch):
    _track_run(monkeypatch, returncode=1)
    monkeypatch.setattr(chainlink_bootstrap.shutil, "which", lambda _: "/usr/bin/chainlink")
    ensure_chainlink_initialized(tmp_path)  # logged, not raised
