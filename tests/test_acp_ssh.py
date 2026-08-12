from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

import pytest

from mimir.acp.profiles import Profile, RemoteProfile
from mimir.acp.ssh import SshError, build_ssh_argv, child_environment, run_ssh_proxy, stop_child


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
    async def read(self, size: int = -1) -> bytes:
        return b""


class Process:
    def __init__(self, waits: list[object]) -> None:
        self.stdout = Reader()
        self.stdin = None
        self.returncode = None
        self.waits = iter(waits)
        self.terminated = 0
        self.killed = 0

    async def wait(self) -> int:
        value = next(self.waits)
        if value == "block":
            await asyncio.Future()
        self.returncode = int(value)
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1


@pytest.mark.asyncio
async def test_stop_child_waits_then_terminates_and_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.ssh.WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.TERMINATE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.KILL_TIMEOUT", 0.01)
    process = Process(["block", "block", 0])
    await stop_child(process)
    assert process.terminated == 1
    assert process.killed == 1


@pytest.mark.asyncio
async def test_stop_child_reports_finite_kill_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.ssh.WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.TERMINATE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.ssh.KILL_TIMEOUT", 0.01)
    process = Process(["block", "block", "block"])
    with pytest.raises(SshError, match="did not stop"):
        await stop_child(process)
    assert process.terminated == process.killed == 1


class Output:
    def __init__(self, stream: io.BytesIO) -> None:
        self.stream = stream
        self.closed = False

    def write(self, data: bytes) -> None:
        self.stream.write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    async def wait_closed(self) -> None:
        return None


def _fake_ssh(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "ssh"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o755)
    return executable


@pytest.mark.asyncio
async def test_actual_subprocess_pumps_without_first_output_timeout_and_sanitizes_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile, _ = remote_profile(tmp_path)
    observed = tmp_path / "observed.json"
    ssh = _fake_ssh(tmp_path, """
import json,os,sys,time
with open(os.environ['OBSERVED'],'w') as stream: json.dump({'argv':sys.argv,'env':dict(os.environ)},stream)
time.sleep(.08)
for line in sys.stdin.buffer:
 message=json.loads(line); assert message['params']['_meta']=={'mimir.webKey':'sentinel'}
 sys.stdout.buffer.write(b'{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\\n'); sys.stdout.buffer.flush()
""")
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"authenticate","params":{"methodId":"mimir-web-key"}}\n')
    reader.feed_eof()
    output = io.BytesIO()
    transport = type("Transport", (), {"close": lambda self: None})()
    monkeypatch.setattr("mimir.acp.ssh.open_stdio", lambda target: asyncio.sleep(0, result=(reader, Output(target), transport)))
    monkeypatch.setattr("mimir.acp.ssh.CONNECT_TIMEOUT", 0.02)
    environment = {"PATH": os.environ.get("PATH", ""), "OBSERVED": str(observed), "PYTHONPATH": "bad", "MIMIR_ACP_PROFILE": "remote"}
    await run_ssh_proxy(profile, "sentinel", output, _ssh_path=ssh, _environment=environment)
    assert json.loads(output.getvalue())["result"] == {}
    captured = json.loads(observed.read_text())
    assert captured["argv"][-1] == "mimir-agent acp relay --home '/remote path'"
    assert "PYTHONPATH" not in captured["env"] and "MIMIR_ACP_PROFILE" not in captured["env"]
    assert "sentinel" not in json.dumps(captured)


@pytest.mark.asyncio
async def test_actual_subprocess_failure_is_sanitized_and_reaped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile, _ = remote_profile(tmp_path)
    ssh = _fake_ssh(tmp_path, "import sys; sys.stderr.write('private-sentinel'); raise SystemExit(23)\n")
    reader = asyncio.StreamReader()
    reader.feed_eof()
    output = io.BytesIO()
    transport = type("Transport", (), {"close": lambda self: None})()
    monkeypatch.setattr("mimir.acp.ssh.open_stdio", lambda target: asyncio.sleep(0, result=(reader, Output(target), transport)))
    with pytest.raises(SshError, match="SSH connection failed") as raised:
        await run_ssh_proxy(profile, "raw-secret", output, _ssh_path=ssh, _environment={"PATH": os.environ.get("PATH", "")})
    assert "private-sentinel" not in str(raised.value)


@pytest.mark.asyncio
async def test_actual_subprocess_cancellation_terminates_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile, _ = remote_profile(tmp_path)
    marker = tmp_path / "terminated"
    ssh = _fake_ssh(tmp_path, """
import os,signal,sys,time
def stop(*args):
 open(os.environ['MARKER'],'w').write('terminated'); raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
for line in sys.stdin.buffer: time.sleep(10)
""")
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"authenticate","params":{"methodId":"mimir-web-key"}}\n')
    output = io.BytesIO()
    transport = type("Transport", (), {"close": lambda self: None})()
    monkeypatch.setattr("mimir.acp.ssh.open_stdio", lambda target: asyncio.sleep(0, result=(reader, Output(target), transport)))
    task = asyncio.create_task(run_ssh_proxy(profile, "secret", output, _ssh_path=ssh, _environment={"PATH": os.environ.get("PATH", ""), "MARKER": str(marker)}))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert marker.read_text() == "terminated"
