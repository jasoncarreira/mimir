from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from mimir.acp.profiles import Profile, RemoteProfile
from mimir.acp.ssh import SshError, _establish, build_ssh_argv, child_environment, stop_child


def remote_profile(tmp_path: Path) -> tuple[Profile, Path]:
    ssh = tmp_path / "ssh"
    ssh.write_text("")
    ssh.chmod(0o755)
    identity = tmp_path / "id"
    identity.write_text("")
    identity.chmod(0o600)
    known = tmp_path / "known"
    known.write_text("")
    known.chmod(0o600)
    profile = Profile("p", Path("/remote path"), RemoteProfile("example.com", "user", 2222, identity, known))
    return profile, ssh


def test_exact_argv_is_injection_safe_and_secret_free(tmp_path: Path) -> None:
    profile, ssh = remote_profile(tmp_path)
    argv = build_ssh_argv(profile, ssh)
    assert argv == (
        str(ssh), "-T", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no", "-o", "ChallengeResponseAuthentication=no",
        "-o", "IdentitiesOnly=yes", "-o", "ClearAllForwardings=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "-o", "LogLevel=ERROR",
        "-o", f"UserKnownHostsFile={profile.remote.known_hosts_file}", "-i", str(profile.remote.identity_file),
        "-p", "2222", "--", "user@example.com", "mimir-agent acp relay --home '/remote path'",
    )
    assert "SECRET" not in str(argv)


def test_ssh_file_allowlist_and_argument_bounds(tmp_path: Path) -> None:
    profile, ssh = remote_profile(tmp_path)
    profile.remote.identity_file.chmod(0o640)
    with pytest.raises(SshError, match="unsafe SSH file"):
        build_ssh_argv(profile, ssh)
    profile.remote.identity_file.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(profile.remote.known_hosts_file)
    unsafe = Profile("p", profile.home, RemoteProfile("example.com", "user", 22, profile.remote.identity_file, link))
    with pytest.raises(SshError, match="unsafe SSH file"):
        build_ssh_argv(unsafe, ssh)


def test_child_environment_is_secret_and_profile_free() -> None:
    assert child_environment({"PATH": "/bin", "SECRET": "public", "PYTHONPATH": "x", "PYTHONHOME": "y", "MIMIR_ACP_PROFILE": "x", "MIMIR_KEY": "raw"}) == {"PATH": "/bin", "SECRET": "public"}


class Reader:
    def __init__(self, value: bytes | None) -> None: self.value = value
    async def read(self, size: int = -1) -> bytes:
        if self.value is None:
            await asyncio.Future()
        value, self.value = self.value, b""
        return value


class Process:
    def __init__(self, stdout: Reader, waits: list[object]) -> None:
        self.stdout = stdout
        self.stdin = None
        self.returncode = None
        self.waits = iter(waits)
        self.terminated = 0
        self.killed = 0
    async def wait(self) -> int:
        value = next(self.waits)
        if isinstance(value, BaseException): raise value
        if value == "block": await asyncio.Future()
        self.returncode = int(value)
        return self.returncode
    def terminate(self) -> None: self.terminated += 1
    def kill(self) -> None: self.killed += 1


@pytest.mark.asyncio
async def test_establishment_is_genuinely_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.ssh.CONNECT_TIMEOUT", 0.01)
    with pytest.raises(SshError, match="timed out"):
        await _establish(Process(Reader(None), ["block"]))
    assert await _establish(Process(Reader(b"{"), ["block"])) == b"{"


@pytest.mark.asyncio
async def test_stop_child_waits_then_terminates_and_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.ssh.WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.TERMINATE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.KILL_TIMEOUT", 0.01)
    process = Process(Reader(b""), ["block", "block", 0])
    await stop_child(process)
    assert process.terminated == 1
    assert process.killed == 1


@pytest.mark.asyncio
async def test_stop_child_reports_finite_kill_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.ssh.WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.TERMINATE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.KILL_TIMEOUT", 0.01)
    process = Process(Reader(b""), ["block", "block", "block"])
    with pytest.raises(SshError, match="did not stop"):
        await stop_child(process)
    assert process.terminated == process.killed == 1
