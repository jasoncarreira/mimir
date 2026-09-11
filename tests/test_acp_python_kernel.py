from __future__ import annotations

import ast
import asyncio
import builtins
import json
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mimir.acp.python_kernel as kernel
from mimir.acp.python_kernel import PythonKernelManager, PythonKernelUnavailable


pytestmark = pytest.mark.timeout(120)


async def _stopped(pid: int) -> bool:
    while True:
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError, IndexError):
            return True
        if state == "Z":
            return True
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_stopped_handles_process_exit_during_proc_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def exited(path: Path) -> str:
        raise ProcessLookupError("process exited after opening stat")

    monkeypatch.setattr(Path, "read_text", exited)
    assert await _stopped(123)


async def _appears(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


@pytest.fixture
def execution_timers(monkeypatch):
    timers = []

    def timeout_at(deadline):
        timer = asyncio.timeout(None)
        timers.append(timer)
        return timer

    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "timeout_at": timeout_at,
    }))
    return timers


@pytest.mark.asyncio
async def test_repl_executes_statements_and_reprs_final_expression(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        result = await manager.execute("one", tmp_path, "value = 40\nvalue + 2")
        assert result == {
            "ok": True,
            "stdout": "",
            "stderr": "",
            "value": "42",
            "exception": "",
            "timedOut": False,
            "kernel": "fresh",
        }
        reused = await manager.execute("one", tmp_path, "value")
        assert reused["value"] == "40"
        assert reused["kernel"] == "reused"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_exception_preserves_partial_namespace_and_traceback(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        failed = await manager.execute("one", tmp_path, "kept = 7\n1 / 0")
        assert failed["ok"] is False
        assert failed["value"] == ""
        assert "ZeroDivisionError" in failed["exception"]
        assert "Traceback (most recent call last):" in failed["exception"]
        assert '<mimir-hands-python>' in failed["exception"]
        assert failed["kernel"] == "fresh"
        retained = await manager.execute("one", tmp_path, "kept")
        assert retained["value"] == "7"
        assert retained["kernel"] == "reused"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_stream_and_text_byte_bounds(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        result = await manager.execute(
            "one",
            tmp_path,
            "import os\nos.write(1, b'x' * 65540)\nos.write(2, b'y' * 65542)\n'v' * 16386",
        )
        assert result["stdout"] == "x" * 65_536 + "\n…[truncated 4 bytes]"
        assert result["stderr"] == "y" * 65_536 + "\n…[truncated 6 bytes]"
        assert result["value"] == "'" + "v" * 16_383 + "\n…[truncated 4 bytes]"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_multibyte_text_bound_keeps_codepoints_and_counts_raw_bytes(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    try:
        value = await manager.execute("value", tmp_path, repr("界" * 6_000))
        retained = value["value"].split("\n…[truncated ", 1)[0]
        assert len(retained.encode("utf-8")) <= kernel.TEXT_LIMIT_BYTES
        assert not retained.endswith("�")
        assert value["value"].endswith("[truncated 1618 bytes]")
        streams = await manager.execute(
            "value", tmp_path, "import os\nos.write(1, b'\\xff' * 65537)"
        )
        assert streams["stdout"].encode("utf-8").startswith("�".encode("utf-8"))
        assert streams["stdout"].endswith("[truncated 1 bytes]")
    finally:
        await manager.close()


def test_text_bound_never_splits_multibyte_or_invalid_codepoints() -> None:
    split = kernel._bounded_text("a" * 16_383 + "界", kernel.TEXT_LIMIT_BYTES)
    assert split == "a" * 16_383 + "\n…[truncated 3 bytes]"
    invalid = kernel._bounded_text("a" * 16_383 + "\udcff", kernel.TEXT_LIMIT_BYTES)
    assert invalid == "a" * 16_383 + "?"
    assert len(invalid.encode("utf-8")) == kernel.TEXT_LIMIT_BYTES


def test_traceback_format_exc_runs_inside_active_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = kernel.traceback.format_exc
    observed = False

    def format_exc() -> str:
        nonlocal observed
        observed = True
        assert sys.exception() is not None
        return original()

    monkeypatch.setattr(kernel.traceback, "format_exc", format_exc)
    ok, value, exception = kernel._execute(
        "raise RuntimeError('boom')",
        {"__name__": "__main__", "__builtins__": builtins},
    )
    assert observed is True
    assert ok is False
    assert value == ""
    assert exception.endswith("RuntimeError: boom\n")


@pytest.mark.asyncio
async def test_exception_utf8_bound_and_omitted_byte_count_are_exact(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    try:
        baseline = await manager.execute("baseline", tmp_path, "raise RuntimeError('')")
        result = await manager.execute(
            "baseline", tmp_path, "raise RuntimeError('界' * 6000)"
        )
        retained, marker = result["exception"].rsplit("\n…[truncated ", 1)
        omitted = int(marker.removesuffix(" bytes]"))
        baseline_bytes = baseline["exception"].encode("utf-8")
        expected_total = (
            len(baseline_bytes)
            - len("RuntimeError\n".encode("utf-8"))
            + len("RuntimeError: \n".encode("utf-8"))
            + len(("界" * 6000).encode("utf-8"))
        )
        assert len(retained.encode("utf-8")) <= kernel.TEXT_LIMIT_BYTES
        assert "�" not in retained
        assert len(retained.encode("utf-8")) + omitted == expected_total
        assert result["ok"] is False
        assert result["kernel"] == "reused"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_timeout_and_crash_discard_namespace(tmp_path: Path, execution_timers) -> None:
    manager = PythonKernelManager()
    task = None
    try:
        assert (await manager.execute("timeout", tmp_path, "1", 5))["ok"] is True
        task = asyncio.create_task(manager.execute(
            "timeout", tmp_path,
            "import pathlib,signal\nmarker = 1\npathlib.Path('entered').touch()\nsignal.pause()", 3
        ))
        await _appears(tmp_path / "entered")
        assert not task.done()
        execution_timers[-1].reschedule(0)
        timed_out = await task
        assert timed_out == {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "value": "",
            "exception": "execution timed out after 3 seconds; namespace state lost",
            "timedOut": True,
            "kernel": "timed_out",
        }
        assert (await manager.execute("timeout", tmp_path, "globals().get('marker')"))[
            "kernel"
        ] == "fresh"
        crashed = await manager.execute("timeout", tmp_path, "import os\nos._exit(23)")
        assert crashed["kernel"] == "crashed"
        assert crashed["exception"] == (
            "kernel process exited with code 23; namespace state lost"
        )
        assert crashed["stdout"] == ""
        assert (await manager.execute("timeout", tmp_path, "1"))["kernel"] == "fresh"
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_timeout_and_crash_retain_streams_exactly(tmp_path: Path, execution_timers) -> None:
    manager = PythonKernelManager()
    task = None
    try:
        assert (await manager.execute("timeout", tmp_path, "1", 5))["ok"] is True
        task = asyncio.create_task(manager.execute(
            "timeout",
            tmp_path,
            "import os,signal,pathlib\nos.write(1,b'before-timeout')\nos.write(2,b'err-timeout')\npathlib.Path('entered').touch()\nsignal.pause()",
            3,
        ))
        await _appears(tmp_path / "entered")
        assert not task.done()
        execution_timers[-1].reschedule(0)
        timed_out = await task
        assert timed_out["kernel"] == "timed_out"
        assert timed_out["stdout"] == "before-timeout"
        assert timed_out["stderr"] == "err-timeout"
        assert timed_out["exception"] == (
            "execution timed out after 3 seconds; namespace state lost"
        )
        crashed = await manager.execute(
            "timeout",
            tmp_path,
            "import os\nos.write(1,b'before-crash')\nos.write(2,b'err-crash')\nos._exit(31)",
        )
        assert crashed == {
            "ok": False,
            "stdout": "before-crash",
            "stderr": "err-crash",
            "value": "",
            "exception": "kernel process exited with code 31; namespace state lost",
            "timedOut": False,
            "kernel": "crashed",
        }
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_crash_result_survives_killpg_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PythonKernelManager()
    denied_groups: list[int] = []
    terminating: list[kernel._Worker] = []
    terminate = manager._terminate

    async def observed_terminate(worker: kernel._Worker) -> None:
        terminating.append(worker)
        try:
            await terminate(worker)
        finally:
            terminating.pop()

    monkeypatch.setattr(manager, "_terminate", observed_terminate)

    def denied(pgid: int, sig: int) -> None:
        # A call from synchronous kill_owned_process_groups cannot satisfy this
        # regression: identify the exact _terminate worker and signal site.
        assert len(terminating) == 1
        assert terminating[0].pgid == pgid
        assert terminating[0].signalled is False
        assert sig == 9
        denied_groups.append(pgid)
        raise PermissionError(1, "Operation not permitted")

    try:
        await manager.execute("crash", tmp_path, "marker = 42")
        pid = manager.kernels()[0]["pid"]
        # The worker exits itself; deny only cleanup's group signal, without
        # leaving a live worker that the patched killpg cannot terminate.
        with monkeypatch.context() as patch:
            patch.setattr(kernel.os, "killpg", denied)
            result = await manager.execute(
                "crash", tmp_path,
                "import os\nos.write(1, b'before-crash')\n"
                "os.write(2, b'err-crash')\nos._exit(31)",
            )
        assert denied_groups == [pid]
        assert result == {
            "ok": False,
            "stdout": "before-crash",
            "stderr": "err-crash",
            "value": "",
            "exception": "kernel process exited with code 31; namespace state lost",
            "timedOut": False,
            "kernel": "crashed",
        }
        fresh = await manager.execute("crash", tmp_path, "globals().get('marker')")
        assert fresh["kernel"] == "fresh"
        assert fresh["value"] == "None"
    finally:
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_live_control_eof_is_killed_without_waiting_for_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PythonKernelManager()
    trace = []
    receive = manager._receive

    async def observed_receive(channel):
        try:
            result = await receive(channel)
        except EOFError:
            assert next(iter(manager._processes)).returncode is None
            trace.append("live EOF")
            raise
        assert result == {"ready": True}
        trace.append("cold handshake")
        return result

    monkeypatch.setattr(manager, "_receive", observed_receive)
    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "timeout_at": lambda deadline: asyncio.timeout(None),
    }))
    try:
        result = await manager.execute(
            "eof",
            tmp_path,
            "import os,stat,signal\nfor fd in range(3,256):\n try:\n  if stat.S_ISSOCK(os.fstat(fd).st_mode): os.close(fd)\n except OSError: pass\nsignal.pause()",
            120,
        )
        assert trace == ["cold handshake", "live EOF"]
        assert result["kernel"] == "crashed"
        assert result["exception"] == (
            "kernel process exited with code -9; namespace state lost"
        )
        assert manager._processes == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_direct_exit_still_kills_owned_process_group_descendant(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    identity = tmp_path / "descendant"
    # Existence is the parent's readiness signal; publish only a complete PID
    # so process-group cleanup cannot interrupt the fixture's write.
    child_code = (
        "import os,signal,pathlib;"
        f"identity=pathlib.Path({str(identity)!r});"
        "identity.with_suffix('.tmp').write_text(str(os.getpid()));"
        "identity.with_suffix('.tmp').replace(identity);"
        "signal.pause()"
    )
    code = (
        "import os,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}],close_fds=True)\n"
        "while not os.path.exists(" + repr(str(identity)) + "): time.sleep(.01)\n"
        "os._exit(37)"
    )
    try:
        result = await manager.execute("descendant", tmp_path, code)
        pid = int(identity.read_text())
        assert result["exception"] == (
            "kernel process exited with code 37; namespace state lost"
        )
        assert await _stopped(pid)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sessions_parallel_and_namespaces_isolated(tmp_path: Path) -> None:
    first_cwd = tmp_path / "project"
    second_cwd = tmp_path / "project-other"
    first_cwd.mkdir()
    second_cwd.mkdir()
    manager = PythonKernelManager()
    tasks = []
    try:
        for name, cwd, value in (("one", first_cwd, 1), ("two", second_cwd, 2)):
            tasks.append(asyncio.create_task(manager.execute(
                name, cwd,
                "import pathlib,time\npathlib.Path('entered').touch()\n"
                "while not pathlib.Path('release').exists(): time.sleep(.01)\n"
                f"value = {value}", 120,
            )))
        await _appears(first_cwd / "entered")
        await _appears(second_cwd / "entered")
        assert all(not task.done() for task in tasks)
        (first_cwd / "release").touch()
        (second_cwd / "release").touch()
        first, second = await asyncio.gather(*tasks)
        assert first["kernel"] == second["kernel"] == "fresh"
        values = await asyncio.gather(
            manager.execute("one", first_cwd, "value"),
            manager.execute("two", second_cwd, "value"),
        )
        assert [item["value"] for item in values] == ["1", "2"]
        assert set(manager._kernels) == {str(first_cwd.resolve()), str(second_cwd.resolve())}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_release_reuses_live_namespace_through_canonical_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.acp.hosted import HostedHandsProvider

    prepare = kernel.prepare_command

    def require_canonical(argv, *, cwd, **kwargs):
        # Keep canonical-path wiring observable on the portable test backend too.
        assert cwd == cwd.resolve()
        return prepare(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(kernel, "prepare_command", require_canonical)

    cwd = tmp_path / "project"
    cwd.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    provider = HostedHandsProvider()
    # Binding, not execution, freezes canonical cwd. Going through the real
    # entrypoint preserves the backend's rejection of later path replacement.
    provider.bind_session("one", alias)
    provider.bind_session("two", cwd)
    one, two = provider._sessions["one"], provider._sessions["two"]
    assert one.cwd == two.cwd == cwd.resolve()
    manager = provider._python_kernels
    try:
        first = await provider.execute_python(
            one,
            "import math\nvalue = 81\ndef root(): return math.sqrt(value)\n"
            "transform = lambda x: root() + x",
        )
        assert first["ok"] is True
        state = manager._kernels[str(cwd.resolve())]
        worker = state.worker
        assert worker is not None
        await manager.release("one")
        assert state.owner is None
        assert state.worker is worker and worker.process.returncode is None
        second = await provider.execute_python(two, "(root(), transform(3), math.factorial(4))")
        assert second["kernel"] == "reused"
        assert second["value"] == "(9.0, 12.0, 24)"
        assert manager._kernels == {str(cwd.resolve()): state}
        assert state.owner == "two"
        await manager.release("one")
        assert state.owner == "two"
        assert (await provider.execute_python(two, "transform(1)"))["value"] == "10.0"
    finally:
        await provider.close()


def test_bound_symlink_cwd_stays_frozen_and_replacement_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.acp.confinement import ConfinementUnavailable, SeatbeltBackend
    from mimir.acp.hosted import HostedHandsProvider

    cwd = (tmp_path / "project").resolve()
    outside = (tmp_path / "outside").resolve()
    cwd.mkdir()
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    provider = HostedHandsProvider()
    provider.bind_session("one", alias)
    session = provider._sessions["one"]
    assert session.cwd == cwd
    # Only profile construction runs here; this does not execute Seatbelt or
    # claim OS confinement on Linux. Use an existing executable for its preflight.
    monkeypatch.setattr(SeatbeltBackend, "executable", Path(sys.executable))
    backend = SeatbeltBackend()
    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)
    prepared = backend.prepare([sys.executable], cwd=session.cwd)
    assert f"(subpath {json.dumps(str(cwd))})" in prepared.argv[2]
    assert f"(subpath {json.dumps(str(outside))})" not in prepared.argv[2]
    cwd.rename(cwd.with_name("original"))
    cwd.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfinementUnavailable, match="changed identity"):
        backend.prepare([sys.executable], cwd=session.cwd)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["value = -1", "%kernel kill", "%kernel release"])
@pytest.mark.parametrize("active", [False, True], ids=["idle-owned", "executing"])
async def test_other_session_cannot_execute_kill_or_release_owned_cwd(
    tmp_path: Path, command: str, active: bool,
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    manager = PythonKernelManager()
    task = None
    try:
        await manager.execute("owner", tmp_path, "value = 42")
        state = manager._kernels[str(tmp_path.resolve())]
        worker = state.worker
        if active:
            task = asyncio.create_task(manager.execute(
                "owner", tmp_path,
                "import pathlib,time\npathlib.Path('entered').touch()\n"
                "while not pathlib.Path('finish').exists(): time.sleep(.01)",
            ))
            await _appears(tmp_path / "entered")
        with pytest.raises(PythonKernelUnavailable, match="owned.*owner.*refused"):
            await manager.execute("intruder", alias, command)
        await manager.release("intruder")
        assert state.owner == "owner"
        assert state.worker is worker
        assert worker is not None and worker.process.returncode is None
        if task is not None:
            (tmp_path / "finish").touch()
            assert (await task)["ok"] is True
        result = await manager.execute("owner", tmp_path, "value")
        assert result["kernel"] == "reused"
        assert result["value"] == "42"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_kernels_command_after_close_is_rejected(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    await manager.close()
    with pytest.raises(PythonKernelUnavailable, match="^kernel manager is closed$"):
        await manager.execute("one", tmp_path, "%kernels")


class _ObservedLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_control_command_waiting_admission_is_rejected_when_close_begins(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    admission = manager._admission = _ObservedLock()
    tasks = []
    try:
        async with admission:
            command = asyncio.create_task(
                manager.execute("one", tmp_path, "%kernel release")
            )
            tasks.append(command)
            await admission.waiting.wait()
            assert not manager._closed
            admission.waiting.clear()
            closing = asyncio.create_task(manager.close())
            tasks.append(closing)
            await admission.waiting.wait()
            assert manager._closed
            assert not command.done()
        with pytest.raises(PythonKernelUnavailable, match="^kernel manager is closed$"):
            await command
        await closing
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_execution_waiting_state_lock_is_rejected_when_close_begins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PythonKernelManager()
    admission = manager._admission = _ObservedLock()
    lock = _ObservedLock()
    state = kernel._Kernel(lock=lock)
    manager._kernels[str(tmp_path.resolve())] = state
    tasks = []

    async def unexpected_spawn(*args: object) -> None:
        pytest.fail("queued execution must not spawn a worker after close begins")

    monkeypatch.setattr(manager, "_spawn", unexpected_spawn)
    try:
        async with lock:
            execution = asyncio.create_task(manager.execute("one", tmp_path, "42"))
            tasks.append(execution)
            await lock.waiting.wait()
            assert state.waiters == 1
            assert state.owner == "one"
            assert not manager._closed
            # Hold close at admission until execution checks the closed flag.
            await admission.acquire()
            closing = asyncio.create_task(manager.close())
            tasks.append(closing)
            await admission.waiting.wait()
            assert manager._closed
            assert not execution.done()
        with pytest.raises(PythonKernelUnavailable, match="^kernel manager is closed$"):
            await execution
        assert state.waiters == 0
        assert not lock.locked()
        assert state.worker is None
        admission.release()
        await closing
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if admission.locked():
            admission.release()
        await manager.close()


@pytest.mark.asyncio
async def test_operator_listing_release_and_kill(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        assert json.loads((await manager.execute("one", tmp_path, "%kernels"))["value"]) == []
        assert manager._kernels == manager._processes == {}
        await manager.execute("one", tmp_path, "value = 42")
        state = manager._kernels[str(tmp_path.resolve())]
        worker = state.worker
        assert worker is not None
        listing = json.loads((await manager.execute("observer", tmp_path, "%kernels"))["value"])
        assert len(listing) == 1
        assert listing[0]["cwd"] == str(tmp_path.resolve())
        assert listing[0]["owner"] == "one"
        assert listing[0]["pid"] == worker.process.pid
        assert listing[0]["idle_seconds"] >= 0
        assert (await manager.execute("one", tmp_path, "%kernel release"))["ok"] is True
        assert state.owner is None and state.worker is worker
        assert (await manager.execute("two", tmp_path, "value"))["value"] == "42"
        assert (await manager.execute("two", tmp_path, "%kernel kill"))["ok"] is True
        assert manager._kernels == manager._processes == {}
        assert await _stopped(worker.process.pid)
        result = await manager.execute("three", tmp_path, "globals().get('value')")
        assert result["kernel"] == "fresh"
        assert result["value"] == "None"
        await manager.retire(tmp_path)
        assert manager._kernels == manager._processes == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_capacity_refuses_when_all_kernels_owned(tmp_path: Path) -> None:
    assert kernel.MAX_KERNELS == 8
    manager = PythonKernelManager()
    try:
        for index in range(kernel.MAX_KERNELS):
            cwd = tmp_path / str(index)
            cwd.mkdir()
            assert (await manager.execute(str(index), cwd, f"value = {index}"))["ok"] is True
        original = {key: state.worker for key, state in manager._kernels.items()}
        extra = tmp_path / "extra"
        extra.mkdir()
        with pytest.raises(PythonKernelUnavailable, match="capacity.*owned.*refused"):
            await manager.execute("extra", extra, "42")
        assert {key: state.worker for key, state in manager._kernels.items()} == original
        assert len(manager._processes) == kernel.MAX_KERNELS
        for index in range(kernel.MAX_KERNELS):
            state = manager._kernels[str((tmp_path / str(index)).resolve())]
            assert state.owner == str(index)
            result = await manager.execute(str(index), tmp_path / str(index), "value")
            assert result["kernel"] == "reused"
            assert result["value"] == str(index)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_capacity_evicts_least_active_detached_kernel_and_descendant(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        for index in range(kernel.MAX_KERNELS):
            cwd = tmp_path / str(index)
            cwd.mkdir()
            assert (await manager.execute(str(index), cwd, f"value = {index}"))["ok"] is True
        victim_cwd = tmp_path / "2"
        # Ignore SIGCHLD so the worker does not retain exited children as zombies.
        spawned = await manager.execute(
            "2", victim_cwd,
            "import os,signal,subprocess,sys\nsignal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])\n"
            "(child.pid, os.getpgid(child.pid))",
        )
        child_pid, child_pgid = ast.literal_eval(spawned["value"])
        victim = manager._kernels[str(victim_cwd.resolve())]
        worker = victim.worker
        assert worker is not None and child_pgid == worker.pgid
        await manager.release("1")
        await manager.release("2")
        # Make LRU differ from insertion order, release order, and oldest owned entry.
        now = asyncio.get_running_loop().time()
        manager._kernels[str((tmp_path / "0").resolve())].last_activity = now - 30
        manager._kernels[str((tmp_path / "1").resolve())].last_activity = now - 10
        victim.last_activity = now - 20
        extra = tmp_path / "extra"
        extra.mkdir()
        assert (await manager.execute("extra", extra, "42"))["kernel"] == "fresh"
        assert len(manager._kernels) == kernel.MAX_KERNELS
        assert str(victim_cwd.resolve()) not in manager._kernels
        assert victim.worker is None
        assert worker.process not in manager._processes
        assert await _stopped(worker.process.pid)
        assert await _stopped(child_pid)
        while True:
            try:
                os.killpg(worker.pgid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        for index in (0, 1):
            result = await manager.execute(str(index), tmp_path / str(index), "value")
            assert result["kernel"] == "reused"
            assert result["value"] == str(index)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_function_import_and_loaded_data_persist(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("loaded")
    manager = PythonKernelManager()
    try:
        first = await manager.execute(
            "one",
            tmp_path,
            "import pathlib\ndata = pathlib.Path('data.txt').read_text()\ndef render(): return data.upper()",
        )
        second = await manager.execute("one", tmp_path, "render()")
        assert first["ok"] is True
        assert second["value"] == "'LOADED'"
        assert second["kernel"] == "reused"
    finally:
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_queue_wait_is_outside_timeout_and_other_session_is_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.acp.execution_scope import ScopeApproval

    # Direct manager callers supply the canonical cwd frozen by session binding.
    tmp_path = tmp_path.resolve()
    manager = PythonKernelManager()
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    # Each project needs explicit access to the two coordination files outside
    # its cwd; do not make this concurrency test depend on an unconfined backend.
    markers = (tmp_path / "parallel-one", tmp_path / "parallel-two")
    for marker in markers:
        marker.touch()
    grants = tuple(ScopeApproval(marker, False) for marker in markers)
    parallel_one = tmp_path / "one"
    parallel_two = tmp_path / "two"
    parallel_one.mkdir()
    parallel_two.mkdir()
    loop = asyncio.get_running_loop()
    now = 1000.0
    queued = None
    queued_reads = []
    tasks = []

    class Clock:
        def time(self):
            if asyncio.current_task() is queued:
                queued_reads.append(now)
            return now

        def __getattr__(self, name):
            return getattr(loop, name)

    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "get_running_loop": lambda: Clock(),
        "timeout_at": lambda deadline: asyncio.timeout(0 if deadline <= now else None),
    }))
    try:
        blocker = asyncio.create_task(
            manager.execute(
                "one",
                tmp_path,
                "import pathlib,time\npathlib.Path('entered').write_text('yes')\n"
                "while not pathlib.Path('release').exists(): time.sleep(.01)",
                120,
            )
        )
        tasks.append(blocker)
        while not entered.exists():
            await asyncio.sleep(0)
        state = manager._kernels[str(tmp_path)]
        # The active call already holds this lock; observe its real queued waiter.
        lock = state.lock
        queued = asyncio.create_task(
            manager.execute("one", tmp_path, "2", 1)
        )
        tasks.append(queued)
        while state.waiters != 1:
            await asyncio.sleep(0)
        assert lock.locked()
        now += 2  # More than the queued call's budget, without spending wall time.
        assert queued_reads == []
        assert not queued.done()
        release.write_text("yes")
        assert (await blocker)["ok"]
        queued_result = await queued
        assert queued_result["value"] == "2"
        assert queued_result["timedOut"] is False
        assert queued_reads[0] == 1002.0

        first, second = await asyncio.gather(
            manager.execute(
                "parallel-one",
                parallel_one,
                f"import pathlib,time\npathlib.Path({str(markers[0])!r}).write_text('yes')\nwhile pathlib.Path({str(markers[1])!r}).read_text() != 'yes': time.sleep(.01)\n1",
                120,
                approved_paths=grants,
            ),
            manager.execute(
                "parallel-two",
                parallel_two,
                f"import pathlib,time\npathlib.Path({str(markers[1])!r}).write_text('yes')\nwhile pathlib.Path({str(markers[0])!r}).read_text() != 'yes': time.sleep(.01)\n2",
                120,
                approved_paths=grants,
            ),
        )
        assert first["value"] == "1"
        assert second["value"] == "2"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_queued_and_active_cancellation_have_distinct_worker_effects(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    tasks = []
    try:
        assert (await manager.execute("one", tmp_path, "value = 4"))["ok"]
        state = manager._kernels[str(tmp_path.resolve())]
        worker = state.worker
        lock = state.lock = _ObservedLock()
        active = asyncio.create_task(
            manager.execute("one", tmp_path,
                            "import pathlib,time\npathlib.Path('entered').touch()\n"
                            "while not pathlib.Path('release').exists(): time.sleep(.01)",
                            120)
        )
        tasks.append(active)
        while not (tmp_path / "entered").exists():
            await asyncio.sleep(0)
        queued = asyncio.create_task(manager.execute("one", tmp_path, "value"))
        tasks.append(queued)
        await lock.waiting.wait()
        assert state.waiters == 1
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert state.waiters == 0
        assert state.worker is worker and worker.process.returncode is None
        assert not active.done()
        (tmp_path / "release").touch()
        assert (await active)["ok"]
        retained = await manager.execute("one", tmp_path, "value")
        assert retained["kernel"] == "reused" and retained["value"] == "4"

        cancelled = asyncio.create_task(
            manager.execute("one", tmp_path,
                            "pathlib.Path('cancelling').touch()\n"
                            "while True: time.sleep(1)", 120)
        )
        tasks.append(cancelled)
        while not (tmp_path / "cancelling").exists():
            await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert manager._processes == {}
        assert (await manager.execute("one", tmp_path, "1"))["kernel"] == "fresh"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_worker_and_idle_task_are_lazy(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    assert manager._kernels == {}
    assert manager._processes == {}
    assert kernel.IDLE_SECONDS == 1_800
    first = asyncio.create_task(
        manager.execute("one", tmp_path,
                        "import pathlib,time\npathlib.Path('entered').touch()\n"
                        "while not pathlib.Path('release').exists(): time.sleep(.01)\n"
                        "sequence = [1]", 120)
    )
    tasks = [first]
    try:
        while not (tmp_path / "entered").exists():
            await asyncio.sleep(0)
        state = manager._kernels[str(tmp_path.resolve())]
        assert state.idle_task is None
        second = asyncio.create_task(manager.execute("one", tmp_path, "sequence.append(2)\nsequence"))
        tasks.append(second)
        while state.waiters != 1:
            await asyncio.sleep(0)
        assert not first.done() and not second.done()
        (tmp_path / "release").touch()
        assert (await first)["value"] == ""
        assert (await second)["value"] == "[1, 2]"
        assert manager._kernels[str(tmp_path.resolve())].idle_task is not None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.fixture
def idle_clock(monkeypatch):
    class Clock:
        now = 1000.0

        def __init__(self):
            self.waits = asyncio.Queue()

        def time(self):
            return self.now

        def __getattr__(self, name):
            return getattr(asyncio.get_running_loop(), name)

        async def sleep(self, delay):
            release = asyncio.Event()
            self.waits.put_nowait((asyncio.current_task(), delay, release))
            await release.wait()

        async def expire(self, timer):
            task, delay, release = timer
            assert delay == kernel.IDLE_SECONDS == 1800
            self.now += delay
            release.set()
            await task

    clock = Clock()
    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "get_running_loop": lambda: clock,
        "sleep": clock.sleep, "timeout_at": lambda deadline: asyncio.timeout(None),
    }))
    return clock


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_idle_retirement_discards_worker_and_next_call_is_fresh(
    tmp_path: Path, idle_clock,
) -> None:
    manager = PythonKernelManager()
    try:
        await manager.execute("one", tmp_path, "value = 9")
        state = manager._kernels[str(tmp_path.resolve())]
        process = state.worker.process
        timer = await idle_clock.waits.get()
        assert timer[0] is state.idle_task
        assert state.worker.process.returncode is None
        await idle_clock.expire(timer)
        assert str(tmp_path.resolve()) not in manager._kernels
        assert state.worker is None
        assert await _stopped(process.pid)
        result = await manager.execute("one", tmp_path, "globals().get('value')")
        assert result["kernel"] == "fresh"
        assert result["value"] == "None"
    finally:
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_registered_waiter_wins_idle_retirement_race(
    tmp_path: Path, idle_clock,
) -> None:
    manager = PythonKernelManager()
    tasks = []
    try:
        await manager.execute("one", tmp_path, "value = 1")
        initial = await idle_clock.waits.get()
        active = asyncio.create_task(
            manager.execute("one", tmp_path,
                            "import pathlib,time\npathlib.Path('entered').touch()\n"
                            "while not pathlib.Path('release').exists(): time.sleep(.01)\nvalue")
        )
        tasks.append(active)
        while not (tmp_path / "entered").exists():
            await asyncio.sleep(0)
        await initial[0]
        state = manager._kernels[str(tmp_path.resolve())]
        assert state.idle_task is None
        waiter = asyncio.create_task(manager.execute("one", tmp_path, "value + 1"))
        tasks.append(waiter)
        while state.waiters != 1:
            await asyncio.sleep(0)
        assert not active.done() and not waiter.done()
        (tmp_path / "release").touch()
        assert (await active)["kernel"] == "reused"
        assert (await waiter)["value"] == "2"
        timer = await idle_clock.waits.get()
        assert timer[0] is state.idle_task
        await idle_clock.expire(timer)
        assert str(tmp_path.resolve()) not in manager._kernels
        assert (await manager.execute("one", tmp_path, "3"))["kernel"] == "fresh"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_real_waiter_registered_at_idle_expiry_keeps_worker(
    tmp_path: Path, idle_clock,
) -> None:
    manager = PythonKernelManager()
    waiter = None
    try:
        await manager.execute("one", tmp_path, "value = 9")
        state = manager._kernels[str(tmp_path.resolve())]
        worker = state.worker
        timer = await idle_clock.waits.get()
        assert timer[0] is state.idle_task
        await state.lock.acquire()
        waiter = asyncio.create_task(manager.execute("one", tmp_path, "value"))
        while state.waiters != 1:
            await asyncio.sleep(0)
        await idle_clock.expire(timer)
        assert state.worker is worker
        assert worker is not None and worker.process.returncode is None
        state.lock.release()
        result = await waiter
        assert result["kernel"] == "reused"
        assert result["value"] == "9"
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if manager._kernels.get(str(tmp_path.resolve())) is not None:
            state = manager._kernels[str(tmp_path.resolve())]
            if state.lock.locked():
                state.lock.release()
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_late_background_output_is_discarded_between_calls(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        first = await manager.execute(
            "one",
            tmp_path,
            "import threading,time,pathlib,os\n"
            "def late():\n"
            " while not pathlib.Path('release').exists(): time.sleep(.01)\n"
            " print('late',flush=True)\n"
            " discarded = os.fstat(1).st_rdev == os.stat(os.devnull).st_rdev\n"
            " pathlib.Path('emitted').write_text(str(discarded))\n"
            "threading.Thread(target=late,daemon=True).start()",
        )
        assert first["stdout"] == ""
        (tmp_path / "release").touch()
        while not (tmp_path / "emitted").exists():
            await asyncio.sleep(0)
        # Readiness follows the actual write, while no execution owns stdout.
        while not (destination := (tmp_path / "emitted").read_text()):
            await asyncio.sleep(0)
        assert destination == "True"
        second = await manager.execute("one", tmp_path, "print('current')")
        assert second["stdout"] == "current\n"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_launch_socket_modes_cwd_and_environment_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_spawn = asyncio.create_subprocess_exec
    observed: dict[str, object] = {}

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        observed["args"] = args
        observed["kwargs"] = kwargs.copy()
        passed = kwargs["pass_fds"]
        assert isinstance(passed, tuple) and len(passed) == 1
        mode = os.fstat(passed[0]).st_mode
        assert stat.S_ISSOCK(mode)
        probe = socket.fromfd(passed[0], socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            assert probe.family == socket.AF_UNIX
            assert probe.type == socket.SOCK_STREAM
        finally:
            probe.close()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("MIMIR_KERNEL_TEST_ENV", "inherited")
    manager = PythonKernelManager()
    modes: list[int] = []
    real_output = manager._output_path

    def output_path(directory: Path) -> Path:
        path = real_output(directory)
        modes.append(stat.S_IMODE(path.stat().st_mode))
        return path

    monkeypatch.setattr(manager, "_output_path", output_path)
    try:
        result = await manager.execute(
            "one", tmp_path, "import os\n(os.getcwd(),os.environ.get('MIMIR_KERNEL_TEST_ENV'))"
        )
        assert observed["args"][-5:] == (
            sys.executable,
            "-m",
            "mimir.acp.python_kernel",
            "--control-fd",
            str(observed["kwargs"]["pass_fds"][0]),
        )
        options = observed["kwargs"]
        assert options["cwd"] == tmp_path
        assert isinstance(options["env"], dict)
        assert "MIMIR_KERNEL_TEST_ENV" not in options["env"]
        assert options["start_new_session"] is True
        assert options["stdin"] == asyncio.subprocess.DEVNULL
        assert options["stdout"] == asyncio.subprocess.DEVNULL
        assert options["stderr"] == asyncio.subprocess.DEVNULL
        assert modes == [0o600, 0o600]
        assert stat.S_IMODE(manager._directory.stat().st_mode) == 0o700
        assert result["value"] == f"({str(tmp_path)!r}, None)"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_output_setup_and_protocol_failures_discard_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PythonKernelManager()
    calls = 0
    real_output = manager._output_path

    def fail_second_output(directory: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("output denied")
        return real_output(directory)

    monkeypatch.setattr(manager, "_output_path", fail_second_output)
    with pytest.raises(PythonKernelUnavailable, match="output denied"):
        await manager.execute("one", tmp_path, "1")
    assert manager._processes == {}
    monkeypatch.setattr(manager, "_output_path", real_output)
    assert (await manager.execute("one", tmp_path, "1"))["ok"] is True

    async def invalid_response(channel: socket.socket) -> dict[str, object]:
        del channel
        return {"invalid": True}

    monkeypatch.setattr(manager, "_receive", invalid_response)
    with pytest.raises(PythonKernelUnavailable, match="invalid kernel response"):
        await manager.execute("one", tmp_path, "1")
    assert manager._processes == {}
    await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("phase", ["spawn", "handshake", "output"])
@pytest.mark.asyncio
async def test_deadline_expires_during_spawn_handshake_and_output_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    manager = PythonKernelManager()
    loop = asyncio.get_running_loop()
    now = 1000.0
    trace = []
    timers = []

    class Clock:
        def time(self):
            return now

        def __getattr__(self, name):
            return getattr(loop, name)

    def timeout_at(deadline):
        assert deadline == 1060.0
        timer = asyncio.timeout_at(0 if now > deadline else None)
        timers.append(timer)
        return timer

    async def blocked(*args, **kwargs):
        nonlocal now
        if phase == "handshake":
            # _receive is reached only after a real child has been spawned and owned.
            assert len(manager._processes) == 1
        trace.append(phase)
        now = 1061.0
        timers[-1].reschedule(0)
        await asyncio.Event().wait()

    real_output = manager._output_path

    def expired_output(directory):
        nonlocal now
        path = real_output(directory)
        trace.append("output")
        now = 1061.0
        return path

    try:
        if phase == "output":
            assert (await manager.execute("setup", tmp_path, "1"))["ok"]
            monkeypatch.setattr(manager, "_output_path", expired_output)
        if phase == "handshake":
            monkeypatch.setattr(manager, "_receive", blocked)
        monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
            **vars(asyncio), "get_running_loop": lambda: Clock(),
            "timeout_at": timeout_at,
            "create_subprocess_exec": blocked if phase == "spawn" else asyncio.create_subprocess_exec,
        }))
        result = await manager.execute("setup", tmp_path, "2", 60)
        assert trace == (["output", "output"] if phase == "output" else [phase])
        assert result == {
            "ok": False, "stdout": "", "stderr": "", "value": "",
            "exception": "execution timed out after 60 seconds; namespace state lost",
            "timedOut": True, "kernel": "timed_out",
        }
        assert manager._processes == {}
    finally:
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_bounded_wait_retains_ownership_until_eventual_direct_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal; signal.pause()",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    release = asyncio.Event()
    entered = asyncio.Event()
    trace = []

    class DelayedProcess:
        pid = process.pid

        @property
        def returncode(self) -> int | None:
            return process.returncode

        async def wait(self) -> int:
            trace.append("wait")
            entered.set()
            await release.wait()
            result = await process.wait()
            trace.append("reaped")
            return result

    delayed = DelayedProcess()
    manager = PythonKernelManager()
    parent, child = socket.socketpair()
    child.close()
    worker = kernel._Worker(delayed, process.pid, parent)
    manager._processes[delayed] = process.pid

    async def expire_wait(awaitable, timeout):
        assert timeout == kernel._REAP_TIMEOUT_SECONDS
        await entered.wait()
        trace.append("expiry")
        async with asyncio.timeout(0):
            return await awaitable

    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "wait_for": expire_wait,
    }))
    try:
        await manager._terminate(worker)
        trace.append("returned")
        assert trace == ["wait", "expiry", "returned"]
        assert delayed in manager._processes
        assert worker.reaper in manager._reapers
        assert not worker.reaper.done()
        release.set()
        await manager.close()
        assert trace == ["wait", "expiry", "returned", "reaped"]
        assert process.returncode is not None
        assert delayed not in manager._processes
        assert manager._reapers == set()
    finally:
        release.set()
        await manager.close()
        if process.returncode is None:
            process.kill()
        await process.wait()


def test_worker_is_plain_exec_subprocess_without_ipykernel_or_zmq() -> None:
    path = Path(kernel.__file__)
    tree = ast.parse(path.read_text())
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "ipykernel" not in imports
    assert "zmq" not in imports
    source = path.read_text()
    assert '"-m", "mimir.acp.python_kernel"' in source
    assert "start_new_session=True" in source
    assert sys.executable
    assert os.name == "posix"


@pytest.mark.asyncio
@pytest.mark.parametrize("adoption", ["equal", "narrower", "wider", "different"])
async def test_adoption_compares_complete_spawn_policy(tmp_path, adoption):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"
    a.touch()
    b.touch()
    from mimir.acp.execution_scope import ScopeApproval
    a, b = ScopeApproval(a, False), ScopeApproval(b, False)
    policies = {"equal": (a, a), "narrower": (), "wider": (a, b), "different": (b,)}
    manager = PythonKernelManager()
    try:
        await manager.execute("one", cwd, "kept = 42", approved_paths=(a,))
        original = next(iter(manager._processes))
        await manager.release("one")
        result = await manager.execute("two", cwd, "globals().get('kept')",
                                       approved_paths=policies[adoption])
        assert result["kernel"] == ("reused" if adoption == "equal" else "fresh")
        assert result["value"] == ("42" if adoption == "equal" else "None")
        assert (original.returncode is None) == (adoption == "equal")
        state = manager._kernels[str(cwd.resolve())]
        assert state.worker.approved_paths == tuple(sorted(set(policies[adoption])))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_retire_owned_does_not_kill_another_sessions_kernel(tmp_path):
    manager = PythonKernelManager()
    try:
        await manager.execute("one", tmp_path, "kept = 42")
        await manager.retire_owned("two")
        result = await manager.execute("one", tmp_path, "kept")
        assert result["kernel"] == "reused" and result["value"] == "42"
        await manager.retire_owned("one")
        assert manager.kernels() == []
        assert not manager._directory_descriptors
    finally:
        await manager.close()


@pytest.fixture(autouse=True)
def _unit_backend_on_unsupported_platform(monkeypatch):
    # These pre-existing lifecycle/unit tests exercise real subprocesses, not OS
    # confinement. The dedicated scope/backend integration tests use Seatbelt.
    if sys.platform != "darwin":
        from mimir.acp.confinement import PreparedCommand
        import mimir.acp.hosted as hosted_module
        import mimir.acp.python_kernel as kernel_module
        def prepare(argv, **kwargs):
            env = dict(os.environ)
            env.pop("MIMIR_KERNEL_TEST_ENV", None)
            env.pop("MIMIR_HOSTED_SENTINEL", None)
            return PreparedCommand(tuple(argv), env)
        monkeypatch.setattr(hosted_module, "prepare_command", prepare)
        monkeypatch.setattr(kernel_module, "prepare_command", prepare)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["pass", "import os; os._exit(31)", "timeout"])
