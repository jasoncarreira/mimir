from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shlex
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.acp.agent import ConnectionState, MimirAcpAgent
from mimir.acp.host import _FrameDelivery, close_protocol_writer
from mimir.acp.journal import JournalCache
from mimir.acp.proxy import ProxyRouter, _OutputWriter, run_router
from mimir.acp.session_store import SessionStore
from mimir.acp.transport import close_writer, pump_stream


def _journal_source(progress: Path) -> str:
    # Keep C-level bytes separate: they have no line framing and must not alter
    # the existing text journal's ordered prefix. Neither file needs pipe EOF.
    return f"_journal_path = {str(progress)!r}\n" + r'''
import faulthandler, os, signal, socket, threading, time
from mimir.acp import proxy
_journal_fd = os.open(_journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
_wakeup_fd = os.open(_journal_path + '.wakeup',
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK, 0o600)
_diagnostic_fd = os.open(_journal_path + '.diagnostics',
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
_stack_fd = os.open(_journal_path + '.stacks',
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
def diagnose(value):
    os.write(_diagnostic_fd, (f'{time.monotonic():.6f} thread={threading.get_ident()} '
                             + value + '\n').encode())
# The C watchdog can dump even when Python dispatch or the journal tee stalls.
# This is observation only: it neither expires the controlled timer nor exits.
faulthandler.dump_traceback_later(proxy.SIGNAL_EXIT_TIMEOUT, repeat=True, file=_stack_fd)
def record(value):
    os.write(_journal_fd, value + b'\n')
record(b'child-started')

_tee_writer = None
_tee_ack = threading.Event()
def _journal_flush():
    if _tee_writer is not None:
        diagnose('flush-enter')
        _tee_ack.clear()
        diagnose('flush-cleared')
        _tee_writer.sendall(b'\0')
        diagnose('flush-sent')
        _tee_ack.wait()
        diagnose('flush-returned')

_journal_install = proxy._ShutdownHooks.install
def _install(self):
    global _tee_writer, _tee_thread
    record(b'install-enter')
    _journal_install(self)
    reader, _tee_writer = socket.socketpair()
    _tee_writer.setblocking(False)
    production_fd = signal.set_wakeup_fd(_tee_writer.fileno())
    assert production_fd >= 0
    def forward():
        with reader:
            while True:
                data = reader.recv(4096)
                for value in data:
                    if value == 255:
                        return
                    if value == 0:
                        diagnose('tee-ack-enter')
                        _tee_ack.set()
                        diagnose('tee-ack-returned')
                        continue
                    os.write(_wakeup_fd, bytes([value]))
                    diagnose(f'tee-forward-enter:{value}')
                    try:
                        os.write(production_fd, bytes([value]))
                    except BlockingIOError:
                        # A full production socket is already readable.
                        pass
                    diagnose(f'tee-forward-returned:{value}')
    _tee_thread = threading.Thread(target=forward, daemon=True)
    _tee_thread.start()
    record(b'handlers-installed')
proxy._ShutdownHooks.install = _install
_journal_close_wakeup = proxy._ShutdownHooks._close_wakeup
def _close_wakeup(self):
    global _tee_writer
    if _tee_writer is not None:
        signal.set_wakeup_fd(_wakeup_fd)
        _journal_flush()
        writer, _tee_writer = _tee_writer, None
        writer.sendall(b'\xff')
        _tee_thread.join()
        writer.close()
    _journal_close_wakeup(self)
    # After loop close, retain the original journal's C-delivery evidence.
    signal.set_wakeup_fd(_wakeup_fd)
proxy._ShutdownHooks._close_wakeup = _close_wakeup
_journal_signal = proxy._ShutdownHooks._handle_signal
def _handle_signal(self, signum, frame):
    diagnose(f'signal-dispatch:{signum} interrupted={frame.f_code.co_name}:{frame.f_lineno}')
    _journal_flush()
    record(b'signal-enter:' + str(signum).encode())
    return _journal_signal(self, signum, frame)
proxy._ShutdownHooks._handle_signal = _handle_signal
_journal_force_exit = proxy._ShutdownHooks._force_exit
def _force_exit(self):
    record(b'force-exit-enter')
    return _journal_force_exit(self)
proxy._ShutdownHooks._force_exit = _force_exit
_journal_exit = os._exit
def _exit(code):
    diagnose(f'exit-dispatch:{code}')
    _journal_flush()
    _journal_exit(code)
os._exit = _exit

class JournalTimer(threading.Timer):
    def __init__(self, interval, function, args=None, kwargs=None):
        def fired(*args, **kwargs):
            record(b'watchdog-fired')
            return function(*args, **kwargs)
        super().__init__(interval, fired, args, kwargs)

    def start(self):
        record(b'watchdog-start-enter')
        super().start()
        record(b'watchdog-start-returned')

    def run(self):
        diagnose(f'watchdog-timed-wait:{self.interval}')
        try:
            super().run()
        finally:
            diagnose(f'watchdog-run-returned:finished={self.finished.is_set()}')

    def cancel(self):
        diagnose('watchdog-cancel-enter')
        super().cancel()
        diagnose('watchdog-cancel-returned')

class InputTimer(JournalTimer):
    def run(self):
        # No wall-clock deadline: EOF in the escalation protocol is NOT expiry.
        diagnose('watchdog-input-wait')
        token = os.read(0, 1)
        cancelled = self.finished.is_set()
        diagnose(f'watchdog-input-returned:{token!r} cancelled={cancelled}')
        if token == b'x' and not cancelled:
            self.function(*self.args, **self.kwargs)
        self.finished.set()
        diagnose('watchdog-input-run-returned')

proxy.threading.Timer = JournalTimer
'''


@asynccontextmanager
async def _shutdown_ceiling(
    process: asyncio.subprocess.Process, progress: Path, outstanding: Callable[[], str],
    *, timeout: float = 120,
) -> AsyncIterator[None]:
    try:
        async with asyncio.timeout(timeout):
            yield
    except TimeoutError:
        state = progress.read_text() if progress.exists() else "<no child progress>"
        wakeup = Path(str(progress) + ".wakeup")
        delivery = (
            " ".join(f"wakeup-byte:{value}" for value in wakeup.read_bytes()) or "<no wakeup bytes>"
            if wakeup.exists() else "<wakeup journal not created>"
        )
        details = []
        for suffix in ("diagnostics", "stacks"):
            path = Path(str(progress) + "." + suffix)
            content = path.read_text() if path.exists() else "<not created>"
            details.append(f"{suffix}:\n{content or '<empty>'}")
        pytest.fail(
            f"ACP shutdown ceiling expired: outstanding={outstanding()}, "
            f"pid={process.pid}, returncode={process.returncode}; child progress:\n{state}"
            f"\nsignal delivery: {delivery}"
            + "\n" + "\n".join(details)
        )


async def _accept_unavailable_backend_risk_for_lifecycle(session_id: str) -> bool:
    """Explicit test operator consent; available backends still must confine."""
    return True


@pytest.fixture
def lifecycle_shell(monkeypatch: pytest.MonkeyPatch) -> str:
    # Only generation teardown may finish this shell, not a sleep or tool timer.
    import mimir.acp.hosted as hosted

    monkeypatch.setattr(hosted, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "timeout_at": lambda deadline: asyncio.timeout(None),
    }))
    return f"exec {shlex.quote(sys.executable)} -c 'import signal; signal.pause()'"


