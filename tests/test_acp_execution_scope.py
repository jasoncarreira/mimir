from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mimir.acp import hosted
from mimir.acp.confinement import ConfinementUnavailable, PreparedCommand
from mimir.acp.execution_scope import ScopeApproval, canonical_scope_path, MAX_SCOPE_REQUESTS
from mimir.acp.hosted import HostedHandsProvider, HostedMcpError


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "prepare_command", lambda argv, **kw: PreparedCommand(tuple(argv), {}))
    instance = HostedHandsProvider(request_scope_permission=AsyncMock(return_value=True))
    instance.bind_session("s", tmp_path / "cwd")
    return instance


def test_canonical_scope_reuses_lexical_identity_then_host_resolution(tmp_path):
    target = tmp_path / "target"
    target.touch()
    (tmp_path / "link").symlink_to(target)
    assert canonical_scope_path("./link", tmp_path) == target.resolve()
    assert canonical_scope_path("a/../target", tmp_path) == target.resolve()
    for value in ("/", "missing", "*", "bad\x00", "\ud800"):
        with pytest.raises(ValueError):
            canonical_scope_path(value, tmp_path)


@pytest.mark.asyncio
async def test_exact_grant_query_and_idempotence(provider, tmp_path):
    path = tmp_path / "file"
    path.touch()
    session = provider._sessions["s"]
    provider._python_kernels.retire = AsyncMock()
    result = await provider.request_scope(session, str(path))
    assert result["approved"] and str(path) in result["paths"]
    assert "all variables/imports will be lost" in result["message"]
    assert str(tmp_path) not in result["paths"]
    assert not session.scope.allows(tmp_path / "sibling")
    assert (await provider.request_scope(session, ""))["paths"] == result["paths"]
    assert (await provider.request_scope(session, str(path)))["approved"]
    provider._request_scope_permission.assert_awaited_once_with("s", ScopeApproval(path, False))
    provider._python_kernels.retire.assert_awaited_once()


@pytest.mark.asyncio
async def test_directory_descendant_does_not_prompt(provider, tmp_path):
    path = tmp_path / "data"
    child = path / "sub" / "deep"
    child.mkdir(parents=True)
    sibling = tmp_path / "data-old"
    sibling.mkdir()
    session = provider._sessions["s"]
    provider._python_kernels.retire = AsyncMock()
    assert (await provider.request_scope(session, str(path)))["approved"]
    for descendant in (child.parent, child):
        assert (await provider.request_scope(session, str(descendant)))["approved"]
    assert not session.scope.allows(tmp_path)
    assert not session.scope.allows(sibling)
    provider._request_scope_permission.assert_awaited_once_with("s", ScopeApproval(path, True))
    provider._python_kernels.retire.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_kind_is_frozen_before_permission_reply(provider, tmp_path):
    path = tmp_path / "file"
    path.touch()
    async def approve(session_id, grant):
        assert grant == ScopeApproval(path, False)
        path.unlink()
        path.mkdir()
        return True
    provider._request_scope_permission = approve
    session = provider._sessions["s"]
    assert (await provider.request_scope(session, str(path)))["approved"]
    assert session.scope.approved == {ScopeApproval(path, False)}
    assert not session.scope.allows(path / "child")


@pytest.mark.asyncio
async def test_denial_is_final_for_alias_and_broader_request(provider, tmp_path):
    path = tmp_path / "file"
    path.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    session = provider._sessions["s"]
    provider._request_scope_permission.return_value = False
    provider._python_kernels.retire = AsyncMock()
    for value in (path, alias, tmp_path):
        result = await provider.request_scope(session, str(value))
        assert not result["approved"]
    provider._request_scope_permission.assert_awaited_once()
    provider._python_kernels.retire.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_bounded_and_late_reply_cannot_install(provider, tmp_path):
    path = tmp_path / "file"
    path.touch()
    entered, answer = asyncio.Event(), asyncio.Event()
    async def approve(*args):
        entered.set()
        await answer.wait()
        return True
    provider._request_scope_permission = approve
    session = provider._sessions["s"]
    task = asyncio.create_task(provider.request_scope(session, str(path)))
    await entered.wait()
    assert not (await provider.request_scope(session, str(path)))["approved"]
    session.scope.invalidate()
    answer.set()
    assert not (await task)["approved"]
    assert not session.scope.approved
    session.scope.attempts = MAX_SCOPE_REQUESTS
    assert not (await provider.request_scope(session, str(path)))["approved"]


