"""The default suite must not inherit host coding executables."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.isolation_fixtures import _coding_cli_path, _HOST_CODING_CLIS


@pytest.mark.parametrize("name", (
    "opencode", "codex", "factory.js", "feature-factory", "chainlink", "gh",
))
def test_host_coding_cli_is_not_on_path(name: str) -> None:
    assert shutil.which(name) is None


def test_real_opencode_admission_requires_explicit_executable() -> None:
    pytest.importorskip("mimir_cultivate")
    from mimir_cultivate.opencode import opencode_available

    assert not opencode_available()


@pytest.mark.skipif(os.name != "posix", reason="POSIX uid traversal contract")
@pytest.mark.parametrize("umask", (0o022, 0o077))
def test_mirrored_tools_survive_private_pytest_and_tmpdir_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask: int,
) -> None:
    source = tmp_path / "bin"
    source.mkdir()
    for name in (*_HOST_CODING_CLIS, "git"):
        executable = source / name
        executable.write_text("#!/bin/sh\necho fixture-git\n", encoding="utf-8")
        executable.chmod(0o755)
    private_temp = tmp_path / "private-temp"
    private_temp.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(private_temp))
    monkeypatch.setattr(tempfile, "tempdir", str(private_temp))
    original_umask = os.umask(umask)
    try:
        with _coding_cli_path(str(source)) as sanitized:
            mirror = Path(sanitized)
            assert mirror.parent.parent == Path("/tmp")
            for directory in (mirror, mirror.parent):
                assert stat.S_IMODE(directory.stat().st_mode) == 0o755
            for name in _HOST_CODING_CLIS:
                assert shutil.which(name, path=sanitized) is None
            assert (mirror / "git").resolve() == source / "git"
            result = subprocess.run(
                ["git"], env={"PATH": sanitized}, capture_output=True, text=True,
                check=True,
            )
            assert result.stdout.strip() == "fixture-git"
        assert not mirror.parent.exists()
    finally:
        os.umask(original_umask)


def test_mirrored_git_executes_after_dropping_uid() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux root to execute under a distinct uid")
    import pwd

    try:
        worker = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("requires an unprivileged nobody account")
    assert worker.pw_uid != 0
    git = shutil.which("git")
    assert git is not None
    with tempfile.TemporaryDirectory(prefix="mimir-cli-source-", dir="/tmp") as temporary:
        source = Path(temporary)
        source.chmod(0o755)
        (source / "git").symlink_to(Path(git).resolve())
        coding_cli = source / "gh"
        coding_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        coding_cli.chmod(0o755)
        with _coding_cli_path(str(source)) as sanitized:
            result = subprocess.run(
                ["git", "--version"], env={"PATH": sanitized}, cwd="/",
                user=worker.pw_uid, group=worker.pw_gid, extra_groups=[],
                capture_output=True, text=True, check=True,
            )
            assert result.stdout.startswith("git version ")
            assert shutil.which("gh", path=sanitized) is None
