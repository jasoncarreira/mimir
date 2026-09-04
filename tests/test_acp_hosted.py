from __future__ import annotations

import asyncio
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

import mimir.acp.hosted as hosted
from mimir.acp.hands_contract import hands_v1_wire_descriptors
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
        "stdout": f"{tmp_path}\npresent",
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


@pytest.mark.asyncio
async def _child_identity(path: Path) -> tuple[int, int]:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            pid, pgid = path.read_text().split()
            return int(pid), int(pgid)
        except (FileNotFoundError, ValueError):
            await asyncio.sleep(0.01)
    raise AssertionError("owned grandchild did not start")


async def _assert_process_stopped(pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text().split()[2]
        except (FileNotFoundError, IndexError):
            return
        if state == "Z":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"owned process {pid} is still running")


def _pipe_holding_grandchild_command(identity: Path) -> str:
    source = (
        "import os,time; "
        f"open({str(identity)!r},'w').write(f'{{os.getpid()}} {{os.getpgrp()}}'); "
        "print('held',flush=True); time.sleep(30)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)} &"


@pytest.mark.asyncio
async def test_shell_deadline_kills_pipe_holding_owned_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, connection = await _connected(tmp_path)
    monkeypatch.setattr(hosted, "SHELL_TIMEOUT_SECONDS", 1)
    identity = tmp_path / "timeout-child"
    result = await provider.request(
        connection,
        "tools/call",
        {
            "name": "shell",
            "arguments": {"command": _pipe_holding_grandchild_command(identity)},
        },
    )
    pid, pgid = await _child_identity(identity)
    assert result["structuredContent"] == {
        "stdout": "held\n",
        "stderr": "\n[timed out after 1 s]",
        "exitCode": -1,
    }
    assert pgid != os.getpgrp()
    await _assert_process_stopped(pid)
    assert not provider._processes


@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.asyncio
async def test_shell_cancel_and_close_kill_pipe_holding_owned_grandchild(
    tmp_path: Path, action: str
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
    pid, pgid = await _child_identity(identity)
    while any(process.returncode is None for process in provider._processes):
        await asyncio.sleep(0)
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
async def test_hosted_provider_owns_internal_session_kernels(tmp_path: Path) -> None:
    provider = HostedHandsProvider()
    provider.bind_session("one", tmp_path)
    provider.bind_session("two", tmp_path)
    first = await provider.execute_python(provider._sessions["one"], "value = 7\nvalue")
    reused = await provider.execute_python(provider._sessions["one"], "value")
    isolated = await provider.execute_python(
        provider._sessions["two"], "globals().get('value')"
    )
    assert first["kernel"] == "fresh"
    assert reused["kernel"] == "reused"
    assert reused["value"] == "7"
    assert isolated["value"] == "None"
    assert set(provider._python_kernels._sessions) == {"one", "two"}
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
    }
    with pytest.raises(HostedMcpError) as failure:
        await provider.request(
            connection,
            "tools/call",
            {"name": name, "arguments": arguments[name]},
        )
    assert failure.value.as_error() == {"code": -32603, "message": "Internal error"}
    assert "structuredContent" not in failure.value.as_error()
