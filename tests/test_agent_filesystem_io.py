from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mimir.agent import Agent
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import (
    AgentEvent,
    AuthContext,
    InformationFlowLabels,
    PromptBlock,
    SourceLabel,
    TurnContext,
)
from mimir.turn_logger import TurnLogger


@pytest.fixture
def wiki_agent(tmp_path):
    agent = Agent.__new__(Agent)
    agent._config = SimpleNamespace(home=tmp_path)
    return agent


@pytest.mark.parametrize("operation", ["is_dir", "rglob", "stat"])
async def test_snapshot_full_scan_runs_on_wiki_pool(
    wiki_agent, tmp_path, monkeypatch, operation,
):
    wiki = tmp_path / "state" / "wiki"
    nested = wiki / "nested"
    nested.mkdir(parents=True)
    pages = [wiki / "root.md", nested / "page.md"]
    for page in pages:
        page.write_text("content")
    expected = {str(page): page.stat().st_mtime for page in pages}
    for name in ("orphans.md", "dangling-links.md", "backlinks-index.md", "notes.txt"):
        (wiki / name).write_text("excluded")

    entered = threading.Event()
    release = threading.Event()
    observed = set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr("mimir.wiki_backlinks._BACKLINKS_EXECUTOR", pool)
        worker = pool.submit(threading.get_ident).result(timeout=5)

        def observe(method):
            observed.add(method)
            assert threading.get_ident() == worker
            if method == operation:
                entered.set()
                assert release.wait(5), "loop did not release wiki scan"

        original_is_dir = Path.is_dir
        original_rglob = Path.rglob
        original_stat = Path.stat

        def is_dir(path):
            if path == wiki:
                observe("is_dir")
            return original_is_dir(path)

        def rglob(path, *args, **kwargs):
            # A generator keeps the observation inside traversal, not just
            # the creation of the lazy iterator.
            if path == wiki:
                observe("rglob")
            yield from original_rglob(path, *args, **kwargs)

        def stat(path, *args, **kwargs):
            if path in pages:
                observe("stat")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "is_dir", is_dir)
        monkeypatch.setattr(Path, "rglob", rglob)
        monkeypatch.setattr(Path, "stat", stat)
        task = asyncio.create_task(wiki_agent._snapshot_wiki_mtimes_async())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            # This task advances while our filesystem worker is blocked.
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert task.result() == expected
        assert observed == {"is_dir", "rglob", "stat"}


async def test_snapshot_missing_wiki_is_empty(wiki_agent):
    assert await wiki_agent._snapshot_wiki_mtimes_async() == {}


@pytest.mark.parametrize("change", ["add", "edit", "delete", "generated", "unchanged"])
async def test_post_turn_backlinks_compares_fresh_nested_snapshots(
    wiki_agent, tmp_path, monkeypatch, change,
):
    wiki = tmp_path / "state" / "wiki"
    nested = wiki / "nested" / "deeper"
    nested.mkdir(parents=True)
    page = nested / "page.md"
    page.write_text("before")
    generated = [wiki / name for name in (
        "orphans.md", "dangling-links.md", "backlinks-index.md",
    )]
    for output in generated:
        output.write_text("generated")
    snapshot = await wiki_agent._snapshot_wiki_mtimes_async()
    ctx = SimpleNamespace(wiki_mtime_snapshot=snapshot)
    run = AsyncMock()
    monkeypatch.setattr("mimir.wiki_backlinks.run", run)

    if change == "add":
        (nested / "added.md").write_text("new page")
    elif change == "delete":
        page.unlink()
    elif change in {"edit", "generated"}:
        for target in ([page] if change == "edit" else generated):
            before = target.stat()
            target.write_text("after")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    await wiki_agent._post_turn_wiki_backlinks(ctx)
    if change in {"add", "edit", "delete"}:
        run.assert_awaited_once_with(tmp_path)
    else:
        run.assert_not_awaited()


async def test_prompt_assembles_commitments_off_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    config = Config.from_env()
    (config.home / "logs").mkdir(parents=True, exist_ok=True)
    agent = Agent(
        config=config,
        turn_logger=TurnLogger(config.turns_log),
        message_buffer=MessageBuffer(history_path=config.home / "messages.jsonl"),
        index_generator=IndexGenerator(config.home),
    )
    auth = AuthContext(
        principal="alice", canonical_principal="alice", roles=(),
        event_ingress=None, trigger="user_message", channel_id="ch-io",
        interactivity=None,
    )
    ctx = TurnContext(
        turn_id="filesystem-io", session_id="ch-io", trigger="user_message",
        channel_id="ch-io", started_at=0, auth_context=auth,
    )
    event = AgentEvent(
        trigger="user_message", channel_id="ch-io", author="alice", content="hello",
    )
    block = PromptBlock("COMMITMENTS_IO_SENTINEL", InformationFlowLabels().with_source(
        SourceLabel(
            principal="alice", domain="commitments", resource_id="ch-io",
            bridge_instance="test", sensitivity="private",
            authorized_principals=frozenset({"alice"}), source_kind="protected_prompt",
        ),
    ))
    monkeypatch.setattr(agent, "_select_recent_activity", lambda *args: ([], ()))
    monkeypatch.setattr(agent, "_assemble_session_summaries", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_assemble_usage_block", lambda *args: (None, []))
    monkeypatch.setattr(agent, "_assemble_upcoming_block", lambda *args: None)
    monkeypatch.setattr(agent, "_assemble_self_state_block", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent._feedback, "recent_prompt_block", lambda *args: None)
    monkeypatch.setattr("mimir.core_blocks.load_channel_memory", lambda *args: None)
    monkeypatch.setattr("mimir.skill_resolver.find_skill_for_channel", lambda *args: None)
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    calls = []

    def commitments(channel_id, auth_context):
        calls.append((channel_id, auth_context))
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5), "loop did not release commitments assembly"
        return block

    monkeypatch.setattr(agent, "_assemble_commitments_block", commitments)
    task = asyncio.create_task(agent._build_turn_prompt(ctx, event, saga_block=None))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    prompt, recent = task.result()
    assert calls == [(event.channel_id, auth)]
    assert "COMMITMENTS_IO_SENTINEL" in prompt
    assert block.labels.sources <= ctx.ifc_labels.sources
    assert recent == []
