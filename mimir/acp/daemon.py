from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import logging
import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import MimirAcpAgent
from .sdk import connection_busy_error, run_stdio_agent

ACP_AUTH_TIMEOUT = 10.0
ACP_PEER_WATCHDOG_INTERVAL = 5.0
ACP_PEER_DRAIN_TIMEOUT = 30.0
ACP_PEER_GRACE_TIMEOUT = 5.0
ACP_PEER_CANCEL_TIMEOUT = 2.0
ACP_PEER_ABORT_TIMEOUT = 1.0
ACP_PEER_RETIRE_TIMEOUT = 1.0
ACP_SHUTDOWN_TIMEOUT = 20.0
ACP_MAX_PEERS = 1
ACP_CLOSE_CONCURRENCY = 4
_SOCKET_MODE = 0o600
_DIRECTORY_MODE = 0o700
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})
_LOGGER = logging.getLogger(__name__)


class AcpDaemonError(RuntimeError):
    pass


def _libc_getpeereid() -> Any | None:
    try:
        function = ctypes.CDLL(None, use_errno=True).getpeereid
    except (AttributeError, OSError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    function.restype = ctypes.c_int
    return function


def peer_credentials_supported() -> bool:
    return (
        hasattr(socket, "SO_PEERCRED")
        or hasattr(socket.socket, "getpeereid")
        or _libc_getpeereid() is not None
    )


def _acp_enablement_from_env() -> tuple[bool, bool]:
    raw = os.environ.get("MIMIR_ACP_ENABLED")
    if raw is None or not raw.strip():
        return False, False
    normalized = raw.strip().lower()
    if normalized in _FALSE_VALUES:
        return False, False
    if normalized in _TRUE_VALUES:
        supported = os.name == "posix" and peer_credentials_supported()
        if not supported:
            raise AcpDaemonError(
                "MIMIR_ACP_ENABLED is true, but owner-verifiable Unix peers are unsupported"
            )
        return True, True
    raise AcpDaemonError(
        f"MIMIR_ACP_ENABLED={raw!r} is not a recognised boolean"
    )


def acp_enabled_from_env() -> bool:
    enabled, _ = _acp_enablement_from_env()
    return enabled


def _safe_stat(path: Path, *, kind: str, mode: int, uid: int) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise AcpDaemonError(f"cannot inspect ACP {kind}: {exc}") from exc
    if stat.S_ISLNK(value.st_mode):
        raise AcpDaemonError(f"ACP {kind} must not be a symlink")
    expected = stat.S_ISDIR if kind == "directory" else stat.S_ISSOCK
    if not expected(value.st_mode):
        raise AcpDaemonError(f"ACP {kind} has an unsafe file type")
    if value.st_uid != uid:
        raise AcpDaemonError(f"ACP {kind} is not owned by the daemon user")
    if stat.S_IMODE(value.st_mode) != mode:
        raise AcpDaemonError(f"ACP {kind} must have mode {mode:04o}")
    return value


def _peer_uid(sock: Any) -> int | None:
    if hasattr(socket, "SO_PEERCRED"):
        try:
            value = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", value)
            return uid
        except (AttributeError, OSError, struct.error):
            pass
    getpeereid = getattr(sock, "getpeereid", None)
    if getpeereid is not None:
        try:
            uid, _ = getpeereid()
            return int(uid)
        except (OSError, TypeError, ValueError):
            pass
    function = _libc_getpeereid()
    if function is not None:
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        try:
            result = function(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid))
        except (AttributeError, OSError, TypeError, ValueError):
            result = -1
        if result == 0:
            return int(uid.value)
    return None


