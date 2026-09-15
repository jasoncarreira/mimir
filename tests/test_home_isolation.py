from __future__ import annotations

import logging
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir import home_isolation


@pytest.fixture
def probe_identity(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwall", lambda: [
        SimpleNamespace(pw_uid=0, pw_gid=0),
        SimpleNamespace(pw_uid=1002, pw_gid=1002),
    ])
    real_stat = os.fstat

    def owned_stat(fd):
        result = real_stat(fd)
        return SimpleNamespace(st_uid=0, st_mode=result.st_mode)

    monkeypatch.setattr(os, "fstat", owned_stat)


@pytest.mark.parametrize("readable,verdict,level", [
    (False, "isolated", logging.INFO),
    (True, "exposed", logging.ERROR),
])
def test_filesystem_verdicts(tmp_path, monkeypatch, caplog, probe_identity,
                             readable, verdict, level):
    def run(argv, **kwargs):
        probe, control = map(Path, argv[-2:])
        assert probe.parent == control.parent == tmp_path
        assert stat.S_IMODE(probe.stat().st_mode) == 0o600
        assert stat.S_IMODE(control.stat().st_mode) == 0o644
        assert probe.read_bytes() == control.read_bytes() == b"mimir-home-isolation-probe"
        assert kwargs["user"] == kwargs["group"] == 1002
        assert kwargs["extra_groups"] == []
        assert kwargs["env"] == {}
        assert kwargs["timeout"] == 5
        assert argv[1:4] == ["-I", "-S", "-c"]
        original_read = Path.read_bytes

        def read(path):
            if path == probe and not readable:
                raise PermissionError("filesystem enforces ownership")
            return original_read(path)

        # Exercise the actual child program with two modeled filesystem policies.
        with monkeypatch.context() as child:
            child.setattr(os, "getuid", lambda: 1002)
            child.setattr(os, "geteuid", lambda: 1002)
            child.setattr(sys, "argv", ["-c", *argv[5:]])
            child.setattr(Path, "read_bytes", read)
            with pytest.raises(SystemExit) as result:
                exec(argv[4], {})
        return subprocess.CompletedProcess(argv, result.value.code)

    monkeypatch.setattr(subprocess, "run", run)
    caplog.set_level(logging.INFO, logger=home_isolation.__name__)
    assert home_isolation.check_home_isolation(tmp_path) == verdict
    assert any(r.levelno == level and f"Home uid isolation: {verdict}" in r.message
               for r in caplog.records)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [
    "unprivileged", "no_uid", "readonly", "ownership", "mode", "launch", "timeout",
    "bad_exit", "cleanup",
])
def test_unavailable_probe_is_unknown(tmp_path, monkeypatch, caplog, probe_identity, failure):
    def unavailable(*args, **kwargs):
        raise PermissionError("unavailable")

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=2))
    if failure == "unprivileged":
        monkeypatch.setattr(os, "geteuid", lambda: 1001)
    elif failure == "no_uid":
        monkeypatch.setattr(pwd, "getpwall", lambda: [])
    elif failure == "readonly":
        monkeypatch.setattr(home_isolation.tempfile, "mkstemp", unavailable)
    elif failure in ("ownership", "mode"):
        monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(
            st_uid=1002 if failure == "ownership" else 0,
            st_mode=0o644 if failure == "mode" else 0o600,
        ))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("invalid probe ran"))
    elif failure == "launch":
        monkeypatch.setattr(subprocess, "run", unavailable)
    elif failure == "timeout":
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("probe", 5)
        monkeypatch.setattr(subprocess, "run", timeout)
    elif failure == "cleanup":
        monkeypatch.setattr(Path, "unlink", unavailable)

    assert home_isolation.check_home_isolation(tmp_path) == "unknown"
    assert "Home uid isolation: unknown" in caplog.text
    assert "Home uid isolation: isolated" not in caplog.text
    if failure != "cleanup":
        assert not list(tmp_path.iterdir())
    else:
        assert "cleanup failed" in caplog.text


@pytest.mark.parametrize("uid,euid,owner,requested", [
    (0, 0, 1001, 0), (1001, 1001, 1001, 1001),
    (1002, 0, 1001, 1002), (0, 1002, 1001, 1002),
])
def test_child_rejects_wrong_identity(monkeypatch, uid, euid, owner, requested):
    monkeypatch.setattr(os, "getuid", lambda: uid)
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    monkeypatch.setattr(sys, "argv", ["-c", str(owner), str(requested), "probe", "control"])
    with pytest.raises(SystemExit) as result:
        exec(home_isolation._READ_PROBE, {})
    assert result.value.code == 2


@pytest.mark.parametrize("control_value", [None, b"wrong marker"])
def test_child_requires_readable_control(tmp_path, monkeypatch, control_value):
    probe, control = tmp_path / "probe", tmp_path / "control"
    probe.write_bytes(b"mimir-home-isolation-probe")
    if control_value is not None:
        control.write_bytes(control_value)
    monkeypatch.setattr(os, "getuid", lambda: 1002)
    monkeypatch.setattr(os, "geteuid", lambda: 1002)
    monkeypatch.setattr(sys, "argv", ["-c", "0", "1002", str(probe), str(control)])
    with pytest.raises((OSError, SystemExit)) as result:
        exec(home_isolation._READ_PROBE, {})
    if isinstance(result.value, SystemExit):
        assert result.value.code == 2


def test_child_unexpected_read_error_is_not_isolated(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.write_bytes(b"mimir-home-isolation-probe")
    monkeypatch.setattr(os, "getuid", lambda: 1002)
    monkeypatch.setattr(os, "geteuid", lambda: 1002)
    monkeypatch.setattr(sys, "argv", ["-c", "0", "1002", str(tmp_path / "missing"), str(control)])
    with pytest.raises(FileNotFoundError):
        exec(home_isolation._READ_PROBE, {})
