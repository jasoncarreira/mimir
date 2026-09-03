from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

import mimir.acp.hosted as hosted
from mimir.acp.hands_contract import hands_v1_wire_descriptors
from mimir.acp.hosted import (
    OUTPUT_LIMIT_BYTES,
    READ_LIMIT_BYTES,
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

    target.write_bytes(b"x" * (READ_LIMIT_BYTES + 1))
    with pytest.raises(HostedMcpError) as too_large:
        await provider.request(
            connection,
            "tools/call",
            {"name": "read", "arguments": {"path": "target"}},
        )
    assert too_large.value.as_error() == {
        "code": -32000,
        "message": f"file too large ({READ_LIMIT_BYTES + 1} bytes)",
    }

    target.write_bytes(b"x" * READ_LIMIT_BYTES)
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
    command = (
        "pwd; printf \"$MIMIR_HOSTED_SENTINEL\"; "
        f"python -c 'import os; os.write(1, b\"x\"*{OUTPUT_LIMIT_BYTES + 7}); "
        f"os.write(2, b\"y\"*{OUTPUT_LIMIT_BYTES + 9})'"
    )
    result = await provider.request(
        connection,
        "tools/call",
        {"name": "shell", "arguments": {"command": command}},
    )
    structured = result["structuredContent"]
    assert structured["stdout"].startswith(f"{tmp_path}\npresent")
    prefix = f"{tmp_path}\npresent"
    assert structured["stdout"].endswith(
        f"\n…[truncated {len(prefix.encode()) + 7} bytes]"
    )
    assert structured["stderr"].endswith("\n…[truncated 9 bytes]")
    assert structured["exitCode"] == 0
    assert SHELL_TIMEOUT_SECONDS == 60


@pytest.mark.asyncio
async def test_shell_timeout_and_cancel_cleanup_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, connection = await _connected(tmp_path)
    monkeypatch.setattr(hosted, "SHELL_TIMEOUT_SECONDS", 1)
    result = await provider.request(
        connection,
        "tools/call",
        {"name": "shell", "arguments": {"command": "sleep 30"}},
    )
    assert result["structuredContent"] == {
        "stdout": "",
        "stderr": "\n[timed out after 1 s]",
        "exitCode": -1,
    }
    assert not provider._processes

    monkeypatch.setattr(hosted, "SHELL_TIMEOUT_SECONDS", 60)
    call = asyncio.create_task(
        provider.request(
            connection,
            "tools/call",
            {"name": "shell", "arguments": {"command": "sleep 30"}},
            request_id="cancel-me",
        )
    )
    while not provider._processes:
        await asyncio.sleep(0)
    await provider.notification(
        connection, "notifications/cancelled", {"requestId": "cancel-me"}
    )
    with pytest.raises(HostedMcpError, match="Request cancelled") as cancelled:
        await call
    assert cancelled.value.code == -32800
    assert not provider._processes


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
