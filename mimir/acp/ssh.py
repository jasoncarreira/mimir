from __future__ import annotations

import asyncio
import os
import shlex
import stat
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .credentials import CredentialError, NativeCredentialStore
from .profiles import Profile, ProfileError, ProfileStore, RemoteProfile, selected_profile
from .proxy import FrameWriter, open_stdio
from .transport import FORCE_CLOSE_TIMEOUT, close_writer, pump_stream

SSH_PATH = Path("/usr/bin/ssh")
CONNECT_TIMEOUT = 12.0
WAIT_TIMEOUT = 1.0
TERMINATE_TIMEOUT = 2.0
KILL_TIMEOUT = 1.0

class SshError(RuntimeError):
    pass


def _safe_file(path: Path, *, executable_root: bool = False, strict: bool = False) -> None:
    try: value = path.lstat()
    except OSError as exc: raise SshError("unsafe SSH file") from exc
    owner = 0 if executable_root else os.getuid()
    forbidden = 0o022 if not strict else 0o077
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_uid != owner or value.st_mode & forbidden:
        raise SshError("unsafe SSH file")
    if executable_root and not value.st_mode & 0o111: raise SshError("unsafe SSH executable")


def build_ssh_argv(profile: Profile, ssh_path: Path | None = None) -> tuple[str, ...]:
    ssh_path = SSH_PATH if ssh_path is None else ssh_path
    remote = profile.remote
    if remote is None: raise SshError("remote profile required")
    _safe_file(ssh_path, executable_root=ssh_path == SSH_PATH)
    _safe_file(remote.identity_file, strict=True)
    _safe_file(remote.known_hosts_file)
    command = shlex.join(["mimir-agent", "acp", "relay", "--home", str(profile.home)])
    argv = (
        str(ssh_path), "-T", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no", "-o", "ChallengeResponseAuthentication=no",
        "-o", "IdentitiesOnly=yes", "-o", "ClearAllForwardings=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "-o", "LogLevel=ERROR",
        "-o", f"UserKnownHostsFile={remote.known_hosts_file}", "-i", str(remote.identity_file),
        "-p", str(remote.port), "--", f"{remote.user}@{remote.host}", command,
    )
    encoded = [part.encode() for part in argv]
    if any(len(part) > 4096 or b"\x00" in part or b"\n" in part or b"\r" in part for part in encoded) or sum(map(len, encoded)) + len(encoded) > 32768:
        raise SshError("SSH arguments are invalid")
    return argv


def child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if environ is None else environ
    return {key: value for key, value in values.items() if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("MIMIR_")}

async def _discard(reader: asyncio.StreamReader) -> None:
    while await reader.read(65536): pass

async def stop_child(process: Any) -> None:
    if process.stdin is not None and not process.stdin.is_closing():
        try:
            await asyncio.wait_for(close_writer(process.stdin), FORCE_CLOSE_TIMEOUT)
        except TimeoutError:
            pass
    if process.returncode is not None:
        await process.wait(); return
    try:
        await asyncio.wait_for(process.wait(), WAIT_TIMEOUT); return
    except TimeoutError: pass
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT); return
    except TimeoutError: pass
    process.kill()
    try: await asyncio.wait_for(process.wait(), KILL_TIMEOUT)
    except TimeoutError as exc: raise SshError("SSH child did not stop") from exc

async def run_ssh_proxy(
    profile: Profile,
    credential: str,
    output: BinaryIO,
    *,
    _ssh_path: Path | None = None,
    _environment: Mapping[str, str] | None = None,
) -> None:
    process = await asyncio.wait_for(asyncio.create_subprocess_exec(
        *build_ssh_argv(profile, _ssh_path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=child_environment(_environment)), CONNECT_TIMEOUT)
    if process.stdin is None or process.stdout is None or process.stderr is None:
        await stop_child(process)
        raise SshError("SSH pipes unavailable")
    try:
        reader, writer, input_transport = await open_stdio(output)
    except BaseException:
        await stop_child(process)
        raise
    discard = asyncio.create_task(_discard(process.stderr))
    upstream = asyncio.create_task(pump_stream(reader, FrameWriter(process.stdin, credential)))
    downstream = asyncio.create_task(pump_stream(process.stdout, writer))
    try:
        await asyncio.gather(upstream, downstream)
        try:
            code = await asyncio.wait_for(process.wait(), WAIT_TIMEOUT)
        except TimeoutError as exc:
            raise SshError("SSH child did not exit") from exc
        if code:
            raise SshError("SSH connection failed")
    finally:
        for task in (upstream, downstream, discard):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (upstream, downstream, discard) if task is not None), return_exceptions=True)
        input_transport.close()
        closing = asyncio.gather(close_writer(writer), close_writer(process.stdin), return_exceptions=True)
        try:
            await asyncio.wait_for(closing, FORCE_CLOSE_TIMEOUT)
        except TimeoutError:
            pass
        await stop_child(process)

async def run_remote_proxy(profile_name: str | None, output: BinaryIO, *, profiles: ProfileStore | None = None, credentials: NativeCredentialStore | None = None) -> None:
    name = selected_profile(profile_name); profile = (profiles or ProfileStore()).get(name)
    if profile is None: raise ProfileError("profile-not-found")
    if profile.remote is None: raise SshError("remote profile required")
    credential = (credentials or NativeCredentialStore()).get(name)
    if credential is None: raise CredentialError("credential-read-failed")
    await run_ssh_proxy(profile, credential, output)
