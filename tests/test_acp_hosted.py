from __future__ import annotations

import asyncio
import errno
import os
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import mimir.acp.hosted as hosted
from mimir.acp.hands_contract import hands_v1_wire_descriptors, validate_tool_result
from mimir.acp.hosted import (
    SHELL_TIMEOUT_SECONDS,
    HostedHandsProvider,
    HostedMcpError,
)


async def _connected(tmp_path: Path) -> tuple[HostedHandsProvider, str]:
    provider = HostedHandsProvider()
    provider.bind_session("session", tmp_path)
    connection = provider.connect("session")
    initialized = await provider.request(
        connection,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert initialized == {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "mimir-hands", "version": "1"},
    }
    await provider.notification(connection, "notifications/initialized")
    return provider, connection


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(PermissionError(errno.EPERM, "denied"), id="PermissionError"),
        pytest.param(ProcessLookupError(errno.ESRCH, "gone"), id="ESRCH"),
    ],
)
@pytest.mark.asyncio
async def test_terminate_process_killpg_error_still_reaps_and_untracks(
    monkeypatch: pytest.MonkeyPatch, error: OSError,
) -> None:
    provider = HostedHandsProvider()
    process = Mock(spec=asyncio.subprocess.Process)
    process.wait = AsyncMock(return_value=0)
    # EPERM is tolerated for an exited leader, not an unsignalable live one.
    process.returncode = 0
    pgid = 12345
    provider._processes[process] = pgid
    killpg = Mock(side_effect=error)
    monkeypatch.setattr(hosted.os, "killpg", killpg)

    assert await provider._terminate_process(process, pgid) is None

    killpg.assert_called_once_with(pgid, 9)
    process.wait.assert_awaited_once_with()
    assert provider._processes == {}


@pytest.mark.asyncio
async def test_terminate_process_killpg_einval_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HostedHandsProvider()
    process = Mock(spec=asyncio.subprocess.Process)
    process.wait = AsyncMock(return_value=0)
    error = OSError(errno.EINVAL, "invalid signal")
    killpg = Mock(side_effect=error)
    monkeypatch.setattr(hosted.os, "killpg", killpg)

    with pytest.raises(OSError) as raised:
        await provider._terminate_process(process, 12345)

    assert raised.value is error
    killpg.assert_called_once_with(12345, 9)
    process.wait.assert_not_awaited()


@pytest.fixture
def shell_timers(monkeypatch):
    timers = []

    def timeout_at(deadline):
        timer = asyncio.timeout(None)
        timers.append(timer)
        return timer

    monkeypatch.setattr(hosted, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "timeout_at": timeout_at,
    }))
    return timers


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_shell_and_python_cleanup_kill_owned_process_groups(
    tmp_path: Path, shell_timers,
) -> None:
    provider, connection = await _connected(tmp_path)
    await provider.request(
        connection,
        "tools/call",
        {"name": "python", "arguments": {"code": "value = 1"}},
        request_id="python",
    )
    python_process = next(iter(provider._python_kernels._processes))
    owned = asyncio.Event()

    class Processes(dict):
        def __setitem__(self, process, pgid):
            super().__setitem__(process, pgid)
            owned.set()

    provider._processes = Processes()
    command = f"exec {shlex.quote(sys.executable)} -c 'import signal; signal.pause()'"
    shell_call = asyncio.create_task(
        provider.request(
            connection,
            "tools/call",
            {"name": "shell", "arguments": {"command": command}},
            request_id="shell",
        )
    )
    try:
        await owned.wait()
        shell_process = next(iter(provider._processes))
        assert shell_process.returncode is None
        await provider.close()
        await asyncio.gather(shell_call, return_exceptions=True)
        assert shell_process.returncode is not None
        assert python_process.returncode is not None
        assert provider._processes == {}
        assert provider._python_kernels._processes == {}
    finally:
        shell_call.cancel()
        await asyncio.gather(shell_call, return_exceptions=True)
        await provider.close()


