from __future__ import annotations

import io
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


@pytest.fixture(autouse=True)
def mount_table(monkeypatch):
    """Own the mount metadata; tests must not depend on the runner filesystem."""
    real_open = Path.open
    metadata = {"text": "1 0 0:1 / / rw - btrfs /dev/test rw\n"}

    def open_path(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            if isinstance(metadata["text"], Exception):
                raise metadata["text"]
            return io.StringIO(metadata["text"])
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    return metadata


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


@pytest.mark.parametrize("filesystem,verdict,level", [
    ("virtiofs", "suspected-exposed", logging.ERROR),
    ("btrfs", "unknown", logging.WARNING),
    ("ext4", "unknown", logging.WARNING),
])
def test_unprivileged_mount_fallback(tmp_path, monkeypatch, caplog, mount_table,
                                    filesystem, verdict, level):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not spawn"))
    mount_table["text"] = f"1 0 0:1 / / rw - {filesystem} source rw\n"
    assert home_isolation.check_home_isolation(tmp_path) == verdict
    assert any(r.levelno == level and f"Home uid isolation: {verdict}" in r.message
               for r in caplog.records)
    assert "A non-owning uid read" not in caplog.text
    if filesystem == "virtiofs":
        assert "Mount-type inference only" in caplog.text
        assert "cannot switch" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("metadata", ["", "broken\n", "1 - virtiofs\n",
                                       FileNotFoundError("no proc"),
                                       PermissionError("unreadable")])
def test_missing_mount_evidence_stays_unknown(tmp_path, monkeypatch, caplog,
                                              mount_table, metadata):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    mount_table["text"] = metadata
    assert home_isolation.check_home_isolation(tmp_path) == "unknown"
    assert "suspected-exposed" not in caplog.text


def test_mount_lookup_uses_resolved_longest_component_match(tmp_path, mount_table):
    home = tmp_path / "home space\\name"
    nested = home / "nested"
    nested.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)

    def escape(path):
        return str(path).replace("\\", r"\134").replace(" ", r"\040")

    mount_table["text"] = (
        "1 0 0:1 / / rw - btrfs source rw\n"
        f"2 1 0:2 / {escape(home)} rw - virtiofs source rw\n"
        f"3 2 0:3 / {escape(nested)} rw - ext4 source rw\n"
        f"4 1 0:4 / {escape(tmp_path)}/hom rw - virtiofs source rw\n"
    )
    assert home_isolation._home_filesystem(home) == "virtiofs"
    assert home_isolation._home_filesystem(alias) == "ext4"
    other = tmp_path / "home-other"
    other.mkdir()
    assert home_isolation._home_filesystem(other) == "btrfs"
    # Stacked mountpoints are deliberately not guessed from record ordering.
    mount_table["text"] += f"5 2 0:5 / {escape(home)} rw - btrfs source rw\n"
    assert home_isolation._home_filesystem(home) is None


@pytest.mark.parametrize("exit_code,verdict", [(10, "isolated"), (11, "exposed"),
                                                (2, "suspected-exposed")])
def test_read_probe_takes_precedence_over_mount_inference(
    tmp_path, monkeypatch, probe_identity, mount_table, exit_code, verdict,
):
    mount_table["text"] = "1 0 0:1 / / rw - virtiofs source rw\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=exit_code))
    assert home_isolation.check_home_isolation(tmp_path) == verdict
    assert not list(tmp_path.iterdir())
