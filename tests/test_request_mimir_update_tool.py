"""Tests for the ``request_mimir_update`` tool — the operator-approval
side of the pending-update flag flow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.tools.registry import request_mimir_update
from mimir.update_on_start import _read_flag, flag_path


@pytest.mark.asyncio
async def test_writes_flag_at_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default args → bare flag (no pin, no --pre). Verifies the path
    + the parsed contents from the startup side's perspective."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    result = await request_mimir_update.ainvoke({})
    assert "Pending-update flag written" in result
    assert str(flag_path(tmp_path)) in result

    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == ""
    assert parsed.include_prereleases is False
    assert parsed.approved_at is not None


@pytest.mark.asyncio
async def test_pinned_version_carried_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator approved a specific release → the pin survives to
    startup-side parsing."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    result = await request_mimir_update.ainvoke({"target_version": "0.2.0"})
    assert "pinned to 0.2.0" in result

    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == "0.2.0"


@pytest.mark.asyncio
async def test_include_prereleases_flag_carried_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--pre is opt-in. The tool's parameter routes through to the
    flag's metadata so startup-side install passes ``--pre`` to pip."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    result = await request_mimir_update.ainvoke({
        "target_version": "0.2.0rc1",
        "include_prereleases": True,
    })
    assert "pre-releases allowed" in result

    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == "0.2.0rc1"
    assert parsed.include_prereleases is True


@pytest.mark.asyncio
async def test_strips_whitespace_in_target_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator pasted ``" 0.2.0 "`` from somewhere — strip it."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    await request_mimir_update.ainvoke({"target_version": "  0.2.0  "})
    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == "0.2.0"


@pytest.mark.asyncio
async def test_missing_mimir_home_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``MIMIR_HOME`` env → the tool returns an error string and
    doesn't write anywhere. Shouldn't happen in normal deployments
    but defensive."""
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    result = await request_mimir_update.ainvoke({})
    assert "request_mimir_update failed" in result
    assert "MIMIR_HOME" in result


@pytest.mark.asyncio
async def test_overwrites_existing_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator changed their mind — second invocation overrides the
    first. Common case: approved a pin, then changed to ""latest""."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    await request_mimir_update.ainvoke({"target_version": "0.2.0"})
    await request_mimir_update.ainvoke({})  # second call: empty target

    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == ""


@pytest.mark.asyncio
async def test_tool_is_coroutine() -> None:
    """Sanity — same shape as spawn_claude_code (async, no sync
    fallback). Deepagents routes async tools via ``coroutine``."""
    import asyncio
    assert request_mimir_update.coroutine is not None
    assert asyncio.iscoroutinefunction(request_mimir_update.coroutine)
    assert request_mimir_update.func is None


@pytest.mark.asyncio
async def test_tool_in_all_mimir_tools() -> None:
    """The tool is wired into ``all_mimir_tools`` so deepagents
    discovers it at agent construction."""
    from mimir.tools.registry import all_mimir_tools
    tools = all_mimir_tools()
    names = {t.name for t in tools}
    assert "request_mimir_update" in names