async def test_parent_output_reads_retained_inode_not_worker_symlink(tmp_path, ending, execution_timers):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("outside-fixture-must-not-leak")
    manager = PythonKernelManager()
    task = None
    try:
        await manager.execute("s", cwd, "1")
        directory = manager._kernels[str(cwd.resolve())].directory
        tail = ("import signal\nPath('entered').touch()\nsignal.pause()"
                if ending == "timeout" else ending)
        code = (f"from pathlib import Path\n"
                f"for p in Path({str(directory)!r}).iterdir():\n"
                f"    p.unlink()\n    p.symlink_to({str(secret)!r})\n" + tail)
        task = asyncio.create_task(manager.execute("s", cwd, code, 1))
        if ending == "timeout":
            await _appears(cwd / "entered")
            assert not task.done()
            execution_timers[-1].reschedule(0)
        result = await task
        assert "outside-fixture-must-not-leak" not in repr(result)
        assert result["stdout"] == result["stderr"] == ""
        assert result["kernel"] == {"pass": "reused", "import os; os._exit(31)": "crashed",
                                    "timeout": "timed_out"}[ending]
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_parent_output_creation_uses_pinned_scratch_directory(tmp_path):
    manager = PythonKernelManager()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        await manager.execute("s", tmp_path, "1")
        directory = manager._kernels[str(tmp_path.resolve())].directory
        moved = directory.with_name(directory.name + "-moved")
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        output = manager._output_path(directory)
        assert not list(outside.iterdir())
        assert (moved / output.name).is_file()
        os.close(manager._output_descriptors.pop(output))
        os.unlink(output.name, dir_fd=manager._directory_descriptors[directory])
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_directory_substituted_for_output_never_retains_kernel_lock(tmp_path):
    manager = PythonKernelManager()
    try:
        await manager.execute("s", tmp_path, "1")
        directory = manager._kernels[str(tmp_path.resolve())].directory
        code = ("from pathlib import Path\n"
                f"for p in Path({str(directory)!r}).iterdir():\n"
                "    p.unlink()\n    p.mkdir()\n")
        result = await manager.execute("s", tmp_path, code)
        assert result["ok"]
        assert not manager._output_descriptors
        assert not manager._kernels[str(tmp_path.resolve())].lock.locked()
        assert (await manager.execute("s", tmp_path, "42"))["value"] == "42"
        descriptor = manager._directory_descriptors[directory]
        await manager.retire(tmp_path)
        assert directory not in manager._directory_descriptors
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exited", [False, True])
async def test_kernel_signal_denial_requires_confirmed_process_exit(monkeypatch, exited):
    from unittest.mock import Mock
    entered = asyncio.Event()
    class Process:
        returncode = None
        async def wait(self):
            entered.set()
            if not exited:
                await asyncio.Event().wait()
            self.returncode = 31
            return 31
    process = Process()
    worker = kernel._Worker(process, 123, Mock())
    manager = PythonKernelManager()
    denied = PermissionError("initial signal genuinely denied")
    def killpg(*args):
        raise denied
    monkeypatch.setattr(kernel.os, "killpg", killpg)
    async def wait_for(awaitable, timeout):
        if timeout != kernel._EXIT_CONFIRMATION_SECONDS:
            return await asyncio.wait_for(awaitable, timeout)
        task = asyncio.create_task(awaitable)
        await entered.wait()
        async with asyncio.timeout(None if exited else 0):
            return await task

    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "wait_for": wait_for,
    }))
    try:
        if exited:
            await manager._terminate(worker)
            assert worker.signalled
            assert process.returncode == 31
        else:
            with pytest.raises(PermissionError) as caught:
                await manager._terminate(worker)
            assert caught.value is denied
            assert not worker.signalled
            assert worker.reaper is None
        assert entered.is_set()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_kernel_repeated_cancel_does_not_repeat_successful_signal(monkeypatch):
    from unittest.mock import Mock
    entered, finish = asyncio.Event(), asyncio.Event()
    class Process:
        returncode = None
        async def wait(self):
            entered.set()
            await finish.wait()
            self.returncode = -9
            return -9
    process = Process()
    worker = kernel._Worker(process, 123, Mock())
    manager = PythonKernelManager()
    calls = []
    def killpg(*args):
        calls.append(args)
        if len(calls) > 1:
            raise PermissionError("dying process group")
    monkeypatch.setattr(kernel.os, "killpg", killpg)
    async def wait_for(awaitable, timeout):
        assert timeout == kernel._REAP_TIMEOUT_SECONDS
        return await awaitable

    monkeypatch.setattr(kernel, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "wait_for": wait_for,
    }))
    task = asyncio.create_task(manager._terminate(worker))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.signalled
        finish.set()
        await manager._terminate(worker)
        assert calls == [(123, 9)]
    finally:
        finish.set()
        await manager.close()