@pytest.mark.asyncio
async def test_hosted_provider_serves_connection_initialize_list_call_and_disconnect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "value.txt"
    path.write_text("value")
    provider = HostedHandsProvider()
    provider.bind_session("session", tmp_path)
    connection = provider.connect("session")
    assert connection.startswith("mimir-hosted-connection:")
    assert len(connection.removeprefix("mimir-hosted-connection:")) == 24

    with pytest.raises(HostedMcpError, match="not initialized") as pending:
        await provider.request(connection, "tools/list", {})
    assert pending.value.code == -32600

    await provider.request(
        connection,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    with pytest.raises(HostedMcpError, match="not initialized"):
        await provider.request(connection, "initialize", {})
    await provider.notification(connection, "notifications/initialized")
    assert await provider.request(connection, "tools/list") == {
        "tools": hands_v1_wire_descriptors()
    }
    assert await provider.request(
        connection,
        "tools/call",
        {
            "name": "read",
            "arguments": {"path": "value.txt"},
            "_meta": {"progressToken": 1},
        },
        request_id="read",
    ) == {"content": [], "structuredContent": {"content": "value"}}
    assert await provider.disconnect(connection) == {}
    with pytest.raises(HostedMcpError, match="Unknown MCP connection"):
        await provider.request(connection, "tools/list")


@pytest.mark.asyncio
async def test_relative_absolute_symlink_encoding_size_and_frame_contract(
    tmp_path: Path,
) -> None:
    provider, connection = await _connected(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(b"a\xffb")
    (tmp_path / "link").symlink_to(target)

    for path in ("link", str(target)):
        result = await provider.request(
            connection,
            "tools/call",
            {"name": "read", "arguments": {"path": path}},
            request_id=1,
        )
        assert result["structuredContent"] == {"content": "a�b"}

    target.write_bytes(b"x" * 1_048_577)
    with pytest.raises(HostedMcpError) as too_large:
        await provider.request(
            connection,
            "tools/call",
            {"name": "read", "arguments": {"path": "target"}},
        )
    assert too_large.value.as_error() == {
        "code": -32000,
        "message": "file too large (1048577 bytes)",
    }

    target.write_bytes(b"x" * 1_048_576)
    with pytest.raises(HostedMcpError, match="frame limit") as frame:
        await provider.request(
            connection,
            "tools/call",
            {"name": "read", "arguments": {"path": "target"}},
        )
    assert frame.value.code == -32000


@pytest.mark.asyncio
async def test_edit_cardinality_atomic_mode_and_symlink_contract(tmp_path: Path) -> None:
    provider, connection = await _connected(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(b"before before")
    target.chmod(0o640)

    for old_text, count in (("absent", 0), ("before", 2)):
        with pytest.raises(HostedMcpError) as mismatch:
            await provider.request(
                connection,
                "tools/call",
                {
                    "name": "edit",
                    "arguments": {
                        "path": "target",
                        "oldText": old_text,
                        "newText": "after",
                    },
                },
            )
        assert mismatch.value.message == f"edit mismatch: oldText occurs {count} times"
    assert target.read_bytes() == b"before before"

    target.write_bytes(b"before")
    original_inode = target.stat().st_ino
    changed = await provider.request(
        connection,
        "tools/call",
        {
            "name": "edit",
            "arguments": {
                "path": str(target),
                "oldText": "before",
                "newText": "after",
            },
        },
    )
    assert changed == {"content": [], "structuredContent": {"changed": True}}
    assert target.read_bytes() == b"after"
    assert target.stat().st_ino != original_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o640

    link = tmp_path / "link"
    link.symlink_to(target)
    linked = await provider.request(
        connection,
        "tools/call",
        {
            "name": "edit",
            "arguments": {
                "path": "link",
                "oldText": "after",
                "newText": "linked",
            },
        },
    )
    assert linked["structuredContent"] == {"changed": True}
    assert link.is_symlink()
    assert target.read_text() == "linked"


@pytest.mark.asyncio
async def test_shell_uses_bin_sh_cwd_environment_and_bounded_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIMIR_HOSTED_SENTINEL", "present")
    provider, connection = await _connected(tmp_path)
    environment = await provider.request(
        connection,
        "tools/call",
        {
            "name": "shell",
            "arguments": {
                "command": "printf '%s\\n%s' \"$PWD\" \"$MIMIR_HOSTED_SENTINEL\""
            },
        },
    )
    assert environment["structuredContent"] == {
        "stdout": f"{tmp_path}\n",
        "stderr": "",
        "exitCode": 0,
    }
    command = (
        f"{shlex.quote(sys.executable)} -c 'import os; "
        "os.write(1, b\"\\xff\" + b\"x\"*262150); "
        "os.write(2, b\"\\xfe\" + b\"y\"*262152)'"
    )
    result = await provider.request(
        connection,
        "tools/call",
        {"name": "shell", "arguments": {"command": command}},
    )
    structured = result["structuredContent"]
    assert structured["stdout"] == (
        "�" + "x" * 262143 + "\n…[truncated 7 bytes]"
    )
    assert structured["stderr"] == (
        "�" + "y" * 262143 + "\n…[truncated 9 bytes]"
    )
    assert structured["exitCode"] == 0
    assert SHELL_TIMEOUT_SECONDS == 60


async def _child_identity(path: Path) -> tuple[int, int]:
    while True:
        try:
            pid, pgid = path.read_text().split()
            return int(pid), int(pgid)
        except (FileNotFoundError, ValueError):
            await asyncio.sleep(0.01)


async def _assert_process_stopped(pid: int) -> None:
    while True:
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError, IndexError):
            return
        if state == "Z":
            return
        await asyncio.sleep(0.01)


def _pipe_holding_grandchild_command(identity: Path) -> str:
    source = (
        "import os,signal,pathlib; "
        "print('held',flush=True); "
        f"p=pathlib.Path({str(identity)!r}); "
        "p.with_suffix('.tmp').write_text(f'{os.getpid()} {os.getpgrp()}'); "
        "p.with_suffix('.tmp').replace(p); signal.pause()"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)} &"


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_shell_deadline_kills_pipe_holding_owned_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell_timers,
) -> None:
    provider = HostedHandsProvider(1)
    provider.bind_session("session", tmp_path)
    connection = provider.connect("session")
    await provider.request(
        connection,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    await provider.notification(connection, "notifications/initialized")
    captures = []
    drain = provider._drain_output

    async def observed_drain(stream, capture):
        captures.append(capture)
        await drain(stream, capture)

    monkeypatch.setattr(provider, "_drain_output", observed_drain)
    identity = tmp_path / "timeout-child"
    call = asyncio.create_task(provider.request(
        connection,
        "tools/call",
        {
            "name": "shell",
            "arguments": {"command": _pipe_holding_grandchild_command(identity)},
        },
    ))
    try:
        pid, pgid = await _child_identity(identity)
        while not any(capture.retained == b"held\n" for capture in captures):
            await asyncio.sleep(0)
        while any(process.returncode is None for process in provider._processes):
            await asyncio.sleep(0)
        assert not call.done()  # The exited shell's descendant still holds its pipes.
        shell_timers[-1].reschedule(0)
        result = await call
        assert result["structuredContent"] == {
            "stdout": "held\n",
            "stderr": "\n[timed out after 1 s]",
            "exitCode": -1,
        }
        assert pgid != os.getpgrp()
        await _assert_process_stopped(pid)
        assert not provider._processes
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await provider.close()


@pytest.fixture
def shell_deadlines(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    observed = []
    clock_reads = []

    def clock() -> float:
        now = asyncio.get_running_loop().time()
        clock_reads.append(now)
        return now

    def timeout_at(deadline: float) -> object:
        assert len(clock_reads) == 1
        observed.append(deadline - clock_reads.pop())
        return asyncio.timeout_at(deadline)

    # Observe the producer, not elapsed startup time; leave asyncio's own clock alone.
    monkeypatch.setattr(hosted, "asyncio", SimpleNamespace(**{
        **vars(asyncio),
        "get_running_loop": lambda: SimpleNamespace(time=clock),
        "timeout_at": timeout_at,
    }))
    return observed


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_shell_and_python_default_timeout_is_60_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell_deadlines: list[float],
) -> None:
    provider, connection = await _connected(tmp_path)
    observed: list[int | float] = []

    async def execute(
        session_id: str, cwd: Path, code: str, timeout: int | float, **kwargs: object,
    ) -> dict[str, object]:
        observed.append(timeout)
        return {
            "ok": True, "stdout": "", "stderr": "", "value": "",
            "exception": "", "timedOut": False, "kernel": "fresh",
        }

    monkeypatch.setattr(provider._python_kernels, "execute", execute)
    await provider.request(
        connection, "tools/call", {"name": "shell", "arguments": {"command": "true"}}
    )
    await provider.request(
        connection, "tools/call", {"name": "python", "arguments": {"code": "pass"}}
    )
    assert provider._sessions["session"].timeout_seconds == 60
    assert observed[-1] == 60
    # The configured budget above is exact. This one is DERIVED: the fixture
    # reconstructs it as `deadline - clock_read`, and the product built that
    # deadline as `loop.time() + 60`. For a large monotonic base `(t + 60.0) - t`
    # is not exactly 60.0 -- macOS observed 59.99999999999997 -- so compare with a
    # tolerance far tighter than any timing signal this asserts.
    assert shell_deadlines == [pytest.approx(60, abs=1e-6)]
    await provider.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_timeout_comes_only_from_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell_deadlines: list[float],
) -> None:
    monkeypatch.setenv("MIMIR_ACP_TIMEOUT", "1")
    provider = HostedHandsProvider(7)
    provider.bind_session("selected", tmp_path)
    connection = provider.connect("selected")
    await provider.request(
        connection,
        "initialize",
        {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    await provider.notification(connection, "notifications/initialized")
    observed: list[int | float] = []

    async def execute(
        session_id: str, cwd: Path, code: str, timeout: int | float, **kwargs: object,
    ) -> dict[str, object]:
        observed.append(timeout)
        return {
            "ok": True, "stdout": "", "stderr": "", "value": "",
            "exception": "", "timedOut": False, "kernel": "fresh",
        }

    monkeypatch.setattr(provider._python_kernels, "execute", execute)
    await provider.request(
        connection, "tools/call", {"name": "shell", "arguments": {"command": "true"}}
    )
    await provider.request(
        connection, "tools/call", {"name": "python", "arguments": {"code": "pass"}}
    )
    assert observed[-1] == 7
    # Derived the same way as the 60-second case above; see that comment.
    assert shell_deadlines == [pytest.approx(7, abs=1e-6)]
    with pytest.raises(HostedMcpError, match="Invalid params"):
        await provider.request(
            connection,
            "tools/call",
            {"name": "python", "arguments": {"code": "pass", "timeout": 1}},
        )
    await provider.close()


@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_shell_cancel_and_close_kill_pipe_holding_owned_grandchild(
    tmp_path: Path, action: str, shell_timers,
) -> None:
    provider, connection = await _connected(tmp_path)
    identity = tmp_path / f"{action}-child"
    call = asyncio.create_task(
        provider.request(
            connection,
            "tools/call",
            {
                "name": "shell",
                "arguments": {
                    "command": _pipe_holding_grandchild_command(identity)
                },
            },
            request_id="cancel-me",
        )
    )
    try:
        pid, pgid = await _child_identity(identity)
        while any(process.returncode is None for process in provider._processes):
            await asyncio.sleep(0)
        assert not call.done()
        if action == "cancel":
            await provider.notification(
                connection, "notifications/cancelled", {"requestId": "cancel-me"}
            )
        else:
            await provider.close()
        with pytest.raises(HostedMcpError, match="Request cancelled") as cancelled:
            await call
        assert cancelled.value.code == -32800
        assert pgid != os.getpgrp()
        await _assert_process_stopped(pid)
        assert not provider._processes
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await provider.close()


@pytest.mark.asyncio
async def test_call_arguments_metadata_and_results_are_strict(tmp_path: Path) -> None:
    provider, connection = await _connected(tmp_path)
    invalid = (
        {"name": "read", "arguments": {"path": "x", "extra": True}},
        {"name": "read", "arguments": {"path": "x"}, "_meta": None},
        {
            "name": "read",
            "arguments": {"path": "x"},
            "_meta": {"progressToken": True},
        },
        {"name": "unknown", "arguments": {}},
    )
    for params in invalid:
        with pytest.raises(HostedMcpError, match="Invalid params") as error:
            await provider.request(connection, "tools/call", params)
        assert error.value.code == -32602

    with pytest.raises(HostedMcpError, match="Method not found") as missing:
        await provider.request(connection, "other", {})
    assert missing.value.code == -32601
    await provider.close()
    await provider.close()


@pytest.mark.asyncio
async def test_tools_call_envelope_accepts_only_name_arguments_and_progress_token(
    tmp_path: Path,
) -> None:
    (tmp_path / "value").write_text("ok")
    provider, connection = await _connected(tmp_path)
    try:
        for params in (
            {"arguments": {"path": "value"}},
            {"name": "read"},
            {"name": "read", "arguments": {"path": "value"}, "extra": True},
            {
                "name": "read",
                "arguments": {"path": "value"},
                "_meta": {"progressToken": "accepted", "extra": True},
            },
        ):
            with pytest.raises(HostedMcpError) as failure:
                await provider.request(connection, "tools/call", params)
            assert failure.value.as_error() == {
                "code": -32602,
                "message": "Invalid params",
            }

        assert await provider.request(
            connection,
            "tools/call",
            {
                "name": "read",
                "arguments": {"path": "value"},
                "_meta": {"progressToken": "accepted"},
            },
        ) == {"content": [], "structuredContent": {"content": "ok"}}
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["disconnect", "cancel_session"])
async def test_hosted_provider_releases_project_kernel(
    tmp_path: Path, boundary: str,
) -> None:
    provider = HostedHandsProvider()
    connection = provider.connect("one", tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    provider.bind_session("two", alias)
    try:
        first = await provider.execute_python(provider._sessions["one"], "value = 7\nvalue")
        reused = await provider.execute_python(provider._sessions["one"], "value")
        assert first["kernel"] == "fresh"
        assert reused["kernel"] == "reused"
        assert reused["value"] == "7"
        worker = next(iter(provider._python_kernels._processes))
        with pytest.raises(HostedMcpError, match="kernel unavailable"):
            await provider.execute_python(provider._sessions["two"], "value = 99")
        if boundary == "disconnect":
            await provider.disconnect(connection)
        else:
            await provider.cancel_session("one")
        assert worker.returncode is None
        transferred = await provider.execute_python(provider._sessions["two"], "value")
        assert transferred["kernel"] == "reused"
        assert transferred["value"] == "7"
        assert set(provider._python_kernels._kernels) == {str(tmp_path.resolve())}
        with pytest.raises(HostedMcpError, match="kernel unavailable"):
            await provider.execute_python(provider._sessions["one"], "value")
    finally:
        await provider.close()
    assert provider._python_kernels._processes == {}


@pytest.mark.parametrize(
    ("name", "result"),
    [
        ("read", {}),
        ("read", {"content": "value", "extra": True}),
        ("read", {"content": 1}),
        ("edit", {}),
        ("edit", {"changed": True, "extra": True}),
        ("edit", {"changed": 1}),
        ("shell", {"stdout": "", "stderr": ""}),
        ("shell", {"stdout": "", "stderr": "", "exitCode": 0, "extra": True}),
        ("shell", {"stdout": "", "stderr": "", "exitCode": False}),
        (
            "python",
            {
                "ok": True, "stdout": "", "stderr": "", "value": "",
                "exception": "", "timedOut": 0, "kernel": "fresh",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_malformed_operation_results_are_internal_errors_without_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    result: dict[str, object],
) -> None:
    provider, connection = await _connected(tmp_path)

    async def malformed_result(*args: object) -> dict[str, object]:
        return result

    monkeypatch.setattr(provider, "_call", malformed_result)
    arguments = {
        "read": {"path": "value"},
        "edit": {"path": "value", "oldText": "old", "newText": "new"},
        "shell": {"command": "true"},
        "python": {"code": "pass"},
    }
    with pytest.raises(HostedMcpError) as failure:
        await provider.request(
            connection,
            "tools/call",
            {"name": name, "arguments": arguments[name]},
        )
    assert failure.value.as_error() == {"code": -32603, "message": "Internal error"}
    assert "structuredContent" not in failure.value.as_error()


@pytest.mark.asyncio
async def test_tools_list_is_exact_four_tool_profile_and_extra_tool_errors(
    tmp_path: Path,
) -> None:
    provider, connection = await _connected(tmp_path)
    listed = await provider.request(connection, "tools/list", {})
    assert listed == {"tools": hands_v1_wire_descriptors()}
    assert [descriptor["name"] for descriptor in listed["tools"]] == [
        "read", "edit", "shell", "python", "request_scope"
    ]
    with pytest.raises(HostedMcpError) as failure:
        await provider.request(
            connection, "tools/call", {"name": "Python", "arguments": {"code": "1"}}
        )
    assert failure.value.as_error() == {"code": -32602, "message": "Invalid params"}
    await provider.close()


@pytest.mark.asyncio
async def test_python_result_has_exact_seven_keys(tmp_path: Path) -> None:
    provider, connection = await _connected(tmp_path)
    result = await provider.request(
        connection,
        "tools/call",
        {
            "name": "python",
            "arguments": {"code": "print('out')\n2 + 3"},
            "_meta": {"progressToken": "python-progress"},
        },
        request_id="python",
    )
    assert result["content"] == []
    structured = result["structuredContent"]
    assert set(structured) == {
        "ok", "stdout", "stderr", "value", "exception", "timedOut", "kernel"
    }
    assert structured == {
        "ok": True,
        "stdout": "out\n",
        "stderr": "",
        "value": "5",
        "exception": "",
        "timedOut": False,
        "kernel": "fresh",
    }
    assert type(structured["timedOut"]) is bool
    await provider.close()


@pytest.mark.asyncio
async def test_python_operator_commands_preserve_wire_contract_and_project_scope(
    tmp_path: Path,
) -> None:
    provider, connection = await _connected(tmp_path)
    other_project = tmp_path / "other"
    other_project.mkdir()
    provider.bind_session("other", other_project)
    provider.bind_session("contender", tmp_path)

    async def command(code: str) -> dict[str, object]:
        response = await provider.request(
            connection, "tools/call", {"name": "python", "arguments": {"code": code}},
        )
        assert response["content"] == []
        return validate_tool_result("python", response["structuredContent"])

    try:
        await command("value = 7")
        await provider.execute_python(provider._sessions["other"], "value = 9")
        listed = await command("%kernels")
        assert listed["ok"] is True
        listing = listed["stdout"] + listed["value"]
        for expected in (str(tmp_path.resolve()), str(other_project.resolve()), "session", "other"):
            assert expected in listing
        for process in provider._python_kernels._processes:
            assert str(process.pid) in listing

        released = await command("%kernel release")
        assert released["ok"] is True
        acquired = await provider.execute_python(provider._sessions["contender"], "value")
        assert acquired["kernel"] == "reused"
        assert acquired["value"] == "7"
        with pytest.raises(HostedMcpError, match="owned by session contender; refused"):
            await command("%kernel kill")
        preserved = await provider.execute_python(provider._sessions["contender"], "value")
        assert preserved["kernel"] == "reused"
        assert preserved["value"] == "7"

        await provider.cancel_session("contender")
        assert (await command("%kernel kill"))["ok"] is True
        fresh = await command("globals().get('value')")
        assert fresh["kernel"] == "fresh"
        assert fresh["value"] == "None"
        assert (await command("%kernel kill"))["ok"] is True
        other = await provider.execute_python(provider._sessions["other"], "value")
        assert other["kernel"] == "reused"
        assert other["value"] == "9"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_python_timed_out_is_always_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, connection = await _connected(tmp_path)

    async def malformed(*args: object) -> dict[str, object]:
        return {
            "ok": False, "stdout": "", "stderr": "", "value": "", "exception": "",
            "timedOut": 1, "kernel": "timed_out",
        }

    monkeypatch.setattr(provider, "execute_python", malformed)
    with pytest.raises(HostedMcpError) as failure:
        await provider.request(
            connection,
            "tools/call",
            {"name": "python", "arguments": {"code": "pass"}},
        )
    assert failure.value.as_error() == {"code": -32603, "message": "Internal error"}
    await provider.close()


@pytest.mark.asyncio
async def test_spawn_failure_returns_exact_mcp_error_without_structured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.acp.python_kernel import PythonKernelUnavailable

    provider, connection = await _connected(tmp_path)

    async def unavailable(*args: object, **kwargs: object) -> dict[str, object]:
        raise PythonKernelUnavailable("spawn refused")

    monkeypatch.setattr(provider._python_kernels, "execute", unavailable)
    with pytest.raises(HostedMcpError) as failure:
        await provider.request(
            connection,
            "tools/call",
            {"name": "python", "arguments": {"code": "1"}},
        )
    assert failure.value.as_error() == {
        "code": -32000,
        "message": "hands_python kernel unavailable: spawn refused",
    }
    assert "structuredContent" not in failure.value.as_error()
    await provider.close()


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


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_shell_double_cancellation_reaps_without_signalling_killed_group_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    running = asyncio.Event()
    reaping = asyncio.Event()
    signals: list[tuple[int, int]] = []

    class Process:
        pid = 12345
        returncode: int | None = None

        def __init__(self) -> None:
            self.wait_count = 0
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            self.wait_count += 1
            if self.wait_count == 1:
                running.set()
                await asyncio.Future()
            if self.wait_count == 2:
                reaping.set()
                await asyncio.Future()
            self.returncode = -9
            return -9

    process = Process()

    async def spawn(*args: object, **kwargs: object) -> Process:
        return process

    def killpg(pgid: int, signal: int) -> None:
        if signals:
            # The killed macOS group can refuse a second signal before the
            # subprocess watcher updates returncode. Never retry that signal.
            raise PermissionError("already killed, not reaped")
        signals.append((pgid, signal))

    monkeypatch.setattr(hosted, "prepare_command", lambda *a, **kw: SimpleNamespace(argv=("sh",), env={}, execution_mode="confined"))
    monkeypatch.setattr(hosted.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(hosted.os, "killpg", killpg)
    provider = HostedHandsProvider()
    provider.bind_session("session", tmp_path)
    task = asyncio.create_task(provider._shell(provider._sessions["session"], "unused"))
    try:
        await running.wait()
        task.cancel()  # proxy cancellation while the shell is running
        await reaping.wait()
        task.cancel()  # provider cancellation while process.wait() is reaping
        with pytest.raises(asyncio.CancelledError):
            await task
        assert signals == [(process.pid, 9)]
        assert process.returncode == -9
        assert provider._processes == {}
        assert provider._signalled_processes == set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.asyncio
async def test_first_live_process_signal_denial_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = None

        async def wait(self) -> int:
            pytest.fail("must not wait forever for an unsignalable live process")

    def denied(*args: object) -> None:
        raise PermissionError("not allowed")

    monkeypatch.setattr(hosted.os, "killpg", denied)
    provider = HostedHandsProvider()
    process = Process()
    with pytest.raises(PermissionError, match="not allowed"):
        await provider._terminate_process(process, 12345)
    assert provider._signalled_processes == set()
    await provider.close()
