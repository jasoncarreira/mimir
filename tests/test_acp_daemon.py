from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.acp.daemon import (
    ACP_MAX_PEERS,
    AcpDaemon,
    AcpDaemonError,
    _Peer,
    _peer_uid,
    acp_enabled_from_env,
)


class _Channels:
    def register(self, bridge: object) -> None:
        pass


def _short_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="mimir-acp-", dir="/tmp"))


def _bundle(home: Path) -> SimpleNamespace:
    resolver = SimpleNamespace(_yaml_path=home / "state" / "identities.yaml")
    core = SimpleNamespace(identity_resolver=resolver)
    adapters = SimpleNamespace(channels=_Channels())
    return SimpleNamespace(
        config=SimpleNamespace(home=home, acp_journal_ttl_days=7),
        core=core,
        adapters=adapters,
    )


def test_enabled_false_values_skip_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.daemon.peer_credentials_supported", lambda: False)
    for value in ("0", "FALSE", " No ", "off", "N"):
        monkeypatch.setenv("MIMIR_ACP_ENABLED", value)
        assert acp_enabled_from_env() is False


def test_explicit_enable_fails_without_peer_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.daemon.peer_credentials_supported", lambda: False)
    monkeypatch.setenv("MIMIR_ACP_ENABLED", "true")
    with pytest.raises(AcpDaemonError, match="unsupported"):
        acp_enabled_from_env()


@pytest.mark.asyncio
async def test_daemon_creates_owner_only_socket_and_removes_it(tmp_path: Path) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    await daemon.start()
    assert stat.S_IMODE(daemon.directory.stat().st_mode) == 0o700
    socket_stat = daemon.socket_path.lstat()
    assert stat.S_ISSOCK(socket_stat.st_mode)
    assert stat.S_IMODE(socket_stat.st_mode) == 0o600
    assert socket_stat.st_uid == os.getuid()
    await daemon.stop()
    assert not daemon.socket_path.exists()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_daemon_rejects_symlink_directory(tmp_path: Path) -> None:
    home = _short_home()
    target = home / "target"
    target.mkdir()
    (home / ".mimir").mkdir()
    (home / ".mimir" / "acp").symlink_to(target, target_is_directory=True)
    daemon = AcpDaemon(_bundle(home))
    with pytest.raises(AcpDaemonError, match="symlink"):
        await daemon.start()


@pytest.mark.asyncio
async def test_live_socket_is_not_unlinked(tmp_path: Path) -> None:
    home = _short_home()
    first = AcpDaemon(_bundle(home))
    await first.start()
    identity = first.socket_path.lstat().st_ino
    second = AcpDaemon(_bundle(home))
    with pytest.raises(AcpDaemonError, match="already listening"):
        await second.start()
    assert first.socket_path.lstat().st_ino == identity
    await first.stop()
    shutil.rmtree(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["mode", "owner", "type"])
async def test_daemon_rejects_unsafe_directory(
    unsafe: str,
) -> None:
    home = _short_home()
    parent = home / ".mimir"
    parent.mkdir()
    directory = parent / "acp"
    if unsafe == "type":
        directory.write_text("not a directory")
    else:
        directory.mkdir(mode=0o700)
        if unsafe == "mode":
            directory.chmod(0o755)
    daemon = AcpDaemon(_bundle(home))
    if unsafe == "owner":
        daemon._uid = os.getuid() + 1
    with pytest.raises(AcpDaemonError, match="unsafe file type|owned|mode"):
        await daemon.start()
    shutil.rmtree(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["mode", "type", "symlink"])