@pytest.mark.asyncio
@pytest.mark.parametrize("journal", [False, True], ids=["production", "chained-journal"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
async def test_idle_selector_signal_wakes_and_tears_down(
    journal: bool, signum: signal.Signals, tmp_path: Path,
) -> None:
    progress = tmp_path / "child-progress"
    source = (_journal_source(progress) if journal else "") + r'''
import asyncio, io, os, signal, socket, sys, threading
from mimir.acp import proxy

signum = int(sys.argv[1])
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
select = loop._selector.select
installed = None
observed = False
original_install = proxy._ShutdownHooks.install
def install(self):
    global installed
    original_install(self)
    installed = self
proxy._ShutdownHooks.install = install

def idle_select(timeout=None):
    global observed
    if installed is not None and not observed and timeout is None:
        # This isolated loop owns all its state. No scheduled deadline, runnable
        # callback or readable descriptor can rescue a missing signal wakeup.
        assert not loop._ready
        assert not loop._scheduled
        assert select(0) == []
        observed = True
        os.write(1, b'idle\n')
        events = select(None)
        reader = installed._wakeup[0]
        assert any(key.fd == reader.fileno() for key, mask in events)
        assert reader.recv(4096, socket.MSG_PEEK) == bytes([signum])
        os.write(1, b'woken\n')
        return events
    return select(timeout)
loop._selector.select = idle_select

def deliver():
    assert os.read(0, 1) == b'x'
    # Target the worker: do not rely on EINTR interrupting main's selector.
    signal.pthread_kill(threading.get_ident(), signum)
threading.Thread(target=deliver, daemon=True).start()

async def run():
    writers = [proxy._OutputWriter(io.BytesIO()), proxy._OutputWriter(io.BytesIO())]
    try:
        await proxy.run_router(asyncio.StreamReader(), writers[0],
                               asyncio.StreamReader(), writers[1], 'secret')
    except proxy.ProxySignalExit as exc:
        assert exc.code == 128 + signum
        assert installed._router._close_complete
        assert all(writer.closed for writer in writers)
        os.write(1, b'torn-down\n')
    else:
        raise AssertionError('signal did not shut down router')
loop.run_until_complete(run())
assert observed
assert installed._wakeup is not None  # retained for outer async drains
loop.close()
assert installed._wakeup is None
assert signal.getsignal(signum) is installed._handler
installed._watchdog.cancel()
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, str(signum),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "idle selector"):
            assert await process.stdout.readline() == b"idle\n"
            # The bound lives in the parent, never in the idle child's loop.
            async with asyncio.timeout(5.0):
                stdout, stderr = await process.communicate(b"x")
            assert (process.returncode, stdout, stderr) == (0, b"woken\ntorn-down\n", b"")
        if journal:
            assert progress.with_suffix(".wakeup").read_bytes() == bytes([signum])
            assert f"signal-enter:{signum}" in progress.read_text().splitlines()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.parametrize("finish", ["hooks-close", "loop-close", "install-failure"])
def test_signal_wakeup_resource_lifetime(finish: str) -> None:
    import subprocess

    source = r'''
import asyncio, signal, socket, sys
from types import SimpleNamespace
from mimir.acp import proxy

finish = sys.argv[1]
loop = asyncio.new_event_loop()
previous_reader, previous_writer = socket.socketpair()
previous_writer.setblocking(False)
signal.set_wakeup_fd(previous_writer.fileno())
previous_handler = signal.getsignal(signal.SIGTERM)
sockets = []
socketpair = socket.socketpair
def tracked_pair():
    pair = socketpair()
    sockets.extend(pair)
    return pair
proxy.socket.socketpair = tracked_pair
set_wakeup_fd = signal.set_wakeup_fd
def failed_install(*args, **kwargs):
    raise ValueError('injected wakeup installation failure')

async def run():
    global hooks
    hooks = proxy._ShutdownHooks(SimpleNamespace(terminate_owned_children=lambda: None))
    if finish == 'install-failure':
        signal.set_wakeup_fd = failed_install
        try:
            hooks.install()
        except ValueError as exc:
            assert str(exc) == 'injected wakeup installation failure'
        else:
            raise AssertionError('installation should fail')
        finally:
            signal.set_wakeup_fd = set_wakeup_fd
        assert signal.getsignal(signal.SIGTERM) is previous_handler
        assert len(loop._selector.get_map()) == 1  # only asyncio's own socket
        return
    hooks.install()
    assert set_wakeup_fd(sockets[1].fileno()) == sockets[1].fileno()
    try:
        loop.close()
    except RuntimeError:
        pass
    else:
        raise AssertionError('closing a running loop must fail')
    assert all(sock.fileno() >= 0 for sock in sockets)
    if finish == 'hooks-close':
        hooks.close()
        hooks.close()
        assert signal.getsignal(signal.SIGTERM) is previous_handler
        assert len(loop._selector.get_map()) == 1

loop.run_until_complete(run())
loop.close()
loop.close()
assert len(sockets) == 2
assert all(sock.fileno() == -1 for sock in sockets)
assert set_wakeup_fd(-1) == previous_writer.fileno()
hooks.close()
assert signal.getsignal(signal.SIGTERM) is previous_handler
previous_reader.close()
previous_writer.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", source, finish], capture_output=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.parametrize("mode", ["double", "fifo", "lifo", "other-loop", "replacement"])
def test_shutdown_hooks_restore_only_owned_resources(mode: str) -> None:
    import subprocess

    source = r'''
import asyncio, signal, socket, sys
from types import SimpleNamespace
from mimir.acp import proxy

mode = sys.argv[1]
loop = asyncio.new_event_loop()
other_loop = asyncio.new_event_loop() if mode == 'other-loop' else loop
original_close = loop.close
other_close = other_loop.close
signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
previous_handlers = {sig: signal.getsignal(sig) for sig in signals}
previous_reader, previous_writer = socket.socketpair()
previous_writer.setblocking(False)
signal.set_wakeup_fd(previous_writer.fileno())
sockets = []
socketpair = socket.socketpair
def tracked_pair():
    pair = socketpair()
    sockets.extend(pair)
    return pair
proxy.socket.socketpair = tracked_pair

async def make():
    return proxy._ShutdownHooks(SimpleNamespace(terminate_owned_children=lambda: None))

# Construct both before installing either: predecessor capture belongs to install.
first = loop.run_until_complete(make())
second = other_loop.run_until_complete(make())
first.install()
first_pair = first._wakeup
assert loop.close is first._close_loop
if mode == 'double':
    first.install()
    assert first._wakeup is first_pair
    assert len(sockets) == 2
    first.close()
elif mode == 'replacement':
    replacement_close = lambda: None
    replacement_handler = lambda sig, frame: None
    loop.close = replacement_close
    for sig in signals:
        signal.signal(sig, replacement_handler)
    signal.set_wakeup_fd(previous_writer.fileno())
    first.close()
    # Negative controls: foreign hooks must not be overwritten by cleanup.
    assert loop.close is replacement_close
    assert all(signal.getsignal(sig) is replacement_handler for sig in signals)
    loop.close = original_close
    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)
else:
    second.install()
    second_pair = second._wakeup
    assert len(sockets) == 4
    if mode == 'lifo':
        second.close()
        assert loop.close is first._close_loop
        assert signal.set_wakeup_fd(first_pair[1].fileno()) == first_pair[1].fileno()
        assert all(signal.getsignal(sig) is first._handler for sig in signals)
        first.close()
    else:
        first.close()
        first.close()
        assert other_loop.close is second._close_loop
        assert all(sock.fileno() == -1 for sock in first_pair)
        assert all(sock.fileno() >= 0 for sock in second_pair)
        assert second._previous_wakeup_fd == previous_writer.fileno()
        assert signal.set_wakeup_fd(second_pair[1].fileno()) == second_pair[1].fileno()
        assert all(signal.getsignal(sig) is second._handler for sig in signals)
        second.close()

assert first._wakeup is None
assert all(sock.fileno() == -1 for sock in sockets)
assert not proxy._ShutdownHooks._wakeup_owners
assert signal.set_wakeup_fd(-1) == previous_writer.fileno()
assert all(signal.getsignal(sig) is handler for sig, handler in previous_handlers.items())
assert loop.close == original_close
assert other_loop.close == other_close
assert len(loop._selector.get_map()) == 1
assert len(other_loop._selector.get_map()) == 1
loop.close()
other_loop.close()
first.close()
second.close()
previous_reader.close()
previous_writer.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", source, mode], capture_output=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, None])
@pytest.mark.parametrize("stage", ["read", "idle", "close", "failure", "read-failure", "drain-failure", "close-failure"])
async def test_proxy_signal_teardown_and_client_eof_are_silent(
    signum: signal.Signals | None, stage: str, tmp_path: Path,
) -> None:
    # Real signals in an isolated interpreter cannot affect pytest's event loop.
    # Deliver during a reader task to exercise interruption of live I/O, rather
    # than relying on a platform-specific selector to reproduce the old crash.
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + """
import asyncio, io, os, signal, sys
from types import SimpleNamespace
from mimir.acp import bootstrap, profiles, proxy

signum = int(sys.argv[1])
stage = sys.argv[2]
profiles.ProfileStore = lambda: SimpleNamespace(get=lambda name: SimpleNamespace(remote=None))
profiles.selected_profile = lambda name: 'test'
proxy.PEER_EOF_GRACE_TIMEOUT = 0.01

async def run_proxy(name, output):
    client = asyncio.StreamReader()
    daemon = asyncio.StreamReader()
    writers = [proxy._OutputWriter(io.BytesIO()), proxy._OutputWriter(io.BytesIO())]
    router = proxy.ProxyRouter(*writers, 'secret')
    original_close = router.close
    original_terminate = router.terminate_owned_children
    def terminate():
        original_terminate()
        record(b'terminated')
        output.write(b'terminated\\n')
    async def close():
        record(b'draining')
        if stage in ('close', 'close-failure') and signum:
            os.kill(os.getpid(), signum)
        if stage == 'close-failure':
            raise ValueError('private shutdown failure')
        await asyncio.sleep(0)
        await original_close()
        if stage == 'failure':
            raise ValueError('private shutdown failure')
        output.write(b'closed\\n')
    router.terminate_owned_children = terminate
    router.close = close
    proxy.ProxyRouter = lambda *args: router
    if stage == 'drain-failure':
        class DrainingReader:
            async def read(self, size):
                try:
                    await asyncio.Future()
                finally:
                    if signum:
                        os.kill(os.getpid(), signum)
                    await asyncio.sleep(0)
        daemon = DrainingReader()
    if stage in ('read-failure', 'drain-failure') or (signum and stage not in ('close', 'close-failure')):
        class SignallingReader:
            async def read(self, size):
                await asyncio.sleep(0)
                if stage == 'idle':
                    record(b'ready')
                    output.write(b'ready\\n')
                elif signum and stage != 'drain-failure':
                    os.kill(os.getpid(), signum)
                if stage in ('read-failure', 'drain-failure'):
                    raise ValueError('private shutdown failure')
                await asyncio.Future()
        client = SignallingReader()
    else:
        client.feed_eof()
    try:
        await proxy.run_router(client, writers[0], daemon, writers[1], 'secret')
    finally:
        if 'failure' not in stage:
            # The outer proxy's transport teardown must see a quiescent router
            # on signal exits too, not only on the normal EOF return.
            assert router._close_complete
            assert all(writer.closed for writer in writers)

proxy.run_proxy = run_proxy
raise SystemExit(bootstrap.main([]))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, str(signum or 0), stage,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    outstanding = "ready" if stage == "idle" and signum else "exit"
    try:
        async with _shutdown_ceiling(process, progress, lambda: outstanding):
            if stage == "idle" and signum:
                assert await process.stdout.readline() == b"ready\n"
                process.send_signal(signum)
            outstanding = "exit"
            stdout, stderr = await process.communicate()
            if "failure" in stage:
                assert process.returncode == 1
                assert stderr.startswith(b"detail: ValueError at <string>:")
                assert stderr.endswith(b"\nerror: acp-failed\n")
                assert b"private shutdown failure" not in stderr
            else:
                assert stderr == b""
                assert process.returncode == (128 + signum if signum else 0)
                assert stdout == (b"terminated\n" if signum else b"") + b"closed\n"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def _await_diagnostic(progress: Path, marker: str, *, timeout: float = 30) -> str:
    """Wait for a diagnostic written by the child's watchdog THREAD.

    ``armed`` is written by ``Timer.start()`` on the thread that CALLS start, so
    it orders nothing about markers written inside ``run()`` by the timer thread
    itself. Callers must await this while the child is still ALIVE: once the
    protocol has escalated and reaped it, no writer remains and polling the final
    file only delays the same failure.
    """
    path = progress.with_suffix(".diagnostics")
    deadline = time.monotonic() + timeout
    while True:
        text = path.read_text() if path.exists() else ""
        if marker in text:
            return text
        assert time.monotonic() < deadline, (
            f"{marker!r} not observed within {timeout}s; diagnostics:\n{text or '<empty>'}"
        )
        await asyncio.sleep(0.01)


async def _signal_exit_protocol(
    process: asyncio.subprocess.Process, progress: Path,
    signum: signal.Signals, repeat: bool, *, timeout: float = 120,
    after_armed: Callable[[], Awaitable[None]] | None = None,
) -> None:
    outstanding = "ready"

    async def protocol() -> None:
        nonlocal outstanding, signum
        for marker in ("ready", "armed", "terminated", "draining"):
            outstanding = marker
            observed = await process.stdout.readline()
            assert observed == marker.encode() + b"\n", (marker, observed)
            if marker == "ready":
                process.send_signal(signum)
            elif marker == "armed" and after_armed is not None:
                # Still alive here: escalation and communicate() come after the
                # loop, so this is the only point where a child-thread marker
                # can be synchronised on.
                await after_armed()
        outstanding = "exit"
        if repeat:
            signum = signal.SIGINT if signum != signal.SIGINT else signal.SIGTERM
            process.send_signal(signum)
        else:
            process.stdin.write(b"x")
            await process.stdin.drain()
        stdout, stderr = await process.communicate()
        assert process.returncode == 128 + signum
        assert (stdout, stderr) == (b"", b"")

    task = asyncio.create_task(protocol())
    try:
        # Shield preserves the child's observer when the whole-protocol ceiling
        # expires. The journal can be read without waiting for pipe EOF or exit.
        async with _shutdown_ceiling(process, progress, lambda: outstanding, timeout=timeout):
            await asyncio.shield(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
@pytest.mark.parametrize("repeat", [False, True], ids=["deadline", "escalation"])
@pytest.mark.parametrize("stage", ["route", "close", "writer", "outer", "post-loop", "blocked", "cleanup"])
async def test_signal_exit_bounds_entire_teardown(
    signum: signal.Signals, repeat: bool, stage: str, tmp_path: Path,
) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import atexit, asyncio, io, os, sys, threading
from types import SimpleNamespace
from mimir.acp import bootstrap, profiles, proxy
stage = sys.argv[1]
profiles.ProfileStore = lambda: SimpleNamespace(get=lambda name: SimpleNamespace(remote=None))
profiles.selected_profile = lambda name: 'test'

def mark(value):
    record(value)
    sink.write(value + b'\n')
    sink.flush()

class ControlledTimer(InputTimer):
    def start(self):
        super().start()
        mark(b'armed')

proxy.threading.Timer = ControlledTimer

async def stuck():
    mark(b'draining')
    while True:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass

async def run_proxy(name, output):
    global sink
    sink = output
    class Reader:
        async def read(self, size):
            mark(b'ready')
            try:
                await asyncio.Future()
            finally:
                if stage == 'route':
                    await stuck()
    class Writer(proxy._OutputWriter):
        async def wait_closed(self):
            if stage == 'writer':
                await stuck()
    writers = [Writer(io.BytesIO()), proxy._OutputWriter(io.BytesIO())]
    router = proxy.ProxyRouter(*writers, 'secret')
    original_close = router.close
    original_cleanup = router.terminate_owned_children
    def cleanup():
        original_cleanup()
        mark(b'terminated')
        if stage == 'cleanup':
            mark(b'draining')
            threading.Event().wait()
    async def close():
        if stage == 'blocked':
            mark(b'draining')
            diagnose('teardown-block-enter:router.close')
            threading.Event().wait()
            diagnose('teardown-block-returned:router.close')
        if stage == 'close':
            await stuck()
        await original_close()
    router.close = close
    router.terminate_owned_children = cleanup
    proxy.ProxyRouter = lambda *args: router
    try:
        await proxy.run_router(Reader(), writers[0], asyncio.StreamReader(), writers[1], 'secret')
    finally:
        if stage == 'outer':
            await stuck()
        if stage == 'post-loop':
            loop = asyncio.get_running_loop()
            def after_loop():
                assert loop.is_closed()
                record(b'draining')
                # bootstrap has closed its reserved output and restored fd 1.
                os.write(1, b'draining\n')
                threading.Event().wait()
            atexit.register(after_loop)
proxy.run_proxy = run_proxy
raise SystemExit(bootstrap.main([]))
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, stage,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        # This is only a hang guard; individual pipe reads have no deadline.
        # The controlled watchdog and resistant stage prove the exit boundary:
        # only expiration or a second signal can release the child, regardless
        # of how long the parent takes to observe each ordered marker.
        async def _observe_input_wait() -> None:
            # Written by the timer THREAD; the 'armed' marker above is written by
            # the thread that CALLS start() and orders nothing about it. This must
            # run while the child is alive: escalation and communicate() follow the
            # protocol loop, after which no writer remains and polling the final
            # file would only delay the same failure. Ordering only — later
            # assertions re-read the file for markers written after this point.
            await _await_diagnostic(progress, "watchdog-input-wait")

        await _signal_exit_protocol(
            process, progress, signum, repeat, after_armed=_observe_input_wait,
        )
        assert progress.read_text().splitlines()[:8] == [
            "child-started", "install-enter", "handlers-installed", "ready",
            f"signal-enter:{signum}", "watchdog-start-enter",
            "watchdog-start-returned", "armed",
        ]
        delivered = [signum]
        diagnostics = progress.with_suffix(".diagnostics").read_text()
        assert "watchdog-timed-wait" not in diagnostics
        if repeat:
            delivered.append(signal.SIGINT if signum != signal.SIGINT else signal.SIGTERM)
            assert f"signal-enter:{delivered[-1]}" in progress.read_text().splitlines()
        else:
            assert progress.read_text().splitlines()[-2:] == ["watchdog-fired", "force-exit-enter"]
            assert "watchdog-input-returned:b'x' cancelled=False" in diagnostics
        assert progress.with_suffix(".wakeup").read_bytes() == bytes(delivered)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.parametrize("mode", ["expire", "eof", "cancel"])
def test_controlled_watchdog_reports_why_callback_did_not_run(mode: str, tmp_path: Path) -> None:
    import subprocess

    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import sys
fired = []
timer = InputTimer(proxy.SIGNAL_EXIT_TIMEOUT, lambda: fired.append(True))
if sys.argv[1] == 'cancel':
    timer.cancel()
timer.start()
timer.join()
assert fired == ([True] if sys.argv[1] == 'expire' else [])
'''
    result = subprocess.run(
        [sys.executable, "-c", source, mode],
        input=b"" if mode == "eof" else b"x", capture_output=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    diagnostics = progress.with_suffix(".diagnostics").read_text()
    token = b"" if mode == "eof" else b"x"
    assert f"watchdog-input-returned:{token!r} cancelled={mode == 'cancel'}" in diagnostics
    assert "watchdog-input-run-returned" in diagnostics
    assert "watchdog-timed-wait" not in diagnostics
    assert ("watchdog-fired" in progress.read_text().splitlines()) == (mode == "expire")


@pytest.mark.asyncio
async def test_shutdown_diagnostics_locate_signal_before_journal_flush(tmp_path: Path) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio
from types import SimpleNamespace

def blocked_ack():
    # Simulate the surviving pre-marker stall, after the first handler succeeded.
    faulthandler.dump_traceback(file=_stack_fd)
    os.write(1, b'flush-blocked\n')
    threading.Event().wait()

async def run():
    hooks = proxy._ShutdownHooks(SimpleNamespace(terminate_owned_children=lambda: None))
    hooks.install()
    proxy.threading.Timer = InputTimer
    os.kill(os.getpid(), signal.SIGTERM)
    _tee_ack.wait = blocked_ack
    os.kill(os.getpid(), signal.SIGINT)

asyncio.run(run())
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "flush handshake"):
            assert await process.stdout.readline() == b"flush-blocked\n"
            # The main-thread flush marker does not order the timer thread.
            await _await_diagnostic(progress, "watchdog-input-wait")
        with pytest.raises(pytest.fail.Exception) as failure:
            async with _shutdown_ceiling(process, progress, lambda: "exit", timeout=0.05):
                await process.wait()
        message = str(failure.value)
        assert f"signal-enter:{signal.SIGTERM}\n" in message
        assert f"signal-enter:{signal.SIGINT}\n" not in message
        assert f"signal-dispatch:{signal.SIGINT} interrupted=run:" in message
        assert "flush-sent" in message
        assert "in blocked_ack" in message
        assert "in _journal_flush" in message
        assert "watchdog-input-wait" in message
        assert "watchdog-fired" not in message
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["before-install", "before-handler"])
async def test_preinstall_sigint_exits_without_blocked_teardown(
    delivery: str, tmp_path: Path,
) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, io, sys
from types import SimpleNamespace
from mimir.acp import bootstrap, profiles
profiles.ProfileStore = lambda: SimpleNamespace(get=lambda name: SimpleNamespace(remote=None))
profiles.selected_profile = lambda name: 'test'

original_install = _journal_install
def deliver_install(self):
    if sys.argv[1] == 'before-install':
        os.kill(os.getpid(), signal.SIGINT)
    original_install(self)
    record(b'handlers-installed:cancelling=' + str(asyncio.current_task().cancelling()).encode())
_journal_install = deliver_install
original_signal = signal.signal
def install_handler(signum, handler):
    if sys.argv[1] == 'before-handler' and signum == signal.SIGINT:
        os.kill(os.getpid(), signal.SIGINT)
    return original_signal(signum, handler)

class ArmedTimer(JournalTimer):
    def start(self):
        super().start()
        record(b'armed')
proxy.threading.Timer = ArmedTimer

async def run_proxy(name, output):
    # Only intercept the product registration, not bootstrap's startup handler.
    signal.signal = install_handler
    class Reader:
        async def read(self, size):
            record(b'ready')
            await asyncio.Future()
    async def close(self):
        record(b'draining:blocked-close')
        output.write(b'blocked\n')
        threading.Event().wait()
    proxy.ProxyRouter.close = close
    await proxy.run_router(Reader(), proxy._OutputWriter(io.BytesIO()),
                           asyncio.StreamReader(), proxy._OutputWriter(io.BytesIO()), 'secret')
proxy.run_proxy = run_proxy
raise SystemExit(bootstrap.main([]))
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, delivery,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "startup SIGINT exit"):
            observed = await process.stdout.readline()
            state = progress.read_text().splitlines()
            # A blocked-close handshake makes the unfixed failure immediate and
            # proves it is a live, unarmed child, not just an unexpected exit code.
            assert observed == b"", (process.returncode, state)
            stdout, stderr = await process.communicate()
            assert (process.returncode, stdout, stderr) == (128 + signal.SIGINT, b"", b"")
            assert state == ["child-started", "install-enter"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.parametrize("finish", ["eof", "startup-error"])
def test_startup_sigint_handler_restored(finish: str) -> None:
    import subprocess

    source = r'''
import argparse, asyncio, io, signal, sys
from types import SimpleNamespace
from mimir.acp import bootstrap, profiles, proxy
previous = signal.getsignal(signal.SIGINT)
profiles.ProfileStore = lambda: SimpleNamespace(get=lambda name: SimpleNamespace(remote=None))
profiles.selected_profile = lambda name: 'test'
async def run_proxy(name, output):
    handler = signal.getsignal(signal.SIGINT)
    assert handler is not previous
    assert handler.__name__ == 'startup_sigint'  # Runner must not take ownership
    if sys.argv[1] == 'startup-error':
        raise ValueError('startup failed')
    reader = asyncio.StreamReader()
    reader.feed_eof()
    await proxy.run_router(reader, proxy._OutputWriter(io.BytesIO()),
                           reader, proxy._OutputWriter(io.BytesIO()), 'secret')
    assert signal.getsignal(signal.SIGINT) is handler
proxy.run_proxy = run_proxy
try:
    assert bootstrap._proxy(argparse.Namespace(proxy_profile=None), io.BytesIO()) == 0
except ValueError:
    assert sys.argv[1] == 'startup-error'
else:
    assert sys.argv[1] == 'eof'
assert signal.getsignal(signal.SIGINT) is previous
'''
    result = subprocess.run(
        [sys.executable, "-c", source, finish], capture_output=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("outstanding", ["armed", "terminated", "draining"])
async def test_signal_exit_timeout_reports_child_progress(
    outstanding: str, tmp_path: Path,
) -> None:
    # A deliberately sleeping child, not a cancelled mock read: diagnostics
    # must be available while the child is alive and its pipes remain open.
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import os, signal, sys, time
signal.set_wakeup_fd(_wakeup_fd)
signal.signal(signal.SIGINT, lambda signum, frame: record(b'signal-enter:' + str(signum).encode()))
markers = ['ready', 'armed', 'terminated', 'draining']
outstanding = sys.argv[1]
record(('simulated child sleeping before ' + outstanding).encode())
for marker in markers[:markers.index(outstanding)]:
    os.write(1, marker.encode() + b'\n')
os.write(2, b'sleeping\n')
time.sleep(3600)
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, outstanding,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "sleeping"):
            assert await process.stderr.readline() == b"sleeping\n"
        # Startup has completed; shorten only this diagnostic self-test's ceiling.
        with pytest.raises(pytest.fail.Exception) as failure:
            await _signal_exit_protocol(process, progress, signal.SIGINT, False, timeout=0.05)
        message = str(failure.value)
        assert f"outstanding={outstanding}" in message
        assert f"pid={process.pid}, returncode=None" in message
        assert f"simulated child sleeping before {outstanding}" in message
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.parametrize("stage", ["unarmed", "never-fired", "force-exit", "escalation"])
async def test_shutdown_journal_timeout_distinguishes_surviving_child(
    stage: str, tmp_path: Path,
) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio
from types import SimpleNamespace

class ControlledTimer(JournalTimer):
    def run(self):
        if os.read(0, 1) == b'x':
            self.function(*self.args, **self.kwargs)

proxy.threading.Timer = ControlledTimer
_journal_force_exit = lambda self: record(b'force-exit-survived')
os._exit = lambda code: record(b'escalation-survived:' + str(code).encode())

def cleanup():
    record(b'cleanup-enter')
    threading.Event().wait()

async def run():
    hooks = proxy._ShutdownHooks(SimpleNamespace(terminate_owned_children=cleanup))
    hooks.install()
    os.write(1, b'ready\n')
    threading.Event().wait()

asyncio.run(run())
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )

    async def await_marker(marker: str) -> None:
        while marker not in progress.read_text().splitlines():
            assert process.returncode is None
            await asyncio.sleep(0.01)

    try:
        async with _shutdown_ceiling(process, progress, lambda: "self-test handshake"):
            assert await process.stdout.readline() == b"ready\n"
            if stage != "unarmed":
                process.send_signal(signal.SIGTERM)
                await await_marker("cleanup-enter")
            if stage == "force-exit":
                process.stdin.write(b"x")
                await process.stdin.drain()
                await await_marker("force-exit-survived")
            elif stage == "escalation":
                process.send_signal(signal.SIGINT)
                await await_marker(f"escalation-survived:{128 + signal.SIGINT}")
            # The short timeout tests formatting, never child startup or delivery.
            with pytest.raises(pytest.fail.Exception) as failure:
                async with _shutdown_ceiling(process, progress, lambda: "exit", timeout=0.05):
                    await process.wait()
            message = str(failure.value)
            assert "outstanding=exit" in message
            assert f"pid={process.pid}, returncode=None" in message
            assert "child-started\ninstall-enter\nhandlers-installed\n" in message
            if stage == "unarmed":
                assert "<no wakeup bytes>" in message
                assert "signal-enter:" not in message
                assert "watchdog-start-enter" not in message
            else:
                assert f"wakeup-byte:{signal.SIGTERM}" in message
                assert f"signal-enter:{signal.SIGTERM}\n" in message
                assert "watchdog-start-enter\nwatchdog-start-returned\ncleanup-enter\n" in message
            if stage == "force-exit":
                assert "watchdog-fired\nforce-exit-enter\nforce-exit-survived\n" in message
            else:
                assert "watchdog-fired" not in message
                assert "force-exit-enter" not in message
            if stage == "escalation":
                assert f"wakeup-byte:{signal.SIGINT}" in message
                assert f"signal-enter:{signal.SIGINT}\nescalation-survived:" in message
            assert process.returncode is None
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.skipif(
    sys.platform != "linux",
    reason="C-delivery handshake relies on Linux socketpair MSG_WAITALL readability semantics",
)
async def test_shutdown_journal_wakeup_without_python_signal_handler(tmp_path: Path) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, ctypes, select, socket, time
from types import SimpleNamespace

