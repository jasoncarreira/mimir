from __future__ import annotations

import ast
import asyncio
import builtins
import os
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

import mimir.acp.python_kernel as kernel
from mimir.acp.python_kernel import PythonKernelManager, PythonKernelUnavailable


async def _stopped(pid: int) -> bool:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text().split()[2]
        except (FileNotFoundError, IndexError):
            return True
        if state == "Z":
            return True
        await asyncio.sleep(0.01)
    return False


async def _appears(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


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
            "streams", tmp_path, "import os\nos.write(1, b'\\xff' * 65537)"
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
            "large", tmp_path, "raise RuntimeError('界' * 6000)"
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
        assert result["kernel"] == "fresh"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_timeout_and_crash_discard_namespace(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        assert (await manager.execute("timeout", tmp_path, "1", 5))["ok"] is True
        timed_out = await manager.execute(
            "timeout", tmp_path, "import time\nmarker = 1\ntime.sleep(5)", 0.1
        )
        assert timed_out == {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "value": "",
            "exception": "execution timed out after 0.1 seconds; namespace state lost",
            "timedOut": True,
            "kernel": "timed_out",
        }
        assert (await manager.execute("timeout", tmp_path, "globals().get('marker')"))[
            "kernel"
        ] == "fresh"
        crashed = await manager.execute("crash", tmp_path, "import os\nos._exit(23)")
        assert crashed["kernel"] == "crashed"
        assert crashed["exception"] == (
            "kernel process exited with code 23; namespace state lost"
        )
        assert crashed["stdout"] == ""
        assert (await manager.execute("crash", tmp_path, "1"))["kernel"] == "fresh"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_timeout_and_crash_retain_streams_exactly(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        assert (await manager.execute("timeout", tmp_path, "1", 5))["ok"] is True
        timed_out = await manager.execute(
            "timeout",
            tmp_path,
            "import os,time\nos.write(1,b'before-timeout')\nos.write(2,b'err-timeout')\ntime.sleep(5)",
            1,
        )
        assert timed_out["stdout"] == "before-timeout"
        assert timed_out["stderr"] == "err-timeout"
        assert timed_out["exception"] == (
            "execution timed out after 1 seconds; namespace state lost"
        )
        crashed = await manager.execute(
            "crash",
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
        await manager.close()


@pytest.mark.asyncio
async def test_live_control_eof_is_killed_without_waiting_for_worker(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    try:
        result = await manager.execute(
            "eof",
            tmp_path,
            "import os,stat,time\nfor fd in range(3,256):\n try:\n  if stat.S_ISSOCK(os.fstat(fd).st_mode): os.close(fd)\n except OSError: pass\ntime.sleep(10)",
            2,
        )
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
    child_code = (
        "import os,time,pathlib;"
        f"pathlib.Path({str(identity)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    code = (
        "import os,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}],close_fds=True)\n"
        "while not os.path.exists(" + repr(str(identity)) + "): time.sleep(.01)\n"
        "os._exit(37)"
    )
    try:
        result = await manager.execute("descendant", tmp_path, code)
        async with asyncio.timeout(5):
            while not identity.read_text():
                await asyncio.sleep(0.01)
        pid = int(identity.read_text())
        assert result["exception"] == (
            "kernel process exited with code 37; namespace state lost"
        )
        assert await _stopped(pid)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sessions_parallel_and_namespaces_isolated(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        first, second = await asyncio.gather(
            manager.execute("one", tmp_path, "import time\ntime.sleep(.2)\nvalue = 1"),
            manager.execute("two", tmp_path, "import time\ntime.sleep(.2)\nvalue = 2"),
        )
        assert first["kernel"] == second["kernel"] == "fresh"
        values = await asyncio.gather(
            manager.execute("one", tmp_path, "value"),
            manager.execute("two", tmp_path, "value"),
        )
        assert [item["value"] for item in values] == ["1", "2"]
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


@pytest.mark.asyncio
async def test_queue_wait_is_outside_timeout_and_other_session_is_parallel(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    entered = tmp_path / "entered"
    try:
        blocker = asyncio.create_task(
            manager.execute(
                "one",
                tmp_path,
                "import pathlib,time\npathlib.Path('entered').write_text('yes')\ntime.sleep(.5)",
                2,
            )
        )
        await _appears(entered)
        queued_at = time.monotonic()
        queued = asyncio.create_task(
            manager.execute("one", tmp_path, "2", 0.2)
        )
        await blocker
        queued_result = await queued
        assert queued_result["value"] == "2"
        assert queued_result["timedOut"] is False
        assert time.monotonic() - queued_at > 0.3

        parallel_at = time.monotonic()
        first, second = await asyncio.gather(
            manager.execute(
                "parallel-one",
                tmp_path,
                "import pathlib,time\npathlib.Path('parallel-one').write_text('yes')\ndeadline=time.monotonic()+3\nwhile not pathlib.Path('parallel-two').exists():\n if time.monotonic()>deadline: raise RuntimeError('not parallel')\n time.sleep(.01)\ntime.sleep(.3)\n1",
                5,
            ),
            manager.execute(
                "parallel-two",
                tmp_path,
                "import pathlib,time\npathlib.Path('parallel-two').write_text('yes')\ndeadline=time.monotonic()+3\nwhile not pathlib.Path('parallel-one').exists():\n if time.monotonic()>deadline: raise RuntimeError('not parallel')\n time.sleep(.01)\ntime.sleep(.3)\n2",
                5,
            ),
        )
        assert first["value"] == "1"
        assert second["value"] == "2"
        assert time.monotonic() - parallel_at < 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_queued_and_active_cancellation_have_distinct_worker_effects(
    tmp_path: Path,
) -> None:
    manager = PythonKernelManager()
    try:
        active = asyncio.create_task(
            manager.execute("one", tmp_path, "import time\ntime.sleep(.25)\nvalue=4")
        )
        await asyncio.sleep(0.05)
        queued = asyncio.create_task(manager.execute("one", tmp_path, "value"))
        while manager._sessions["one"].waiters != 1:
            await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await active
        assert (await manager.execute("one", tmp_path, "value"))["kernel"] == "reused"

        cancelled = asyncio.create_task(
            manager.execute("one", tmp_path, "import time\ntime.sleep(10)")
        )
        while not manager._sessions["one"].lock.locked():
            await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert manager._processes == {}
        assert (await manager.execute("one", tmp_path, "1"))["kernel"] == "fresh"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_worker_and_idle_task_are_lazy(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    assert manager._sessions == {}
    assert manager._processes == {}
    assert kernel.IDLE_SECONDS == 1_800
    first = asyncio.create_task(
        manager.execute("one", tmp_path, "import time\ntime.sleep(.2)\nsequence = [1]")
    )
    await asyncio.sleep(0.05)
    second = asyncio.create_task(manager.execute("one", tmp_path, "sequence.append(2)\nsequence"))
    try:
        assert (await first)["value"] == ""
        assert (await second)["value"] == "[1, 2]"
        assert manager._sessions["one"].idle_task is not None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_idle_retirement_discards_worker_and_next_call_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kernel, "IDLE_SECONDS", 0.05)
    manager = PythonKernelManager()
    try:
        await manager.execute("one", tmp_path, "value = 9")
        state = manager._sessions["one"]
        process = state.worker.process
        async with asyncio.timeout(2):
            while state.worker is not None:
                await asyncio.sleep(0.01)
        assert await _stopped(process.pid)
        result = await manager.execute("one", tmp_path, "globals().get('value')")
        assert result["kernel"] == "fresh"
        assert result["value"] == "None"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_registered_waiter_wins_idle_retirement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kernel, "IDLE_SECONDS", 0.1)
    manager = PythonKernelManager()
    try:
        await manager.execute("one", tmp_path, "value = 1")
        active = asyncio.create_task(
            manager.execute("one", tmp_path, "import time\ntime.sleep(.2)\nvalue")
        )
        await asyncio.sleep(0.02)
        waiter = asyncio.create_task(manager.execute("one", tmp_path, "value + 1"))
        assert (await active)["kernel"] == "reused"
        assert (await waiter)["value"] == "2"
        await asyncio.sleep(0.15)
        assert manager._sessions["one"].worker is None
        assert (await manager.execute("one", tmp_path, "3"))["kernel"] == "fresh"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_real_waiter_registered_at_idle_expiry_keeps_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kernel, "IDLE_SECONDS", 0.1)
    manager = PythonKernelManager()
    try:
        await manager.execute("one", tmp_path, "value = 9")
        state = manager._sessions["one"]
        worker = state.worker
        await state.lock.acquire()
        waiter = asyncio.create_task(manager.execute("one", tmp_path, "value"))
        while state.waiters != 1:
            await asyncio.sleep(0)
        await asyncio.sleep(0.15)
        assert state.worker is worker
        assert worker is not None and worker.process.returncode is None
        state.lock.release()
        result = await waiter
        assert result["kernel"] == "reused"
        assert result["value"] == "9"
    finally:
        if manager._sessions.get("one", None) is not None:
            state = manager._sessions["one"]
            if state.lock.locked():
                state.lock.release()
        await manager.close()


@pytest.mark.asyncio
async def test_late_background_output_is_discarded_between_calls(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
        first = await manager.execute(
            "one",
            tmp_path,
            "import threading,time\ndef late():\n time.sleep(.1)\n print('late',flush=True)\nthreading.Thread(target=late,daemon=True).start()",
        )
        assert first["stdout"] == ""
        await asyncio.sleep(0.2)
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

    def output_path() -> Path:
        path = real_output()
        modes.append(stat.S_IMODE(path.stat().st_mode))
        return path

    monkeypatch.setattr(manager, "_output_path", output_path)
    try:
        result = await manager.execute(
            "one", tmp_path, "import os\n(os.getcwd(),os.environ['MIMIR_KERNEL_TEST_ENV'])"
        )
        assert observed["args"] == (
            sys.executable,
            "-m",
            "mimir.acp.python_kernel",
            "--control-fd",
            str(observed["kwargs"]["pass_fds"][0]),
        )
        options = observed["kwargs"]
        assert options["cwd"] == tmp_path
        assert options["env"] is None
        assert options["start_new_session"] is True
        assert options["stdout"] == asyncio.subprocess.DEVNULL
        assert options["stderr"] == asyncio.subprocess.DEVNULL
        assert modes == [0o600, 0o600]
        assert stat.S_IMODE(manager._directory.stat().st_mode) == 0o700
        assert result["value"] == f"({str(tmp_path)!r}, 'inherited')"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_output_setup_and_protocol_failures_discard_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PythonKernelManager()
    calls = 0
    real_output = manager._output_path

    def fail_second_output() -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("output denied")
        return real_output()

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


@pytest.mark.asyncio
async def test_deadline_expires_during_spawn_handshake_and_output_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeout_result = {
        "ok": False,
        "stdout": "",
        "stderr": "",
        "value": "",
        "exception": "execution timed out after 0.01 seconds; namespace state lost",
        "timedOut": True,
        "kernel": "timed_out",
    }
    spawn_manager = PythonKernelManager()

    async def blocked_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_spawn)
    assert await spawn_manager.execute("spawn", tmp_path, "1", 0.01) == timeout_result
    assert spawn_manager._processes == {}
    await spawn_manager.close()
    monkeypatch.undo()

    handshake_manager = PythonKernelManager()

    async def blocked_handshake(channel: socket.socket) -> dict[str, object]:
        del channel
        await asyncio.Event().wait()

    monkeypatch.setattr(handshake_manager, "_receive", blocked_handshake)
    handshake = await handshake_manager.execute("handshake", tmp_path, "1", 1)
    assert handshake == {
        **timeout_result,
        "exception": "execution timed out after 1 seconds; namespace state lost",
    }
    assert handshake_manager._processes == {}
    await handshake_manager.close()

    setup_manager = PythonKernelManager()
    assert (await setup_manager.execute("setup", tmp_path, "1"))["ok"] is True
    real_output = setup_manager._output_path

    def delayed_output() -> Path:
        time.sleep(0.03)
        return real_output()

    monkeypatch.setattr(setup_manager, "_output_path", delayed_output)
    assert await setup_manager.execute("setup", tmp_path, "2", 0.01) == timeout_result
    assert setup_manager._processes == {}
    await setup_manager.close()


@pytest.mark.asyncio
async def test_bounded_wait_retains_ownership_until_eventual_direct_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    release = asyncio.Event()

    class DelayedProcess:
        pid = process.pid

        @property
        def returncode(self) -> int | None:
            return process.returncode

        async def wait(self) -> int:
            await release.wait()
            return await process.wait()

    delayed = DelayedProcess()
    manager = PythonKernelManager()
    parent, child = socket.socketpair()
    child.close()
    worker = kernel._Worker(delayed, process.pid, parent)
    manager._processes[delayed] = process.pid
    monkeypatch.setattr(kernel, "_REAP_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    await manager._terminate(worker)
    assert time.monotonic() - started < 0.2
    assert delayed in manager._processes
    assert manager._reapers
    release.set()
    await manager.close()
    assert process.returncode is not None
    assert delayed not in manager._processes
    assert manager._reapers == set()


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
    assert '"-m",\n                    "mimir.acp.python_kernel"' in source
    assert "start_new_session=True" in source
    assert sys.executable
    assert os.name == "posix"
