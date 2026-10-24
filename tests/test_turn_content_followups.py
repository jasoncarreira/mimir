"""Discriminating regressions for turn-content review follow-ups."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import _shell_reads_home_content, ensure_turn_scratch
from mimir.turn_stores import close_turn_stores, sweep_turn_stores, turn_store, wipe_turn_stores


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize("command", ["cat *.jsonl", "head -20 *", "rg x ."])
def test_remembered_content_store_cwd_taints_without_named_operand(tmp_path, monkeypatch, tool, command):
    from mimir.tools import extra

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(extra, "_effective_shell_cwd", lambda: tmp_path / "logs")
    assert _shell_reads_home_content({"command": command}, tool_name=tool)


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
def test_narrow_search_from_repository_stays_clean(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    assert not _shell_reads_home_content(
        {"command": "rg foo src", "cwd": str(tmp_path / "checkout")}, tool_name=tool,
    )


def test_startup_sweeps_nested_research_drafts_but_preserves_operator_audit(tmp_path):
    draft = tmp_path / "state/pollers/arxiv-agent-memory/research-123/notes-draft.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("ephemeral")
    cursor = draft.parent / "cursor.json"
    cursor.write_text('{"seen": ["id"]}')
    audit = tmp_path / "state/pollers/gmail-poller/triage-dropped.jsonl"
    audit.parent.mkdir(parents=True)
    payload = '{"message_id":"operator-data","subject":"keep","answers":{}}\n'
    audit.write_text(payload)
    sweep_turn_stores(tmp_path)
    assert not draft.exists()
    assert cursor.read_text() == '{"seen": ["id"]}'
    assert audit.read_text() == payload


@pytest.mark.asyncio
async def test_retained_tool_thread_cannot_allocate_after_final_wipe(tmp_path, monkeypatch):
    from threading import Event

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    ctx = SimpleNamespace(turn_id="retained-thread", turn_scratch_path=ensure_turn_scratch(tmp_path, "retained-thread"))
    entered, resume = Event(), Event()
    ctx_handle = set_current_turn(ctx)
    try:
        assert turn_store("large_tool_results")

        def late_writer():
            entered.set()
            assert resume.wait(10)
            return turn_store("large_tool_results")

        worker = asyncio.create_task(asyncio.to_thread(late_writer))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            close_turn_stores(ctx)
            wipe_turn_stores(tmp_path, ctx.turn_id)
        finally:
            resume.set()
        assert await worker is None
        assert not (tmp_path / "large_tool_results" / ctx.turn_id).exists()
    finally:
        reset_current_turn(ctx_handle)