reader, writer = socket.socketpair()
writer.sendall(b'x')
assert select.select([reader], [], [], 0)[0]
libc = ctypes.CDLL(None)
libc.recv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
libc.recv.restype = ctypes.c_ssize_t
buffer = ctypes.create_string_buffer(2)

def deliver():
    # Losing readability proves libc consumed the first byte of MSG_WAITALL.
    # The peer stays open and never sends the second byte. Target this worker,
    # not main: the C handler writes a byte without interrupting main's recv.
    while select.select([reader], [], [], 0)[0]:
        time.sleep(0.001)
    record(b'main-blocked')
    signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
    record(b'worker-signalled')
    _journal_flush()
    os.write(1, b'delivered\n')

async def run():
    hooks = proxy._ShutdownHooks(SimpleNamespace(terminate_owned_children=lambda: None))
    hooks.install()
    threading.Thread(target=deliver, daemon=True).start()
    libc.recv(reader.fileno(), buffer, 2, socket.MSG_WAITALL)
    record(b'recv-returned')
    threading.Event().wait()

asyncio.run(run())
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "C delivery handshake"):
            assert await process.stdout.readline() == b"delivered\n"
            assert progress.with_suffix(".wakeup").read_bytes() == bytes([signal.SIGTERM])
            with pytest.raises(pytest.fail.Exception) as failure:
                async with _shutdown_ceiling(process, progress, lambda: "Python handler", timeout=0.05):
                    await process.wait()
            message = str(failure.value)
            assert "outstanding=Python handler" in message
            assert f"pid={process.pid}, returncode=None" in message
            assert "handlers-installed\nmain-blocked\nworker-signalled\n" in message
            assert f"wakeup-byte:{signal.SIGTERM}" in message
            assert "signal-enter:" not in message
            assert "signal-dispatch:" not in message
            assert "watchdog-start-enter" not in message
            assert "recv-returned" not in message
            assert process.returncode is None
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.asyncio
async def test_real_signal_watchdog_bounds_blocked_cleanup(tmp_path: Path) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, os, signal, threading
from types import SimpleNamespace
from mimir.acp import proxy

