from __future__ import annotations

import ast
import asyncio
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


@pytest.mark.asyncio
async def test_timeout_and_crash_discard_namespace(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    try:
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
    started = time.monotonic()
    try:
        result = await manager.execute(
            "eof",
            tmp_path,
            "import os,stat,time\nfor fd in range(3,256):\n try:\n  if stat.S_ISSOCK(os.fstat(fd).st_mode): os.close(fd)\n except OSError: pass\ntime.sleep(10)",
            2,
        )
        assert time.monotonic() - started < 1
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
async def test_worker_and_idle_task_are_lazy_and_queue_serializes(tmp_path: Path) -> None:
    manager = PythonKernelManager()
    assert manager._sessions == {}
    assert manager._processes == {}
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