async def test_daemon_rejects_unsafe_existing_socket(unsafe: str) -> None:
    home = _short_home()
    directory = home / ".mimir" / "acp"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "daemon.sock"
    if unsafe == "type":
        path.write_text("not a socket")
    elif unsafe == "symlink":
        target = home / "target"
        target.write_text("target")
        path.symlink_to(target)
    else:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        sock.close()
        path.chmod(0o666)
    daemon = AcpDaemon(_bundle(home))
    with pytest.raises(AcpDaemonError, match="unsafe file type|symlink|mode"):
        await daemon.start()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_stale_socket_is_revalidated_and_replaced() -> None:
    home = _short_home()
    directory = home / ".mimir" / "acp"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "daemon.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    path.chmod(0o600)
    daemon = AcpDaemon(_bundle(home))
    await daemon.start()
    reader, writer = await asyncio.open_unix_connection(str(path))
    del reader
    writer.close()
    await writer.wait_closed()
    await daemon.stop()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_stop_does_not_unlink_successor_socket() -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    await daemon.start()
    original = daemon.socket_path.with_name("original.sock")
    daemon.socket_path.rename(original)
    successor = socket.socket(socket.AF_UNIX)
    successor.bind(str(daemon.socket_path))
    daemon.socket_path.chmod(0o600)
    successor_inode = daemon.socket_path.lstat().st_ino
    await daemon.stop()
    assert daemon.socket_path.lstat().st_ino == successor_inode
    successor.close()
    daemon.socket_path.unlink()
    original.unlink()
    shutil.rmtree(home)