proxy.SIGNAL_EXIT_TIMEOUT = 0.5

async def run():
    hooks = proxy._ShutdownHooks(
        SimpleNamespace(terminate_owned_children=threading.Event().wait)
    )
    hooks.install()
    os.kill(os.getpid(), signal.SIGTERM)
    threading.Event().wait()

asyncio.run(run())
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        # No stage marker competes with the real deadline. This timeout is
        # only a harness bound; the independent product timer must exit.
        async with _shutdown_ceiling(process, progress, lambda: "exit"):
            stdout, stderr = await process.communicate()
            assert process.returncode == 128 + signal.SIGTERM
            assert (stdout, stderr) == (b"", b"")
            assert progress.read_text().splitlines()[-2:] == ["watchdog-fired", "force-exit-enter"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.parametrize("kind", ["shell", "python"])
@pytest.mark.parametrize("error", [PermissionError, ProcessLookupError])
def test_sync_group_cleanup_continues_after_unsignalable_group(
    kind: str, error: type[OSError], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock
    from mimir.acp.hosted import HostedHandsProvider
    from mimir.acp.python_kernel import PythonKernelManager

    owner = HostedHandsProvider() if kind == "shell" else PythonKernelManager()
    owner._processes = {Mock(pid=101): 101, Mock(pid=102): 102}
    kill = Mock(side_effect=[error("denied or gone"), None])
    monkeypatch.setattr("os.killpg", kill)
    if kind == "shell":
        owner._python_kernels = Mock()
    owner.kill_owned_process_groups()
    assert [call.args for call in kill.call_args_list] == [(101, 9), (102, 9)]
    if kind == "shell":
        owner._python_kernels.kill_owned_process_groups.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, None])
