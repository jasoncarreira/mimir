from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

import pytest

import mimir.acp.python_kernel as kernel
from mimir.acp.python_kernel import PythonKernelManager


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