@pytest.mark.asyncio
async def test_unavailable_never_spawns(provider, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ConfinementUnavailable("test backend unavailable")
    monkeypatch.setattr(hosted, "prepare_command", unavailable)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(HostedMcpError, match="confinement|backend unavailable"):
        await provider._shell(provider._sessions["s"], "true")
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt integration")
async def test_real_shell_and_persistent_python_scope(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    outside = tmp_path / "approved"
    sibling = tmp_path / "sibling"
    outside.write_text("test")
    sibling.write_text("test")
    (cwd / "escape").symlink_to(sibling)
    approve = AsyncMock(return_value=True)
    provider = HostedHandsProvider(request_scope_permission=approve)
    provider.bind_session("s", cwd)
    session = provider._sessions["s"]
    try:
        assert (await provider._shell(session, "printf test > inside; test -r inside"))["exitCode"] == 0
        first = await provider.execute_python(session, "from pathlib import Path\nPath('python').write_text('test')\nkept=42\nPath('inside').read_text() == 'test'")
        assert first["value"] == "True"
        async def check(path, allowed):
            shell = await provider._shell(session, f"if cat '{path}' >/dev/null 2>/dev/null; then printf allowed; else printf blocked; fi")
            assert shell["stdout"] == ("allowed" if allowed else "blocked")
            python = await provider.execute_python(session, f"try:\n    Path({str(path)!r}).read_bytes()\n    allowed=True\nexcept OSError:\n    allowed=False\nallowed")
            assert python["value"] == str(allowed)
            assert python["kernel"] == "reused"
        for path in (outside, sibling, cwd / "escape", Path("/etc/hosts")):
            await check(path, False)
        approve.return_value = False
        assert not (await provider.request_scope(session, str(sibling)))["approved"]
        assert (await provider.execute_python(session, "kept"))["value"] == "42"
        approve.return_value = True
        grant = await provider.request_scope(session, str(outside))
        assert grant["approved"] and "lost" in grant["message"]
        reset = await provider.execute_python(session, "from pathlib import Path\n'kept' in globals()")
        assert reset["kernel"] == "fresh" and reset["value"] == "False"
        await check(outside, True)
        for path in (sibling, cwd / "escape", Path("/etc/hosts")):
            await check(path, False)
        empty = await provider._shell(session, "cat /etc/hosts >/dev/null 2>/dev/null || true")
        assert empty == {"stdout": "", "stderr": "", "exitCode": 0}
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt integration")
async def test_approved_path_replaced_by_symlink_does_not_widen_python(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    approved = tmp_path / "approved"
    victim = tmp_path / "victim"
    approved.touch()
    victim.touch()
    provider = HostedHandsProvider(request_scope_permission=AsyncMock(return_value=True))
    provider.bind_session("s", cwd)
    session = provider._sessions["s"]
    try:
        assert (await provider.request_scope(session, str(approved)))["approved"]
        approved.unlink()
        approved.symlink_to(victim)
        for path in (approved, victim):
            result = await provider.execute_python(session, f"try:\n    open({str(path)!r}).read()\n    allowed=True\nexcept OSError:\n    allowed=False\nallowed")
            assert result["value"] == "False"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt subprocess integration")
async def test_children_do_not_inherit_parent_protocol_input(tmp_path):
    script = "\n".join([
        "import asyncio, json",
        "from mimir.acp.hosted import HostedHandsProvider",
        "async def main():",
        "    p = HostedHandsProvider()",
        f"    p.bind_session('s', {str(tmp_path)!r})",
        "    s = p._sessions['s']",
        "    try:",
        "        shell = await p._shell(s, 'cat')",
        "        python = await p.execute_python(s, 'import os\\nos.read(0, 100)')",
        "        print(json.dumps([shell['stdout'], python['value']]))",
        "    finally:",
        "        await p.close()",
        "asyncio.run(main())",
    ])
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(b"fake-private-ACP-message")
    assert process.returncode == 0, stderr.decode()
    import json
    assert json.loads(stdout) == ["", "b''"]