@pytest.mark.parametrize("error", ["PermissionError", "RuntimeError"])
async def test_sync_cleanup_exceptions_do_not_escape_exit_boundary(
    signum: signal.Signals | None, error: str, tmp_path: Path,
) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, os, sys
from types import SimpleNamespace
from mimir.acp import proxy
error = getattr(__import__('builtins'), sys.argv[2])
def outer_cleanup():
    os.write(1, b'outer\n')
    raise error('private outer cleanup error')
def cleanup():
    os.write(1, b'router\n')
    raise error('private router cleanup error')
async def setup():
    hooks = proxy._ShutdownHooks(SimpleNamespace(terminate_owned_children=cleanup), outer_cleanup)
    hooks.install()
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(setup())
if int(sys.argv[1]):
    # The installing task is done: exit must come from _cancel, not the deadline.
    proxy.threading.Timer = lambda *args: SimpleNamespace(start=lambda: None)
    os.kill(os.getpid(), int(sys.argv[1]))
    loop.run_forever()
loop.close()
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, str(signum or 0), error,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "exit"):
            stdout, stderr = await process.communicate()
            assert (process.returncode, stderr) == (128 + signum if signum else 0, b"")
            assert stdout == (b"outer\nrouter\n" if signum else b"router\n")
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
async def test_signal_callback_rechecks_completed_installing_task(
    signum: signal.Signals, tmp_path: Path,
) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, io, os, sys
from types import SimpleNamespace
from mimir.acp import proxy
async def setup():
    router = proxy.ProxyRouter(proxy._OutputWriter(io.BytesIO()), proxy._OutputWriter(io.BytesIO()), 'secret')
    hooks = proxy._ShutdownHooks(router)
    hooks.install()
    # Queue cancellation while setup is live, then finish without yielding.
    # An inert watchdog proves the callback, not the fallback, handles this race.
    proxy.threading.Timer = lambda *args: SimpleNamespace(start=lambda: None)
    os.kill(os.getpid(), int(sys.argv[1]))
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(setup())
loop.run_forever()
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, str(signum),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "exit"):
            stdout, stderr = await process.communicate()
            assert process.returncode == 128 + signum
            assert (stdout, stderr) == (b"", b"")
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.asyncio
async def test_signal_deadline_preserves_observed_failure_during_resistant_drain(tmp_path: Path) -> None:
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, io, os, signal
from types import SimpleNamespace
from mimir.acp import bootstrap, profiles, proxy
profiles.ProfileStore = lambda: SimpleNamespace(get=lambda name: SimpleNamespace(remote=None))
profiles.selected_profile = lambda name: 'test'
proxy.threading.Timer = InputTimer
observed_failure = False
original_record_failure = proxy._ShutdownHooks.record_failure
def record_failure(self, error):
    global observed_failure
    original_record_failure(self, error)
    if isinstance(error, ValueError):
        observed_failure = True
proxy._ShutdownHooks.record_failure = record_failure
async def run_proxy(name, output):
    class Failing:
        async def read(self, size):
            await asyncio.sleep(0)
            os.kill(os.getpid(), signal.SIGTERM)
            raise ValueError('private failure')
    class Resistant:
        async def read(self, size):
            while True:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    assert observed_failure
                    output.write(b'failure-draining\n')
                    output.flush()
    await proxy.run_router(Failing(), proxy._OutputWriter(io.BytesIO()),
                           Resistant(), proxy._OutputWriter(io.BytesIO()), 'secret')