class _Transport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class _Writer:
    def __init__(self, sock: object | None = object()) -> None:
        self.sock = sock
        self.closed = False
        self.transport = _Transport()

    def get_extra_info(self, name: str) -> object | None:
        return self.sock if name == "socket" else None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_peer_uid_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid() + 1)
    writer = _Writer()
    await daemon._admit_peer(asyncio.StreamReader(), writer)
    assert writer.closed
    assert daemon._admitted == 0
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_peer_cap_admits_exactly_eight(monkeypatch: pytest.MonkeyPatch) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    daemon._agent = object()
    release = asyncio.Event()

    async def hold(reader: object, writer: object) -> None:
        await release.wait()

    monkeypatch.setattr(daemon, "_run_peer", hold)
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    writers = [_Writer() for _ in range(ACP_MAX_PEERS + 1)]
    for writer in writers:
        await daemon._admit_peer(asyncio.StreamReader(), writer)
    assert daemon._admitted == ACP_MAX_PEERS
    assert all(not writer.closed for writer in writers[:ACP_MAX_PEERS])
    assert writers[-1].closed
    release.set()
    await asyncio.gather(*(peer.task for peer in tuple(daemon._peers)))
    await asyncio.sleep(0)
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_startup_failure_rolls_back_owned_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("listener failure")

    monkeypatch.setattr(asyncio, "start_unix_server", fail)
    with pytest.raises(RuntimeError, match="listener failure"):
        await daemon.start()
    assert not daemon.socket_path.exists()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_preauth_timeout_cancels_connection_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    daemon._agent = object()
    cancelled = asyncio.Event()

    async def runner(*args: object, **kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("mimir.acp.daemon.run_stdio_agent", runner)
    monkeypatch.setattr("mimir.acp.daemon.ACP_AUTH_TIMEOUT", 0.01)
    writer = _Writer()
    with pytest.raises(AcpDaemonError, match="authentication timed out"):
        await daemon._run_peer(asyncio.StreamReader(), writer)
    assert cancelled.is_set()
    assert writer.closed
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_shutdown_closes_at_most_four_peers_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    active = 0
    maximum = 0
    release = asyncio.Event()

    class SlowWriter(_Writer):
        async def wait_closed(self) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await release.wait()
            finally:
                active -= 1

    tasks = [asyncio.create_task(asyncio.sleep(0)) for _ in range(8)]
    peers = [_Peer(SlowWriter(), task) for task in tasks]
    daemon._peers.update(peers)
    stopping = asyncio.create_task(daemon._stop_peers())
    await asyncio.sleep(0.01)
    assert maximum == 4
    release.set()
    await stopping
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_shutdown_aborts_after_cancel_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(stubborn())
    writer = _Writer()
    peer = _Peer(writer, task)
    daemon._peers.add(peer)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_GRACE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_CANCEL_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_ABORT_TIMEOUT", 0.01)
    with pytest.raises(AcpDaemonError, match="transport abort"):
        await daemon._stop_peers()
    assert writer.transport.aborted
    release.set()
    await task
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_total_shutdown_deadline_never_awaits_stuck_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    release = asyncio.Event()

    async def stuck() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    peer_task = asyncio.create_task(stuck())
    daemon._peers.add(_Peer(_Writer(), peer_task))
    monkeypatch.setattr("mimir.acp.daemon.ACP_SHUTDOWN_TIMEOUT", 0.01)
    with pytest.raises(AcpDaemonError, match="total deadline"):
        await asyncio.wait_for(daemon.stop(), 0.1)
    release.set()
    await peer_task
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_authenticated_connection_outlives_preauth_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    release = asyncio.Event()

    class Agent:
        def on_connect(self, peer: object) -> int:
            return 1

        async def authenticate(self, *args: object, **kwargs: object) -> object:
            return object()

    async def runner(agent: object, **kwargs: object) -> None:
        await agent.authenticate("mimir-web-key")
        await release.wait()

    daemon = AcpDaemon(_bundle(home))
    daemon._agent = Agent()
    monkeypatch.setattr("mimir.acp.daemon.run_stdio_agent", runner)
    monkeypatch.setattr("mimir.acp.daemon.ACP_AUTH_TIMEOUT", 0.01)
    writer = _Writer()
    task = asyncio.create_task(daemon._run_peer(asyncio.StreamReader(), writer))
    await asyncio.sleep(0.03)
    assert not task.done()
    release.set()
    await task
    assert writer.closed
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_postauth_watchdog_terminates_non_draining_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    authenticated = asyncio.Event()
    cancelled = asyncio.Event()

    class Agent:
        def on_connect(self, peer: object) -> int:
            return 1

        async def authenticate(self, *args: object, **kwargs: object) -> object:
            authenticated.set()
            return object()

    async def runner(agent: object, **kwargs: object) -> None:
        try:
            await agent.authenticate("mimir-web-key")
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class BlockedWriter(_Writer):
        async def drain(self) -> None:
            await asyncio.Event().wait()

    daemon = AcpDaemon(_bundle(home))
    daemon._agent = Agent()
    monkeypatch.setattr("mimir.acp.daemon.run_stdio_agent", runner)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_WATCHDOG_INTERVAL", 0.0)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_DRAIN_TIMEOUT", 0.01)
    writer = BlockedWriter()
    with pytest.raises(AcpDaemonError, match="stopped draining"):
        await asyncio.wait_for(
            daemon._run_peer(asyncio.StreamReader(), writer), 0.1
        )
    assert authenticated.is_set()
    assert cancelled.is_set()
    assert writer.closed
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_transport_death_retires_only_that_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()

    class Agent:
        def on_connect(self, peer: object) -> int:
            return 1

    async def runner(*args: object, **kwargs: object) -> None:
        return None

    daemon = AcpDaemon(_bundle(home))
    daemon._agent = Agent()
    monkeypatch.setattr("mimir.acp.daemon.run_stdio_agent", runner)
    writer = _Writer()
    await daemon._run_peer(asyncio.StreamReader(), writer)
    assert writer.closed
    assert not writer.transport.aborted
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_existing_socket_owner_is_verified() -> None:
    home = _short_home()
    directory = home / ".mimir" / "acp"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "daemon.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    path.chmod(0o600)
    daemon = AcpDaemon(_bundle(home))
    daemon._uid = os.getuid() + 1
    with pytest.raises(AcpDaemonError, match="not owned"):
        await daemon._remove_stale_socket()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_shutdown_grace_allows_peer_completion_without_cancellation() -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    closed = asyncio.Event()
    cancelled = False

    class ClosingWriter(_Writer):
        def close(self) -> None:
            super().close()
            closed.set()

    async def peer_runner() -> None:
        nonlocal cancelled
        try:
            await closed.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    task = asyncio.create_task(peer_runner())
    daemon._peers.add(_Peer(ClosingWriter(), task))
    await daemon._stop_peers()
    assert task.done()
    assert not cancelled
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_daemon_rejects_symlink_parent() -> None:
    home = _short_home()
    target = home / "target"
    target.mkdir()
    (home / ".mimir").symlink_to(target, target_is_directory=True)
    daemon = AcpDaemon(_bundle(home))
    with pytest.raises(AcpDaemonError, match="parent directory.*symlink"):
        await daemon.start()
    shutil.rmtree(home)


