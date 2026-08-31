from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.acp import sdk
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


@pytest.mark.parametrize("value", [None, "", "   "])
def test_acp_is_disabled_without_explicit_truthy_value(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("MIMIR_ACP_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MIMIR_ACP_ENABLED", value)
    assert acp_enabled_from_env() is False


def test_acp_typo_is_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMIR_ACP_ENABLED", "flase")
    with pytest.raises(AcpDaemonError, match="not a recognised boolean"):
        acp_enabled_from_env()


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
        self.data = bytearray()
        self.transport = _Transport()

    def get_extra_info(self, name: str) -> object | None:
        return self.sock if name == "socket" else None

    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

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
async def test_single_peer_cap_refuses_second_with_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert ACP_MAX_PEERS == 1
    assert all(not writer.closed for writer in writers[:ACP_MAX_PEERS])
    assert writers[-1].closed
    refusal = json.loads(writers[-1].data)
    assert refusal == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": -32001,
            "data": None,
            "message": (
                "An ACP client is already connected; close it before opening another editor"
            ),
        },
    }
    release.set()
    await asyncio.gather(*(peer.task for peer in tuple(daemon._peers)))
    await asyncio.sleep(0)
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_clean_close_allows_immediate_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())

    async def run_until_eof(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read()
        writer.close()
        await writer.wait_closed()

    monkeypatch.setattr(daemon, "_run_peer", run_until_eof)
    await daemon.start()
    first_reader, first_writer = await asyncio.open_unix_connection(
        str(daemon.socket_path)
    )
    del first_reader
    first_writer.close()
    await first_writer.wait_closed()

    second_reader, second_writer = await asyncio.open_unix_connection(
        str(daemon.socket_path)
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_reader.readline(), 0.05)

    second_writer.close()
    await second_writer.wait_closed()
    await daemon.stop()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_tcp_reset_retires_inflight_peer_and_allows_prompt_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    handler_started = asyncio.Event()
    transport_dead = asyncio.Event()

    class Agent:
        def on_connect(self, peer: object) -> int:
            return 1

        async def authenticate(self, method_id: str, **kwargs: object) -> sdk.AuthenticateResponse:
            return sdk.AuthenticateResponse()

        async def ext_method(self, method: str, params: dict[str, object]) -> object:
            assert method == "hold"
            handler_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A normal handler cancellation may still flush state. A dead
                # transport must be declared first so that work is abandoned.
                await transport_dead.wait()
                raise

        async def on_transport_closed(self, generation: int) -> None:
            transport_dead.set()

    daemon._agent = Agent()
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    server = await asyncio.start_server(daemon._admit_peer, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()

    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(*address)

    first_reader, first_writer = await connect()
    first_writer.write(
        b'{"jsonrpc":"2.0","id":1,"method":"authenticate",'
        b'"params":{"methodId":"mimir-web-key"}}\n'
    )
    await first_writer.drain()
    assert json.loads(await asyncio.wait_for(first_reader.readline(), 0.5))["result"] == {}
    first_writer.write(
        b'{"jsonrpc":"2.0","id":2,"method":"_hold","params":{}}\n'
    )
    await first_writer.drain()
    await asyncio.wait_for(handler_started.wait(), 0.5)
    first_peer_task = next(iter(daemon._peers)).task

    concurrent_reader, concurrent_writer = await connect()
    concurrent_writer.write(
        b'{"jsonrpc":"2.0","id":9,"method":"initialize","params":{}}\n'
    )
    await concurrent_writer.drain()
    refusal = json.loads(await asyncio.wait_for(concurrent_reader.readline(), 1.5))
    assert refusal["error"]["code"] == -32001
    assert refusal["id"] is None
    assert "result" not in refusal

    first_socket = first_writer.get_extra_info("socket")
    first_socket.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    first_writer.transport.abort()

    await asyncio.wait_for(transport_dead.wait(), 0.5)
    _, pending = await asyncio.wait({first_peer_task}, timeout=2.0)
    assert not pending, "reset peer did not retire promptly"

    replacement_reader, replacement_writer = await connect()
    replacement_writer.write(
        b'{"jsonrpc":"2.0","id":3,"method":"authenticate",'
        b'"params":{"methodId":"mimir-web-key"}}\n'
    )
    await replacement_writer.drain()
    replacement = json.loads(
        await asyncio.wait_for(replacement_reader.readline(), 2.0)
    )
    assert replacement == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert transport_dead.is_set()

    replacement_writer.close()
    await replacement_writer.wait_closed()
    concurrent_writer.close()
    await concurrent_writer.wait_closed()
    server.close()
    await server.wait_closed()
    await daemon._stop_peers()
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


def _resistant_close(
    entered: asyncio.Event,
    release: asyncio.Event,
    captured: list[asyncio.Task[None]],
):
    """A ``connection.close()`` that swallows cancellation while it flushes.

    ``release`` is the test's escape hatch: without it the task would outlive
    the test and block event-loop teardown, which is why the existing stubborn
    peers in this module are written the same way. ``captured`` lets the test
    drain the task once released.
    """

    async def close(self: object) -> None:
        task = asyncio.current_task()
        if task is not None:
            captured.append(task)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    return close


class _StubAgent:
    def on_connect(self, peer: object) -> int:
        return 1


def _eof_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    return reader


async def _spawn_stdio_runner(writer: object) -> asyncio.Task[None]:
    import mimir.acp.sdk as sdk

    class _Agent:
        def on_connect(self, peer: object) -> int:
            return 1

    reader = asyncio.StreamReader()
    runner = asyncio.create_task(
        sdk.run_stdio_agent(_Agent(), request_reader=reader, response_writer=writer)
    )
    await asyncio.sleep(0)
    reader.feed_eof()
    return runner


@pytest.mark.asyncio
async def test_close_bound_fits_inside_the_daemon_cancel_budget() -> None:
    """The runner must resolve on its own before the daemon escalates.

    ``run_stdio_agent`` waits for the shielded ``connection.close()`` at most
    twice, so two intervals have to fit inside the window the daemon allows
    before it aborts the transport and gives up. If this relationship inverts a
    retired generation can outlive ``_finish_runner``.
    """
    from mimir.acp.daemon import ACP_PEER_CANCEL_TIMEOUT as daemon_cancel
    from mimir.acp.sdk import ACP_CLOSE_CANCEL_TIMEOUT

    assert 2 * ACP_CLOSE_CANCEL_TIMEOUT < daemon_cancel


@pytest.mark.asyncio
async def test_resistant_close_does_not_pin_a_cancelled_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that ignores cancellation must not keep the runner alive.

    The cancellation handler used to await the shielded close with no deadline,
    so a close stuck flushing pinned the runner task indefinitely. The bound is
    the only thing that can end this wait: the close here swallows every
    cancellation, so without it the runner never completes.
    """
    import mimir.acp.sdk as sdk

    entered, release = asyncio.Event(), asyncio.Event()
    captured: list[asyncio.Task[None]] = []
    monkeypatch.setattr(
        sdk.Connection, "close", _resistant_close(entered, release, captured)
    )
    monkeypatch.setattr("mimir.acp.sdk.ACP_CLOSE_CANCEL_TIMEOUT", 0.01)

    runner = await _spawn_stdio_runner(_Writer())
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        runner.cancel()
        # asyncio.wait rather than wait_for: it neither re-cancels nor raises on
        # timeout, so an unbounded close shows up as a pending task and a clean
        # assertion failure instead of hanging the suite. run_stdio_agent's
        # outer ``except BaseException`` absorbs a second cancel, so wait_for
        # could not force the runner down anyway.
        _, pending = await asyncio.wait({runner}, timeout=0.5)
        assert not pending, "cancelled runner did not resolve within the bound"
    finally:
        release.set()
        await asyncio.wait({runner, *captured}, timeout=2.0)


@pytest.mark.asyncio
async def test_resistant_close_still_lets_the_daemon_retire_the_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_finish_runner`` must retire such a peer without giving up.

    The daemon releases its single admission slot from the runner task's done
    callback, so a runner that never completes leaves the generation unretired
    while ``_finish_runner`` has already stopped waiting for it — the window in
    which a replacement peer could be admitted alongside it. A self-bounding
    runner satisfies the daemon's cancel contract instead of raising.
    """
    import mimir.acp.sdk as sdk

    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    entered, release = asyncio.Event(), asyncio.Event()
    captured: list[asyncio.Task[None]] = []
    monkeypatch.setattr(
        sdk.Connection, "close", _resistant_close(entered, release, captured)
    )
    monkeypatch.setattr("mimir.acp.sdk.ACP_CLOSE_CANCEL_TIMEOUT", 0.01)

    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_CANCEL_TIMEOUT", 0.2)
    monkeypatch.setattr("mimir.acp.daemon.ACP_PEER_ABORT_TIMEOUT", 0.2)

    writer = _Writer()
    runner = await _spawn_stdio_runner(writer)
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        # No AcpDaemonError: a self-bounding runner satisfies the cancel
        # contract. An unbounded one makes _finish_runner abort and then raise.
        finish = asyncio.create_task(daemon._finish_runner(runner, writer))
        _, pending = await asyncio.wait({finish}, timeout=2.0)
        assert not pending, "_finish_runner did not settle"
        assert finish.exception() is None, f"daemon gave up: {finish.exception()!r}"
        assert runner.done()
    finally:
        release.set()
        await asyncio.wait({runner, *captured}, timeout=2.0)
        shutil.rmtree(home)


@pytest.mark.asyncio
async def test_second_peer_is_refused_while_an_old_close_is_still_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admission fence must outlast the runner, not just the wait.

    Bounding ``run_stdio_agent``'s wait for the shielded close lets the runner
    return while that close is still live -- and the runner's done callback is
    what decrements ``_admitted``. So the bound alone would *free* the slot for
    a replacement peer while old-generation work continues, which is the
    overlap ``ACP_MAX_PEERS = 1`` exists to prevent.

    Drives a real second admission, which is the only way to show the fence
    holds; asserting on counters alone would not.
    """
    import mimir.acp.sdk as sdk

    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    daemon._uid = os.getuid()

    entered, release = asyncio.Event(), asyncio.Event()
    captured: list[asyncio.Task[None]] = []
    monkeypatch.setattr(
        sdk.Connection, "close", _resistant_close(entered, release, captured)
    )
    monkeypatch.setattr("mimir.acp.sdk.ACP_CLOSE_CANCEL_TIMEOUT", 0.01)

    first_writer = _Writer()
    runner = asyncio.create_task(
        sdk.run_stdio_agent(
            _StubAgent(),
            request_reader=_eof_reader(),
            response_writer=first_writer,
            on_close_abandoned=daemon._record_abandoned_close,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        runner.cancel()
        _, pending = await asyncio.wait({runner}, timeout=0.5)
        assert not pending, "runner did not resolve within the bound"

        # The close is still live, so the generation has not retired.
        assert daemon._unretired_generations() == 1

        second_writer = _Writer()
        await daemon._admit_peer(asyncio.StreamReader(), second_writer)

        assert daemon._admitted == 0, "a replacement peer was admitted"
        assert not daemon._peers
        assert b"error" in bytes(second_writer.data), (
            "the second peer was neither admitted nor refused"
        )
        assert second_writer.closed
    finally:
        release.set()
        await asyncio.wait({runner, *captured}, timeout=2.0)
        shutil.rmtree(home)


@pytest.mark.asyncio
async def test_capacity_returns_once_the_old_close_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that does finish must not fence admission forever.

    The fence prunes completed tasks, so a slow-but-terminating close costs a
    refusal window rather than bricking admission until the process restarts.
    """
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    release = asyncio.Event()

    async def eventually() -> None:
        await release.wait()

    task = asyncio.create_task(eventually())
    daemon._record_abandoned_close(task)
    assert daemon._unretired_generations() == 1

    release.set()
    await task
    assert daemon._unretired_generations() == 0
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_an_abandoned_close_that_fails_keeps_admission_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing is not retiring: a late failure must not return capacity.

    An abandoned close can complete *with an exception* -- cleanup failing
    after the runner already returned. Releasing capacity on ``done()`` alone
    would admit a replacement on a teardown that did not succeed, and would
    also swallow the failure, since nothing retrieves the task's result.
    """
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    daemon._uid = os.getuid()
    release = asyncio.Event()

    async def fails_late() -> None:
        await release.wait()
        raise RuntimeError("teardown did not complete")

    task = asyncio.create_task(fails_late())
    daemon._record_abandoned_close(task)
    assert daemon._unretired_generations() == 1

    release.set()
    await asyncio.wait({task}, timeout=1.0)
    assert task.done()

    # Still fenced: the close finished, but not cleanly.
    assert daemon._unretired_generations() == 1
    assert daemon._failed_retirements == 1

    writer = _Writer()
    await daemon._admit_peer(asyncio.StreamReader(), writer)
    assert daemon._admitted == 0, "a replacement was admitted after failed retirement"
    assert not daemon._peers
    assert b"error" in bytes(writer.data)

    # The failure was consumed, so it cannot resurface as a lost-exception warning.
    assert isinstance(task.exception(), RuntimeError)
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_an_abandoned_close_that_honours_cancellation_returns_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that stops on cancel has retired: nothing is left running."""
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    started = asyncio.Event()

    async def stops_on_cancel() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(stops_on_cancel())
    await asyncio.wait_for(started.wait(), 1.0)
    daemon._record_abandoned_close(task)
    assert daemon._unretired_generations() == 1

    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
    assert task.cancelled()

    assert daemon._unretired_generations() == 0
    assert daemon._failed_retirements == 0
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_close_that_raises_on_forced_cancel_still_fences_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that fails *while being cancelled* must not free the slot.

    ``run_stdio_agent`` cancels the shielded close after the first bound, then
    waits again. If the close answers that cancel by raising, the task is
    ``done()`` — but teardown failed. Reporting only not-done tasks would let
    the runner return, free the single admission slot, admit a replacement
    after a failed teardown, and leave the exception unretrieved.

    Drives the real runner and then a real ``_admit_peer``, because the whole
    point is what the daemon does with the handoff.
    """
    import mimir.acp.sdk as sdk

    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    daemon._uid = os.getuid()

    entered = asyncio.Event()

    async def raises_on_cancel(self: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Answer the forced cancel by failing, not by stopping.
            raise RuntimeError("cleanup failed during cancellation") from None

    monkeypatch.setattr(sdk.Connection, "close", raises_on_cancel)
    monkeypatch.setattr("mimir.acp.sdk.ACP_CLOSE_CANCEL_TIMEOUT", 0.01)

    runner = asyncio.create_task(
        sdk.run_stdio_agent(
            _StubAgent(),
            request_reader=_eof_reader(),
            response_writer=_Writer(),
            on_close_abandoned=daemon._record_abandoned_close,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        runner.cancel()
        _, pending = await asyncio.wait({runner}, timeout=0.5)
        assert not pending, "runner did not resolve within the bound"

        # The failed teardown must have been routed into the accounting.
        assert daemon._unretired_generations() >= 1
        assert daemon._failed_retirements == 1

        writer = _Writer()
        await daemon._admit_peer(asyncio.StreamReader(), writer)
        assert daemon._admitted == 0, "replacement admitted after failed teardown"
        assert not daemon._peers
        assert b"error" in bytes(writer.data)
    finally:
        await asyncio.wait({runner}, timeout=1.0)
        shutil.rmtree(home)


@pytest.mark.asyncio
async def test_close_that_raises_in_the_first_grace_interval_fences_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close can fail before any forced cancellation, and must still fence.

    The retirement decision used to live inside the ``if unfinished`` branch, so
    a close that raised during the FIRST bounded wait left that branch unentered
    and was never reported: the runner returned, the single admission slot was
    freed, and a replacement could enter after failed teardown.
    """
    import mimir.acp.sdk as sdk

    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    daemon._uid = os.getuid()
    entered = asyncio.Event()

    async def fails_during_grace(self: object) -> None:
        entered.set()
        await asyncio.sleep(0)          # let the runner reach the shield
        raise RuntimeError("cleanup failed before any cancel")

    monkeypatch.setattr(sdk.Connection, "close", fails_during_grace)
    # Generous, so the close finishes INSIDE the first interval rather than
    # being cancelled: this is the path that skipped the report.
    monkeypatch.setattr("mimir.acp.sdk.ACP_CLOSE_CANCEL_TIMEOUT", 1.0)

    runner = asyncio.create_task(
        sdk.run_stdio_agent(
            _StubAgent(),
            request_reader=_eof_reader(),
            response_writer=_Writer(),
            on_close_abandoned=daemon._record_abandoned_close,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        runner.cancel()
        _, pending = await asyncio.wait({runner}, timeout=2.0)
        assert not pending, "runner did not resolve"

        assert daemon._unretired_generations() >= 1
        assert daemon._failed_retirements == 1

        writer = _Writer()
        await daemon._admit_peer(asyncio.StreamReader(), writer)
        assert daemon._admitted == 0, "replacement admitted after failed teardown"
        assert b"error" in bytes(writer.data)
    finally:
        await asyncio.wait({runner}, timeout=1.0)
        shutil.rmtree(home)


@pytest.mark.asyncio
async def test_close_failure_surfaced_by_the_shield_fences_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close that simply fails -- no cancellation anywhere -- must fence too.

    Here the shield surfaces the failure directly and it becomes
    ``close_failure``. That was never registered as a failed retirement, so the
    generation looked retired while teardown had not succeeded.
    """
    import mimir.acp.sdk as sdk

    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    monkeypatch.setattr("mimir.acp.daemon._peer_uid", lambda sock: os.getuid())
    daemon._uid = os.getuid()

    async def fails_immediately(self: object) -> None:
        raise RuntimeError("close failed outright")

    monkeypatch.setattr(sdk.Connection, "close", fails_immediately)

    # No runner.cancel() at all: the close just fails. run_stdio_agent
    # propagates that failure, which it should -- the fence has to be raised
    # regardless, and that is what this asserts.
    with pytest.raises(RuntimeError, match="close failed outright"):
        await asyncio.wait_for(
            sdk.run_stdio_agent(
                _StubAgent(),
                request_reader=_eof_reader(),
                response_writer=_Writer(),
                on_close_abandoned=daemon._record_abandoned_close,
            ),
            2.0,
        )

    assert daemon._unretired_generations() >= 1
    assert daemon._failed_retirements == 1

    writer = _Writer()
    await daemon._admit_peer(asyncio.StreamReader(), writer)
    assert daemon._admitted == 0, "replacement admitted after failed teardown"
    assert b"error" in bytes(writer.data)
    shutil.rmtree(home)