class _ConnectionAgent:
    def __init__(self, agent: MimirAcpAgent) -> None:
        self._agent = agent
        self.authenticated = asyncio.Event()
        self.peer: Any | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def on_connect(self, peer: Any) -> int:
        self.peer = peer
        return self._agent.on_connect(peer)

    async def authenticate(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._agent.authenticate(*args, **kwargs)
        self.authenticated.set()
        return result


@dataclass(eq=False)
class _Peer:
    writer: asyncio.StreamWriter
    task: asyncio.Task[None]


class AcpDaemon:
    def __init__(self, bundle: Any) -> None:
        self._bundle = bundle
        config = getattr(bundle, "config", None)
        home = getattr(config, "home", None)
        if home is None:
            raise AcpDaemonError("ACP daemon requires the published runtime home")
        self.home = Path(home)
        self.directory = self.home / ".mimir" / "acp"
        self.socket_path = self.directory / "daemon.sock"
        self._uid = os.getuid() if hasattr(os, "getuid") else -1
        self._agent: MimirAcpAgent | None = None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._peers: set[_Peer] = set()
        self._connection_runners: set[asyncio.Task[None]] = set()
        self._runner_writers: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._admitted = 0
        self._abandoned_closes: set[asyncio.Task[None]] = set()
        self._failed_retirements = 0
        self._stopping = False

    async def start(self) -> None:
        if self._server is not None:
            return
        if os.name != "posix" or not peer_credentials_supported():
            raise AcpDaemonError("ACP daemon requires POSIX Unix peer credentials")
        self._prepare_directory()
        await self._remove_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            listener.setblocking(False)
            bound = self.socket_path.lstat()
            self._socket_identity = (bound.st_dev, bound.st_ino)
            os.chmod(self.socket_path, _SOCKET_MODE, follow_symlinks=False)
            value = _safe_stat(
                self.socket_path, kind="socket", mode=_SOCKET_MODE, uid=self._uid
            )
            self._socket_identity = (value.st_dev, value.st_ino)
            self._agent = MimirAcpAgent(self._bundle)
            server = await asyncio.start_unix_server(
                self._admit_peer, sock=listener, backlog=8
            )
        except BaseException:
            listener.close()
            self._unlink_owned_socket(self._socket_identity)
            self._socket_identity = None
            self._agent = None
            raise
        self._server = server

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        server, self._server = self._server, None

        async def shutdown() -> None:
            if server is not None:
                server.close()
                await server.wait_closed()
            await self._stop_peers()

        shutdown_task = asyncio.create_task(shutdown())
        try:
            done, _ = await asyncio.wait(
                {shutdown_task}, timeout=ACP_SHUTDOWN_TIMEOUT
            )
            if not done:
                shutdown_task.cancel()
                self._abort_peers(tuple(self._peers))
                raise AcpDaemonError("ACP peer shutdown exceeded its total deadline")
            await shutdown_task
        finally:
            self._unlink_owned_socket(self._socket_identity)
            self._socket_identity = None
            self._stopping = False

    def _prepare_directory(self) -> None:
        parent = self.home / ".mimir"
        if parent.exists() and parent.is_symlink():
            raise AcpDaemonError("ACP parent directory must not be a symlink")
        parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        if self.directory.exists() and self.directory.is_symlink():
            raise AcpDaemonError("ACP directory must not be a symlink")
        try:
            self.directory.mkdir(mode=_DIRECTORY_MODE)
        except FileExistsError:
            pass
        _safe_stat(
            self.directory, kind="directory", mode=_DIRECTORY_MODE, uid=self._uid
        )

    async def _remove_stale_socket(self) -> None:
        try:
            value = self.socket_path.lstat()
        except FileNotFoundError:
            return
        _safe_stat(self.socket_path, kind="socket", mode=_SOCKET_MODE, uid=self._uid)
        identity = (value.st_dev, value.st_ino)
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise AcpDaemonError(f"cannot probe existing ACP socket: {exc}") from exc
        else:
            del reader
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), ACP_PEER_ABORT_TIMEOUT)
            except (TimeoutError, OSError):
                writer.transport.abort()
            raise AcpDaemonError("an ACP daemon is already listening")
        current = _safe_stat(
            self.socket_path, kind="socket", mode=_SOCKET_MODE, uid=self._uid
        )
        if (current.st_dev, current.st_ino) != identity:
            raise AcpDaemonError("ACP socket changed during stale-socket probe")
        self.socket_path.unlink()

    async def _admit_peer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sock = writer.get_extra_info("socket")
        if (
            self._stopping
            or sock is None
            or _peer_uid(sock) != self._uid
        ):
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), ACP_PEER_ABORT_TIMEOUT)
            except (TimeoutError, OSError):
                writer.transport.abort()
            return
        if self._admitted + self._unretired_generations() >= ACP_MAX_PEERS:
            retiring = {peer.task for peer in self._peers if not peer.task.done()}
            if retiring:
                await asyncio.wait(retiring, timeout=ACP_PEER_RETIRE_TIMEOUT)
            self._prune_finished_peers()
        if self._admitted + self._unretired_generations() >= ACP_MAX_PEERS:
            payload = {
                "jsonrpc": "2.0",
                "id": None,
                "error": connection_busy_error().to_error_obj(),
            }
            writer.write(
                (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            )
            try:
                await asyncio.wait_for(writer.drain(), ACP_PEER_ABORT_TIMEOUT)
            except (TimeoutError, ConnectionError, OSError):
                pass
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), ACP_PEER_ABORT_TIMEOUT)
            except (TimeoutError, OSError):
                writer.transport.abort()
            return
        self._admitted += 1
        task = asyncio.create_task(self._run_peer(reader, writer))
        peer = _Peer(writer, task)
        self._peers.add(peer)

        def finished(_: asyncio.Task[None]) -> None:
            if peer in self._peers:
                self._peers.discard(peer)
                self._admitted -= 1

        task.add_done_callback(finished)

    def _prune_finished_peers(self) -> None:
        for peer in tuple(self._peers):
            if peer.task.done():
                self._peers.discard(peer)
                self._admitted -= 1

    async def _run_peer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        agent = self._agent
        if agent is None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), ACP_PEER_ABORT_TIMEOUT)
            except (TimeoutError, OSError):
                writer.transport.abort()
            return
        connection_agent = _ConnectionAgent(agent)
        runner = asyncio.create_task(
            run_stdio_agent(
                connection_agent,
                request_reader=reader,
                response_writer=writer,
                on_close_abandoned=self._record_abandoned_close,
            )
        )
        self._connection_runners.add(runner)
        self._runner_writers[runner] = writer

        def runner_finished(task: asyncio.Task[None]) -> None:
            self._connection_runners.discard(task)
            self._runner_writers.pop(task, None)

        runner.add_done_callback(runner_finished)
        auth_wait = asyncio.create_task(connection_agent.authenticated.wait())
        watchdog: asyncio.Task[None] | None = None
        try:
            done, _ = await asyncio.wait(
                {runner, auth_wait}, timeout=ACP_AUTH_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise AcpDaemonError("ACP peer authentication timed out")
            if runner in done:
                await runner
            else:
                watchdog = asyncio.create_task(
                    self._watch_peer(writer, connection_agent.peer)
                )
                done, _ = await asyncio.wait(
                    {runner, watchdog}, return_when=asyncio.FIRST_COMPLETED
                )
                if watchdog in done:
                    await watchdog
                await runner
        finally:
            auth_wait.cancel()
            if watchdog is not None:
                watchdog.cancel()
            await asyncio.gather(
                *(task for task in (auth_wait, watchdog) if task is not None),
                return_exceptions=True,
            )
            await self._finish_runner(runner, writer)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), ACP_PEER_ABORT_TIMEOUT)
            except (TimeoutError, OSError):
                writer.transport.abort()

    async def _watch_peer(
        self, writer: asyncio.StreamWriter, peer: Any | None = None
    ) -> None:
        while True:
            await asyncio.sleep(ACP_PEER_WATCHDOG_INTERVAL)
            if peer is None:
                try:
                    await asyncio.wait_for(writer.drain(), ACP_PEER_DRAIN_TIMEOUT)
                except TimeoutError as exc:
                    raise AcpDaemonError(
                        "authenticated ACP peer stopped draining"
                    ) from exc
                continue
            drain = asyncio.create_task(writer.drain())
            transport_dead = asyncio.create_task(peer.wait_transport_dead())
            try:
                done, _ = await asyncio.wait(
                    {drain, transport_dead},
                    timeout=ACP_PEER_DRAIN_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if peer.closed:
                    return
                if drain in done:
                    await drain
                    continue
                raise AcpDaemonError("authenticated ACP peer stopped draining")
            finally:
                for task in (drain, transport_dead):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(drain, transport_dead, return_exceptions=True)

    def _record_abandoned_close(self, task: asyncio.Task[None]) -> None:
        """Note a generation whose close outlived its runner.

        ``run_stdio_agent`` bounds how long it waits for the shielded close, so
        it can return while that close is still running -- and returning frees
        this daemon's admission slot, which is released from the runner task's
        done callback. Recording the task lets ``_unretired_generations`` fence
        admission until it terminates, and surfaces the condition so the
        supervisor can recycle a peer that never finishes retiring.
        """
        self._abandoned_closes.add(task)
        _LOGGER.error(
            "ACP peer left an unretired close task; refusing new peers until it "
            "terminates (ACP_MAX_PEERS=%d)",
            ACP_MAX_PEERS,
        )

    def _unretired_generations(self) -> int:
        """Generations that have not demonstrably finished retiring.

        Counted against capacity so a replacement peer cannot join a generation
        still holding old work. Finishing is not the same as retiring: a close
        that completes *with an exception* tore down into an unknown state, so
        releasing capacity on ``done()`` alone would admit a replacement on a
        failed teardown and swallow the failure, because nothing retrieves the
        task's result.

        A close that returns, or that honours the cancellation, has stopped —
        nothing of that generation is running, so capacity returns. One that
        raises keeps the fence permanently and is surfaced once, leaving the
        supervisor to recycle a daemon whose teardown cannot be trusted.
        """
        live: set[asyncio.Task[None]] = set()
        for task in self._abandoned_closes:
            if not task.done():
                live.add(task)
                continue
            if task.cancelled():
                # It honoured the cancel: nothing of that generation is left
                # running, so this is a clean retirement.
                continue
            failure = task.exception()
            if failure is None:
                continue
            self._failed_retirements += 1
            _LOGGER.error(
                "ACP abandoned close failed to retire (%r); admission stays "
                "closed until this daemon is recycled",
                failure,
            )
        self._abandoned_closes = live
        return len(live) + self._failed_retirements

    async def _finish_runner(
        self,
        runner: asyncio.Task[None],
        writer: asyncio.StreamWriter,
    ) -> None:
        if runner.done():
            await asyncio.gather(runner, return_exceptions=True)
            return
        runner.cancel()
        _, pending = await asyncio.wait(
            {runner}, timeout=ACP_PEER_CANCEL_TIMEOUT
        )
        if pending:
            writer.transport.abort()
            _, pending = await asyncio.wait(
                pending, timeout=ACP_PEER_ABORT_TIMEOUT
            )
        if pending:
            raise AcpDaemonError("ACP connection runner resisted cancellation and abort")
        await asyncio.gather(runner, return_exceptions=True)

    async def _stop_peers(self) -> None:
        peers = tuple(self._peers)
        semaphore = asyncio.Semaphore(ACP_CLOSE_CONCURRENCY)

        async def close(peer: _Peer) -> None:
            async with semaphore:
                peer.writer.close()
                try:
                    await asyncio.wait_for(
                        peer.writer.wait_closed(), ACP_PEER_GRACE_TIMEOUT
                    )
                except (TimeoutError, OSError):
                    peer.writer.transport.abort()

        await asyncio.gather(*(close(peer) for peer in peers))
        pending = {peer.task for peer in peers if not peer.task.done()}
        if pending:
            _, pending = await asyncio.wait(
                pending, timeout=ACP_PEER_GRACE_TIMEOUT
            )
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(
                pending, timeout=ACP_PEER_CANCEL_TIMEOUT
            )
        if pending:
            pending = set(self._abort_peers(peers, tasks=pending))
            _, pending = await asyncio.wait(
                pending, timeout=ACP_PEER_ABORT_TIMEOUT
            )
        if pending:
            raise AcpDaemonError("ACP peers did not terminate after transport abort")
        await asyncio.gather(*(peer.task for peer in peers), return_exceptions=True)
        runners = set(self._connection_runners)
        for runner in runners:
            writer = self._runner_writers.get(runner)
            if writer is not None:
                await self._finish_runner(runner, writer)

    def _abort_peers(
        self,
        peers: tuple[_Peer, ...],
        *,
        tasks: set[asyncio.Task[None]] | None = None,
    ) -> tuple[asyncio.Task[None], ...]:
        selected = tasks or {peer.task for peer in peers if not peer.task.done()}
        for peer in peers:
            if peer.task not in selected:
                continue
            peer.task.cancel()
            peer.writer.transport.abort()
        for runner in tuple(self._connection_runners):
            runner.cancel()
            writer = self._runner_writers.get(runner)
            if writer is not None:
                writer.transport.abort()
        return tuple(selected)

    def _unlink_owned_socket(self, identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        try:
            value = self.socket_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if (value.st_dev, value.st_ino) != identity:
            return
        if (
            stat.S_ISSOCK(value.st_mode)
            and value.st_uid == self._uid
            and stat.S_IMODE(value.st_mode) == _SOCKET_MODE
        ):
            try:
                self.socket_path.unlink()
            except OSError:
                pass


__all__ = [
    "ACP_AUTH_TIMEOUT",
    "ACP_MAX_PEERS",
    "ACP_PEER_ABORT_TIMEOUT",
    "ACP_PEER_RETIRE_TIMEOUT",
    "AcpDaemon",
    "AcpDaemonError",
    "acp_enabled_from_env",
    "peer_credentials_supported",
]