def test_peer_uid_uses_so_peercred(monkeypatch: pytest.MonkeyPatch) -> None:
    class Sock:
        def getsockopt(self, *args: object) -> bytes:
            return struct.pack("3i", 123, 456, 789)

    monkeypatch.setattr(socket, "SO_PEERCRED", 99, raising=False)
    assert _peer_uid(Sock()) == 456


def test_peer_uid_uses_getpeereid(monkeypatch: pytest.MonkeyPatch) -> None:
    class Sock:
        def getsockopt(self, *args: object) -> bytes:
            raise OSError

        def getpeereid(self) -> tuple[int, int]:
            return (321, 654)

    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    assert _peer_uid(Sock()) == 321


def test_peer_uid_fails_closed_when_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sock:
        def fileno(self) -> int:
            return -1

    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    monkeypatch.setattr("mimir.acp.daemon._libc_getpeereid", lambda: None)
    assert _peer_uid(Sock()) is None


@pytest.mark.asyncio
async def test_preauth_cancellation_resistance_is_post_abort_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    daemon._agent = object()
    aborted = asyncio.Event()
    unrelated_completed = asyncio.Event()

    class AbortTransport(_Transport):
        def abort(self) -> None:
            super().abort()
            aborted.set()

    writer = _Writer()
    writer.transport = AbortTransport()

    async def resistant(*args: object, **kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await aborted.wait()

    async def unrelated_turn() -> None:
        await asyncio.sleep(0)
        unrelated_completed.set()

    monkeypatch.setattr("mimir.acp.daemon.run_stdio_agent", resistant)
    monkeypatch.setattr("mimir.acp.daemon.ACP_AUTH_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_CANCEL_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_ABORT_TIMEOUT", 0.02)
    turn = asyncio.create_task(unrelated_turn())
    with pytest.raises(AcpDaemonError, match="authentication timed out"):
        await asyncio.wait_for(
            daemon._run_peer(asyncio.StreamReader(), writer), 0.1
        )
    await turn
    assert aborted.is_set()
    assert unrelated_completed.is_set()
    assert not daemon._connection_runners
    shutil.rmtree(home)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["failed_authentication", "cancellation", "transport_death"],
)
async def test_connection_failure_leaves_separate_peer_and_runtime_turn_alive(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    survivor_started = asyncio.Event()
    survivor_release = asyncio.Event()
    runtime_completed = asyncio.Event()

    class Agent:
        def on_connect(self, peer: object) -> int:
            return 1

        async def authenticate(self, method_id: str, **kwargs: object) -> object:
            if method_id != "good":
                raise RuntimeError("authentication failed")
            return object()

        async def run_turn(self) -> None:
            await asyncio.sleep(0)
            runtime_completed.set()

    async def runner(agent: object, *, response_writer: object, **kwargs: object) -> None:
        kind = response_writer.kind
        if kind == "survivor":
            await agent.authenticate("good")
            survivor_started.set()
            await survivor_release.wait()
        elif kind == "failed_authentication":
            await agent.authenticate("bad")
        elif kind == "cancellation":
            await asyncio.Event().wait()

    daemon = AcpDaemon(_bundle(home))
    agent = Agent()
    daemon._agent = agent
    monkeypatch.setattr("mimir.acp.daemon.run_stdio_agent", runner)
    survivor_writer = _Writer()
    survivor_writer.kind = "survivor"
    survivor = asyncio.create_task(
        daemon._run_peer(asyncio.StreamReader(), survivor_writer)
    )
    await survivor_started.wait()
    runtime_turn = asyncio.create_task(agent.run_turn())
    failed_writer = _Writer()
    failed_writer.kind = failure
    failed = asyncio.create_task(
        daemon._run_peer(asyncio.StreamReader(), failed_writer)
    )
    if failure == "failed_authentication":
        with pytest.raises(RuntimeError, match="authentication failed"):
            await failed
    elif failure == "cancellation":
        await asyncio.sleep(0)
        failed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await failed
    else:
        await failed
    await runtime_turn
    assert runtime_completed.is_set()
    assert not survivor.done()
    survivor_release.set()
    await survivor
    shutil.rmtree(home)