proxy.run_proxy = run_proxy
raise SystemExit(bootstrap.main([]))
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        async with _shutdown_ceiling(process, progress, lambda: "exit"):
            assert await process.stdout.readline() == b"failure-draining\n"
            # Failure precedence, not scheduler speed against a real timer, is
            # the subject. Expire only after observation and resistant drain.
            stdout, stderr = await process.communicate(b"x")
            assert process.returncode == 1
            assert stdout == b""
            assert stderr.startswith(b"detail: ValueError at <string>:")
            assert stderr.endswith(b"\nerror: acp-failed\n")
            assert b"private failure" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def test_signal_reap_filter_only_suppresses_expected_watcher_diagnostic() -> None:
    import logging
    from mimir.acp.proxy import _SignalReapFilter

    expected = "Unknown child process pid %d, will report returncode 255"
    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord("asyncio", logging.WARNING, __file__, 1, message, (123,), None)
    assert not _SignalReapFilter().filter(record(expected))
    assert _SignalReapFilter().filter(record("unexpected failure %d"))


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
@pytest.mark.parametrize("output_shape", ["pipe", "file", "tty"])
@pytest.mark.parametrize("stderr_shape", ["pipe", "file", "tty"])
@pytest.mark.parametrize("teardown_failure", [False, True], ids=["clean", "failure"])
async def test_local_proxy_signal_with_real_stdio_and_unix_socket(
    signum: signal.Signals, output_shape: str, stderr_shape: str, teardown_failure: bool,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Do not replace run_proxy, run_router, or open_stdio: all three must run
    # against real OS transports. Inject profile/credential lookup and, for the
    # failure cases, an error after the real router close has completed.
    progress = tmp_path / "child-progress"
    source = _journal_source(progress) + r'''
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from mimir.acp import bootstrap, credentials, profiles, proxy
os.chdir(sys.argv[1])
# Relative path avoids platform AF_UNIX path limits from long pytest roots.
profile = SimpleNamespace(home=Path('.'), remote=None, timeout_seconds=60)
profiles.ProfileStore = proxy.ProfileStore = lambda: SimpleNamespace(get=lambda name: profile)
profiles.selected_profile = proxy.selected_profile = lambda name: 'test'
proxy.NativeCredentialStore = lambda: SimpleNamespace(get=lambda name: 'test-secret')
if sys.argv[2] == 'failure':
    original_close = proxy.ProxyRouter.close
    async def close(self):
        await original_close(self)
        raise ValueError('private shutdown failure')
    proxy.ProxyRouter.close = close
raise SystemExit(bootstrap.main(['--profile', 'test']))
'''
    directory = tmp_path / ".mimir" / "acp"
    directory.mkdir(parents=True, mode=0o700)
    peers: list[asyncio.StreamWriter] = []
    connected = asyncio.Event()
    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peers.append(writer)
        connected.set()
    monkeypatch.chdir(tmp_path)
    server = await asyncio.start_unix_server(accept, path=".mimir/acp/daemon.sock")
    file_output = output_shape == "file"
    file_stderr = stderr_shape == "file"
    terminal = None
    stderr_terminal = None
    if "tty" in (output_shape, stderr_shape):
        import pty
        import tty
    if output_shape == "tty":
        terminal = pty.openpty()
        tty.setraw(terminal[1])
        os.set_blocking(terminal[0], False)
    if stderr_shape == "tty":
        stderr_terminal = pty.openpty()
        tty.setraw(stderr_terminal[1])
        os.set_blocking(stderr_terminal[0], False)
    output_path = tmp_path / "output"
    output_file = output_path.open("wb") if file_output else None
    stderr_path = tmp_path / "stderr"
    stderr_file = stderr_path.open("wb") if file_stderr else None
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, str(tmp_path), "failure" if teardown_failure else "clean",
        stdin=asyncio.subprocess.PIPE,
        stdout=terminal[1] if terminal else output_file if file_output else asyncio.subprocess.PIPE,
        stderr=stderr_terminal[1] if stderr_terminal else stderr_file if file_stderr else asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    outstanding = "ready"
    try:
        async with _shutdown_ceiling(process, progress, lambda: outstanding):
            await connected.wait()
            # An actual routed frame, rather than a sleep or just socket acceptance,
            # proves stdio and the signal hooks are installed before signalling.
            frame = b'{"jsonrpc":"2.0","method":"test/ready"}\n'
            try:
                peers[0].write(frame)
                await peers[0].drain()
            except ConnectionError:
                stdout, stderr = await process.communicate()
                if file_stderr:
                    stderr = stderr_path.read_bytes()
                pytest.fail(f"real stdio startup failed: code={process.returncode}, stderr={stderr!r}")
            async def ready() -> bytes:
                if terminal:
                    observed = bytearray()
                    while not observed.endswith(b"\n") and process.returncode is None:
                        try:
                            observed.extend(os.read(terminal[0], 65536))
                        except BlockingIOError:
                            await asyncio.sleep(0.01)
                    return bytes(observed)
                if not file_output:
                    return await process.stdout.readline()
                while output_path.read_bytes() != frame and process.returncode is None:
                    await asyncio.sleep(0.01)
                return output_path.read_bytes()
            observed = await ready()
            if observed != frame:
                stdout, stderr = await process.communicate()
                if file_stderr:
                    stderr = stderr_path.read_bytes()
                pytest.fail(f"real stdio startup failed: code={process.returncode}, stderr={stderr!r}")
            process.send_signal(signum)
            outstanding = "exit"
            stdout, stderr = await process.communicate()
            if file_stderr:
                stderr = stderr_path.read_bytes()
            if stderr_terminal:
                stderr = b""
                while True:
                    try:
                        stderr += os.read(stderr_terminal[0], 65536)
                    except BlockingIOError:
                        break
            assert stdout == (b"" if output_shape == "pipe" else None)
            if teardown_failure:
                assert process.returncode == 1
                assert re.fullmatch(rb"detail: ValueError at <string>:[0-9]+\nerror: acp-failed\n", stderr)
            else:
                assert (process.returncode, stderr) == (128 + signum, b"")
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        if output_file is not None:
            output_file.close()
        if stderr_file is not None:
            stderr_file.close()
        if terminal:
            for fd in terminal:
                os.close(fd)
        if stderr_terminal:
            for fd in stderr_terminal:
                os.close(fd)
        for writer in peers:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stdio_output_failure_closes_acquired_input(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, Mock
    from mimir.acp.proxy import open_stdio

    loop = asyncio.get_running_loop()
    transport = Mock()
    monkeypatch.setattr(loop, "connect_read_pipe", AsyncMock(return_value=(transport, None)))
    monkeypatch.setattr(loop, "connect_write_pipe", AsyncMock(side_effect=ValueError("unsupported pipe")))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=object()))
    import os
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb") as writer:
        with pytest.raises(ValueError, match="unsupported pipe"):
            await open_stdio(writer)
    transport.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_file_output_is_bounded_off_loop_and_preserves_errors() -> None:
    import threading
    from mimir.acp.proxy import _FileOutputWriter, MAX_FRAME_BYTES, ProxyError

    owner = threading.get_ident()
    class Output(io.BytesIO):
        def write(self, data: bytes) -> int:
            assert threading.get_ident() != owner
            return super().write(data)
    output = Output()
    writer = _FileOutputWriter(output)
    writer.write(b"frame\n")
    await writer.drain()
    assert output.getvalue() == b"frame\n"
    with pytest.raises(ProxyError):
        writer.write(b"x" * (MAX_FRAME_BYTES + 1))
    class Failed(Output):
        def write(self, data: bytes) -> int:
            raise OSError("disk full")
    failed = _FileOutputWriter(Failed())
    failed.write(b"frame\n")
    with pytest.raises(OSError, match="disk full"):
        await failed.drain()
    writer.close()
    with pytest.raises(BrokenPipeError):
        writer.write(b"after close")


def _assert_generation_empty(router: ProxyRouter) -> None:
    assert router._active_sessions == set()
    assert len(router._grants) == 0
    assert router._client_requests == {}
    assert router._client_routes == set()
    assert router._daemon_requests == {}
    assert router._local_requests == {}
    assert router._local_sessions == {}
    assert router._local_connections == {}
    assert router._daemon_tombstones == set()
    assert router._execution_permissions == {}
    assert router._execution_permission_tombstones == set()
    assert router._server_sessions == {}
    assert router._server_provider_sessions == {}
    assert router._connection_sessions == {}
    assert router._connection_provider_sessions == {}
    assert router._explicit_server_sessions == {}
    assert router._explicit_connection_sessions == {}
    assert router._used_server_ids == set()
    assert router._used_connection_ids == set()
    assert router._provider._connections == {}
    assert router._provider._processes == {}
    assert router._provider._python_kernels._processes == {}


@pytest.mark.asyncio
async def test_frame_delivery_is_bounded_and_terminal() -> None:
    stream = io.BytesIO()
    errors: list[BaseException] = []
    delivery = _FrameDelivery(stream, 16, errors.append)
    assert delivery.write(b"frame") == 5
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"frame"
    assert not errors


