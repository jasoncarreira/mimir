from __future__ import annotations

import asyncio
import ctypes
import errno
import logging
import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import MimirAcpAgent
from .sdk import run_stdio_agent

ACP_AUTH_TIMEOUT = 10.0
ACP_PEER_GRACE_TIMEOUT = 5.0
ACP_PEER_CANCEL_TIMEOUT = 2.0
ACP_SHUTDOWN_TIMEOUT = 20.0
ACP_MAX_PEERS = 8
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


def acp_enabled_from_env() -> bool:
    raw = os.environ.get("MIMIR_ACP_ENABLED")
    supported = os.name == "posix" and peer_credentials_supported()
    if raw is None or not raw.strip():
        return supported
    normalized = raw.strip().lower()
    if normalized in _FALSE_VALUES:
        return False
    if normalized in _TRUE_VALUES:
        if not supported:
            raise AcpDaemonError(
                "MIMIR_ACP_ENABLED is true, but owner-verifiable Unix peers are unsupported"
            )
        return True
    _LOGGER.warning(
        "MIMIR_ACP_ENABLED=%r is not a recognised boolean; using default %r",
        raw,
        supported,
    )
    return supported


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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def on_connect(self, peer: Any) -> int:
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
        self._admitted = 0
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
        if server is not None:
            server.close()
            await server.wait_closed()
        try:
            await asyncio.wait_for(self._stop_peers(), ACP_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            for peer in tuple(self._peers):
                peer.task.cancel()
                transport = peer.writer.transport
                transport.abort()
            await asyncio.gather(
                *(peer.task for peer in tuple(self._peers)), return_exceptions=True
            )
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
            await writer.wait_closed()
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
            or self._admitted >= ACP_MAX_PEERS
            or sock is None
            or _peer_uid(sock) != self._uid
        ):
            writer.close()
            await writer.wait_closed()
            return
        self._admitted += 1
        task = asyncio.create_task(self._run_peer(reader, writer))
        peer = _Peer(writer, task)
        self._peers.add(peer)

        def finished(_: asyncio.Task[None]) -> None:
            self._peers.discard(peer)
            self._admitted -= 1

        task.add_done_callback(finished)

    async def _run_peer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        agent = self._agent
        if agent is None:
            writer.close()
            await writer.wait_closed()
            return
        connection_agent = _ConnectionAgent(agent)
        task = asyncio.create_task(
            run_stdio_agent(
                connection_agent, request_reader=reader, response_writer=writer
            )
        )
        auth_wait = asyncio.create_task(connection_agent.authenticated.wait())
        try:
            done, _ = await asyncio.wait(
                {task, auth_wait}, timeout=ACP_AUTH_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                task.cancel()
            elif auth_wait in done and connection_agent.authenticated.is_set():
                await task
            else:
                await task
        finally:
            auth_wait.cancel()
            await asyncio.gather(auth_wait, return_exceptions=True)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _stop_peers(self) -> None:
        peers = tuple(self._peers)
        semaphore = asyncio.Semaphore(ACP_CLOSE_CONCURRENCY)

        async def close(peer: _Peer) -> None:
            async with semaphore:
                peer.writer.close()
                try:
                    await peer.writer.wait_closed()
                except OSError:
                    pass

        await asyncio.gather(*(close(peer) for peer in peers))
        pending = [peer.task for peer in peers if not peer.task.done()]
        if pending:
            done, pending_set = await asyncio.wait(
                pending, timeout=ACP_PEER_GRACE_TIMEOUT
            )
            del done
            for task in pending_set:
                task.cancel()
            if pending_set:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_set, return_exceptions=True),
                        ACP_PEER_CANCEL_TIMEOUT,
                    )
                except TimeoutError:
                    for peer in peers:
                        if peer.task in pending_set:
                            peer.writer.transport.abort()

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
    "AcpDaemon",
    "AcpDaemonError",
    "acp_enabled_from_env",
    "peer_credentials_supported",
]