@pytest.mark.asyncio
async def test_frame_delivery_rejects_capacity() -> None:
    delivery = _FrameDelivery(io.BytesIO(), 2, lambda error: None)
    with pytest.raises(BufferError):
        delivery.write(b"long")
    with pytest.raises(BufferError):
        await delivery.wait_terminal()
    delivery.join()


class Partial(io.BytesIO):
    def write(self, data: bytes) -> int:
        return super().write(bytes(data[:2]))


@pytest.mark.asyncio
async def test_frame_delivery_handles_partial_writes() -> None:
    stream = Partial()
    delivery = _FrameDelivery(stream, 32, lambda error: None)
    delivery.write(b"complete")
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"complete"


@pytest.mark.asyncio
async def test_frame_delivery_sustained_ingress_is_ordered_and_bounded() -> None:
    stream = io.BytesIO()
    delivery = _FrameDelivery(stream, 16, lambda error: None)
    frames = [f"{index:03d}\n".encode() for index in range(200)]
    for payload in frames:
        while delivery.reserved_bytes > 11:
            await asyncio.sleep(0)
        delivery.write(payload)
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"".join(frames)
    assert delivery.peak_reserved_bytes <= 16


@pytest.mark.asyncio
async def test_frame_delivery_protocol_failures_reach_owner() -> None:
    class Broken(io.BytesIO):
        def write(self, data: bytes) -> int:
            raise OSError("sink failed")
    errors: list[BaseException] = []
    delivery = _FrameDelivery(Broken(), 64, errors.append)
    delivery.write(b"frame")
    delivery.finish()
    with pytest.raises(OSError, match="sink failed"):
        await delivery.wait_terminal()
    delivery.join()
    assert len(errors) == 1 and isinstance(errors[0], OSError)


@pytest.mark.asyncio
async def test_protocol_writer_uses_bounded_transport_close(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    async def close(writer: object) -> None:
        calls.append(writer)
    monkeypatch.setattr("mimir.acp.host.close_writer", close)
    writer = object()
    await close_protocol_writer(writer)
    assert calls == [writer]


@pytest.mark.asyncio
async def test_eof_stage_preserves_descriptor_ownership() -> None:
    class Writer:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.closed = False
        def write_eof(self) -> None: self.events.append("eof")
        async def drain(self) -> None: self.events.append("drain")
        def close(self) -> None: self.closed = True

    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = Writer()
    await pump_stream(reader, writer)
    assert writer.events == ["eof", "drain"]
    assert not writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["write", "flush"])
async def test_peer_disconnect_write_and_flush_are_reported(stage: str) -> None:
    class Sink(io.BytesIO):
        def write(self, data: bytes) -> int:
            if stage == "write": raise BrokenPipeError
            return super().write(data)
        def flush(self) -> None:
            if stage == "flush": raise ConnectionResetError
            super().flush()

    writer = _OutputWriter(Sink())
    with pytest.raises((BrokenPipeError, ConnectionResetError)):
        writer.write(b"frame\n")
    assert not writer.closed


@pytest.mark.asyncio
async def test_writer_close_uses_exact_finite_drain_close_and_abort_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []
    stages: list[str] = []

    async def wait_for(awaitable: object, timeout: float) -> object:
        timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError

    class Transport:
        def abort(self) -> None: stages.append("abort")

    class Writer:
        transport = Transport()
        async def drain(self) -> None: await asyncio.Future()
        def close(self) -> None: stages.append("close")
        async def wait_closed(self) -> None: await asyncio.Future()

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await close_writer(Writer())
    assert timeouts == [2.0, 1.0, 1.0]
    assert stages == ["close", "abort"]


@pytest.mark.asyncio
async def test_transport_death_tears_down_only_bound_generation(tmp_path: Path) -> None:
    class Peer:
        def __init__(self) -> None:
            self.disconnects: list[str] = []
        async def disconnect_mcp(self, connection_id: str) -> None:
            self.disconnects.append(connection_id)

    old_peer = Peer()
    new_peer = Peer()
    old_connection = ConnectionState(1, old_peer)
    new_connection = ConnectionState(2, new_peer)
    old_provider = SimpleNamespace(peer=old_peer, connection_id="old", closed=False)
    new_provider = SimpleNamespace(peer=new_peer, connection_id="new", closed=False)
    store = SessionStore(tmp_path)
    old_record = store.create_session("owner")
    new_record = store.create_session("owner")
    old_id, new_id = old_record.session_id, new_record.session_id
    old_state = SimpleNamespace(generation=1, active_prompt=None, provider=old_provider, record=old_record)
    successor = SimpleNamespace(generation=2, active_prompt=None, provider=new_provider, record=new_record)
    old_connection.connection_sessions["old"] = old_state
    new_connection.connection_sessions["new"] = successor
    agent = object.__new__(MimirAcpAgent)
    agent._connections = {1: old_connection, 2: new_connection}
    agent._connection = new_connection
    agent._client = new_peer
    agent._bridge = SimpleNamespace(_connected=True)
    agent._sessions = {old_id: old_state, new_id: successor}
    agent._environments = {old_id: (1, object()), new_id: (2, object())}
    agent._execution_keys = {old_id: 1, new_id: 2}
    agent._journals = JournalCache(store)
    old_journal = agent._journals.open(old_record, old_peer)
    new_journal = agent._journals.open(new_record, new_peer)
    agent._boundary_lock = asyncio.Lock()
    await agent.on_transport_closed(1)
    assert old_id not in agent._sessions
    assert agent._sessions[new_id] is successor
    assert agent._execution_keys == {new_id: 2}
    assert agent._journals._sessions == {new_id: new_journal}
    assert old_journal.current_client is None
    assert new_journal.current_client is new_peer
    assert agent._connection is new_connection
    assert new_provider.closed is False


@pytest.mark.asyncio
async def test_inbound_generation_identity_prevents_connection_id_collision() -> None:
    old_state = SimpleNamespace(generation=1, record=SimpleNamespace(session_id="old"))
    new_state = SimpleNamespace(generation=2, record=SimpleNamespace(session_id="new"))
    old_connection = ConnectionState(1, object())
    new_connection = ConnectionState(2, object())
    old_connection.connection_sessions["collision"] = old_state
    new_connection.connection_sessions["collision"] = new_state
    agent = object.__new__(MimirAcpAgent)
    agent._connections = {1: old_connection, 2: new_connection}
    observed: list[tuple[int, str]] = []
    async def revalidate(state: object) -> None:
        observed.append((state.generation, state.record.session_id))
    agent._revalidate_provider = revalidate
    await agent.on_mcp_notification(1, "collision", "notifications/tools/list_changed", None)
    await agent.on_mcp_notification(2, "collision", "notifications/tools/list_changed", None)
    await asyncio.gather(*old_connection.tasks, *new_connection.tasks)
    assert observed == [(1, "old"), (2, "new")]


@pytest.mark.asyncio
async def test_candidate_connection_does_not_retire_active_generation() -> None:
    agent = object.__new__(MimirAcpAgent)
    old = ConnectionState(1, object())
    agent._connection = old
    agent._connections = {1: old}
    agent._generation = 1
    agent._client = old.peer
    agent._auth_context = object()
    agent._display_name = "old"
    agent._bridge = SimpleNamespace(_connected=True)
    agent._active_prompts = {}
    agent._environments = {}
    agent._retirement_tasks = set()
    retired = asyncio.Event()
    async def retire(generation: int) -> None:
        retired.set()
    agent._retire_replaced_generation = retire
    candidate = object()
    generation = agent.on_connect(candidate)
    await asyncio.sleep(0)
    assert generation == 2
    assert agent._connection is old
    assert not retired.is_set()


@pytest.mark.asyncio
async def test_proxy_generation_teardown_retires_hosted_ids_grants_calls_and_workers(
    tmp_path: Path, lifecycle_shell: str,
) -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

    router = ProxyRouter(Writer(), Writer(), "secret")
    router._provider._request_unconfined_permission = _accept_unavailable_backend_risk_for_lifecycle
    router._active_sessions.add("session")
    router._grants.add("session", "hands_python")
    router._server_sessions["server"] = "session"
    router._connection_sessions["connection"] = "session"
    router._provider.bind_session("session", tmp_path)
    connection_id = router._provider.connect("session")
    async with asyncio.timeout(120):
        await router._provider.request(
            connection_id,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        await router._provider.notification(connection_id, "notifications/initialized")
        await router._provider.execute_python(router._provider._sessions["session"], "value = 1")
        worker = next(iter(router._provider._python_kernels._processes))
        shell_call = asyncio.create_task(
            router._provider.request(
                connection_id,
                "tools/call",
                {"name": "shell", "arguments": {"command": lifecycle_shell}},
                request_id="shell",
            )
        )
        while not router._provider._processes:
            await asyncio.sleep(0.01)
        shell = next(iter(router._provider._processes))
        router._fail_generation(ConnectionError("generation retired"))
        failure = await router.wait_failed()
        assert str(failure) == "generation retired"
        assert router._active_sessions == set()
        assert len(router._grants) == 0
        assert router._server_sessions == {}
        assert router._connection_sessions == {}
        assert router._used_server_ids == set()
        assert router._used_connection_ids == set()
        assert router._local_requests == {}
        assert router._daemon_requests == {}
        assert router._provider._connections == {}
        assert router._provider._processes == {}
        assert router._provider._python_kernels._processes == {}
        assert shell.returncode is not None
        assert worker.returncode is not None
        assert shell_call.done()
        await asyncio.gather(shell_call, return_exceptions=True)
        await router.close()


@pytest.mark.asyncio
async def test_daemon_eof_retires_generation_before_client_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lifecycle_shell: str,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def is_closing(self) -> bool:
            return self.closed

        async def wait_closed(self) -> None:
            return None

    client_writer = Writer()
    daemon_writer = Writer()
    router = ProxyRouter(client_writer, daemon_writer, "secret")
    router._provider._request_unconfined_permission = _accept_unavailable_backend_risk_for_lifecycle
    router._active_sessions.add("session")
    router._grants.add("session", "hands_python")
    router._server_sessions["server"] = "session"
    router._server_provider_sessions["server"] = "session"
    router._provider.bind_session("session", tmp_path)
    connection_id = router._provider.connect("session")
    router._connection_sessions[connection_id] = "session"
    router._connection_provider_sessions[connection_id] = "session"
    async with asyncio.timeout(120):
        await router._provider.request(
            connection_id,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        await router._provider.notification(connection_id, "notifications/initialized")
        await router._provider.execute_python(router._provider._sessions["session"], "value = 1")
        worker = next(iter(router._provider._python_kernels._processes))
        await router.route_daemon({
            "jsonrpc": "2.0",
            "id": "shell",
            "method": "mcp/message",
            "params": {
                "connectionId": connection_id,
                "method": "tools/call",
                "params": {"name": "shell", "arguments": {"command": lifecycle_shell}},
            },
        })
        while not router._provider._processes:
            await asyncio.sleep(0.01)
        shell = next(iter(router._provider._processes))
        client_reader = asyncio.StreamReader()
        daemon_reader = asyncio.StreamReader()
        monkeypatch.setattr(
            "mimir.acp.proxy.ProxyRouter",
            lambda client, daemon, credential, timeout_seconds=60: router,
        )
        monkeypatch.setattr("mimir.acp.proxy.PEER_EOF_GRACE_TIMEOUT", 30.0)
        running = asyncio.create_task(
            run_router(
                client_reader,
                client_writer,
                daemon_reader,
                daemon_writer,
                "secret",
            )
        )
        daemon_reader.feed_eof()
        async with asyncio.timeout(5):
            while (
                router._active_sessions
                or len(router._grants)
                or router._server_sessions
                or router._connection_sessions
                or router._local_requests
                or router._daemon_requests
                or router._provider._connections
                or router._provider._processes
                or router._provider._python_kernels._processes
            ):
                await asyncio.sleep(0.01)
        assert not running.done()
        assert router._active_sessions == set()
        assert len(router._grants) == 0
        assert router._server_sessions == {}
        assert router._connection_sessions == {}
        assert router._local_requests == {}
        assert router._daemon_requests == {}
        assert router._provider._connections == {}
        assert shell.returncode is not None
        assert worker.returncode is not None
        client_reader.feed_eof()
        await asyncio.wait_for(running, 5)


@pytest.mark.asyncio
async def test_daemon_eof_quiesces_inflight_allow_session_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def is_closing(self) -> bool:
            return self.closed

        async def wait_closed(self) -> None:
            return None

    class BlockingWriter(Writer):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.blocked = False

        async def drain(self) -> None:
            if self.blocked:
                return
            self.blocked = True
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    client_writer = Writer()
    daemon_writer = BlockingWriter()
    router = ProxyRouter(client_writer, daemon_writer, "secret")
    router._active_sessions.add("session")
    request = {
        "jsonrpc": "2.0",
        "id": "permission",
        "method": "session/request_permission",
        "params": {
            "sessionId": "session",
            "toolCall": {
                "toolCallId": "call",
                "title": "hands_edit",
                "kind": "other",
                "status": "pending",
                "rawInput": {"path": "note", "old_text": "old", "new_text": "new"},
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow_session", "name": "Allow for this session", "kind": "allow_always"},
                {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
            ],
            "_meta": {"mimir.wrapper": "hands_edit"},
        },
    }
    await router.route_daemon(request)
    response = {
        "jsonrpc": "2.0",
        "id": "permission",
        "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
    }
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(json.dumps(response).encode() + b"\n")
    daemon_reader = asyncio.StreamReader()
    monkeypatch.setattr(
        "mimir.acp.proxy.ProxyRouter",
        lambda client, daemon, credential, timeout_seconds=60: router,
    )
    monkeypatch.setattr("mimir.acp.proxy.PEER_EOF_GRACE_TIMEOUT", 30.0)
    running = asyncio.create_task(
        run_router(client_reader, client_writer, daemon_reader, daemon_writer, "secret")
    )
    await asyncio.wait_for(daemon_writer.entered.wait(), 5)
    daemon_reader.feed_eof()
    await asyncio.wait_for(daemon_writer.cancelled.wait(), 5)
    assert not running.done()
    client_reader.feed_eof()
    await asyncio.wait_for(running, 5)
    _assert_generation_empty(router)
    daemon_writer.release.set()
    await asyncio.sleep(0)
    _assert_generation_empty(router)


@pytest.mark.asyncio
async def test_close_cancels_inflight_allow_session_response_before_clearing_grants() -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

    class BlockingWriter(Writer):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def drain(self) -> None:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    daemon_writer = BlockingWriter()
    router = ProxyRouter(Writer(), daemon_writer, "secret")
    router._active_sessions.add("session")
    request = {
        "jsonrpc": "2.0",
        "id": "permission",
        "method": "session/request_permission",
        "params": {
            "sessionId": "session",
            "toolCall": {
                "toolCallId": "call",
                "title": "hands_edit",
                "kind": "other",
                "status": "pending",
                "rawInput": {"path": "note", "old_text": "old", "new_text": "new"},
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow_session", "name": "Allow for this session", "kind": "allow_always"},
                {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
            ],
            "_meta": {"mimir.wrapper": "hands_edit"},
        },
    }
    await router.route_daemon(request)
    routing = asyncio.create_task(router.route_client({
        "jsonrpc": "2.0",
        "id": "permission",
        "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
    }))
    await asyncio.wait_for(daemon_writer.entered.wait(), 5)

    await asyncio.wait_for(router.close(), 5)
    assert daemon_writer.cancelled.is_set()
    assert routing.cancelled()
    assert router._close_complete is True
    assert len(router._grants) == 0

    daemon_writer.release.set()
    await asyncio.sleep(0)
    assert len(router._grants) == 0


@pytest.mark.asyncio
async def test_daemon_eof_quiesces_inflight_session_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def is_closing(self) -> bool:
            return self.closed

        async def wait_closed(self) -> None:
            return None

    client_writer = Writer()
    daemon_writer = Writer()
    router = ProxyRouter(client_writer, daemon_writer, "secret")
    router._active_sessions.add("session")
    router._grants.add("session", "hands_python")
    router._provider.bind_session("session", tmp_path)
    router._server_sessions["server"] = "session"
    router._server_provider_sessions["server"] = "session"
    original_retire = router._retire_session
    transition_entered = asyncio.Event()
    transition_release = asyncio.Event()

    async def pause_after_retirement(session_id: str) -> None:
        await original_retire(session_id)
        transition_entered.set()
        await transition_release.wait()

    router._retire_session = pause_after_retirement
    load = {
        "jsonrpc": "2.0",
        "id": "load",
        "method": "session/load",
        "params": {"cwd": str(tmp_path), "sessionId": "session", "mcpServers": []},
    }
    client_reader = asyncio.StreamReader()
    client_reader.feed_data(json.dumps(load).encode() + b"\n")
    daemon_reader = asyncio.StreamReader()
    monkeypatch.setattr(
        "mimir.acp.proxy.ProxyRouter",
        lambda client, daemon, credential, timeout_seconds=60: router,
    )
    monkeypatch.setattr("mimir.acp.proxy.PEER_EOF_GRACE_TIMEOUT", 30.0)
    running = asyncio.create_task(
        run_router(client_reader, client_writer, daemon_reader, daemon_writer, "secret")
    )
    await asyncio.wait_for(transition_entered.wait(), 5)
    daemon_reader.feed_eof()
    async with asyncio.timeout(5):
        while not router._close_complete:
            await asyncio.sleep(0)
    assert not running.done()
    client_reader.feed_eof()
    await asyncio.wait_for(running, 5)
    _assert_generation_empty(router)
    transition_release.set()
    await asyncio.sleep(0)
    _assert_generation_empty(router)
