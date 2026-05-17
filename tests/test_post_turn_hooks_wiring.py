"""181-M regression: three end-of-turn hooks ported from main.

The SDK-era ``_turn_hooks`` chain ran three side-effecting hooks at
finalize:

  - WikiBacklinksHook  → regenerate state/wiki/{orphans,
    dangling-links,backlinks-index}.md when a wiki content page
    was edited this turn.
  - IndexRebuildHook   → mark_dirty + flush state/INDEX.md +
    memory/INDEX.md (debounced).
  - GitCommitHook      → commit any memory/state changes from this
    turn + debounced push, gated on MIMIR_GIT_TRACKING_ENABLED.

The deepagents agent has no hook chain. 181-M ports these as
``Agent._post_turn_*`` methods invoked at the end of
``_run_turn_body``, after commitment extraction.

Tests stub the underlying ``mimir.wiki_backlinks.run``,
``IndexGenerator.flush``, and ``mimir.git_tracking.commit_turn_changes``
calls to capture invocation without filesystem / subprocess effects.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mimir.agent import Agent
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, TurnContext
from mimir.turn_logger import TurnLogger


def _make_agent(tmp_path: Path) -> Agent:
    os.environ["MIMIR_HOME"] = str(tmp_path)
    cfg = Config.from_env()
    (cfg.home / "logs").mkdir(parents=True, exist_ok=True)
    return Agent(
        config=cfg,
        turn_logger=TurnLogger(cfg.turns_log),
        message_buffer=MessageBuffer(history_path=cfg.home / "messages.jsonl"),
        index_generator=IndexGenerator(cfg.home),
    )


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t-hook",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
    )


# ─── WikiBacklinksHook ─────────────────────────────────────────────


def _write_wiki_page(home: Path, name: str, body: str = "x") -> Path:
    wiki = home / "state" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    p = wiki / name
    p.write_text(body)
    return p


@pytest.mark.asyncio
async def test_wiki_backlinks_skips_when_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no wiki page mtime changed between pre-turn snapshot and
    finalize, wiki_backlinks.run must NOT fire (no regen churn)."""
    agent = _make_agent(tmp_path)
    _write_wiki_page(tmp_path, "page-a.md")

    ran = AsyncMock()
    monkeypatch.setattr("mimir.wiki_backlinks.run", ran)

    ctx = _ctx()
    ctx.wiki_mtime_snapshot = agent._snapshot_wiki_mtimes()
    # No mtime changes in between.
    await agent._post_turn_wiki_backlinks(ctx)
    ran.assert_not_awaited()


@pytest.mark.asyncio
async def test_wiki_backlinks_fires_on_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A content page whose mtime moved between snapshot + finalize
    triggers a single ``wiki_backlinks.run(home)`` call."""
    agent = _make_agent(tmp_path)
    p = _write_wiki_page(tmp_path, "page-a.md")

    ran = AsyncMock(return_value={"orphans": [], "dangling": []})
    monkeypatch.setattr("mimir.wiki_backlinks.run", ran)

    ctx = _ctx()
    ctx.wiki_mtime_snapshot = agent._snapshot_wiki_mtimes()
    # Mutate the wiki page so its mtime advances.
    time.sleep(0.01)
    p.write_text("changed body")

    await agent._post_turn_wiki_backlinks(ctx)
    ran.assert_awaited_once_with(agent._config.home)


@pytest.mark.asyncio
async def test_wiki_backlinks_fires_on_new_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand-new wiki page added during the turn also triggers regen."""
    agent = _make_agent(tmp_path)
    # Wiki dir exists but no pages yet at snapshot time.
    (tmp_path / "state" / "wiki").mkdir(parents=True, exist_ok=True)

    ran = AsyncMock()
    monkeypatch.setattr("mimir.wiki_backlinks.run", ran)

    ctx = _ctx()
    ctx.wiki_mtime_snapshot = agent._snapshot_wiki_mtimes()
    _write_wiki_page(tmp_path, "added-mid-turn.md")

    await agent._post_turn_wiki_backlinks(ctx)
    ran.assert_awaited_once()


@pytest.mark.asyncio
async def test_wiki_backlinks_ignores_generated_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Touching ``orphans.md`` / ``dangling-links.md`` / ``backlinks-
    index.md`` must NOT trigger another regen — otherwise the hook
    loops on its own writes."""
    agent = _make_agent(tmp_path)
    # Set up a wiki with an actual content page (so it survives
    # snapshot) plus the generated outputs.
    _write_wiki_page(tmp_path, "real.md")
    _write_wiki_page(tmp_path, "orphans.md", body="generated")

    ran = AsyncMock()
    monkeypatch.setattr("mimir.wiki_backlinks.run", ran)

    ctx = _ctx()
    ctx.wiki_mtime_snapshot = agent._snapshot_wiki_mtimes()
    # Only touch a generated output; the snapshot didn't include it,
    # and the after-walk won't include it either — should be no diff.
    time.sleep(0.01)
    (tmp_path / "state" / "wiki" / "orphans.md").write_text("regenerated")

    await agent._post_turn_wiki_backlinks(ctx)
    ran.assert_not_awaited()


@pytest.mark.asyncio
async def test_wiki_backlinks_swallow_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise inside wiki_backlinks.run must not propagate — turn
    record stays intact."""
    agent = _make_agent(tmp_path)
    p = _write_wiki_page(tmp_path, "p.md")

    async def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("regen boom")

    monkeypatch.setattr("mimir.wiki_backlinks.run", _boom)

    ctx = _ctx()
    ctx.wiki_mtime_snapshot = agent._snapshot_wiki_mtimes()
    time.sleep(0.01)
    p.write_text("changed")
    # Must not raise.
    await agent._post_turn_wiki_backlinks(ctx)


# ─── IndexRebuildHook ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_rebuild_calls_mark_dirty_and_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)
    mark_calls: list[str] = []
    flush = AsyncMock()
    monkeypatch.setattr(agent._indexes, "mark_dirty", lambda scope="all": mark_calls.append(scope))
    monkeypatch.setattr(agent._indexes, "flush", flush)

    await agent._post_turn_index_rebuild()
    assert mark_calls == ["all"]
    flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_index_rebuild_swallows_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)

    async def _flush_boom() -> None:
        raise RuntimeError("flush boom")

    monkeypatch.setattr(agent._indexes, "flush", _flush_boom)
    # Must not raise — log + continue.
    await agent._post_turn_index_rebuild()


# ─── GitCommitHook ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_git_commit_threads_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)
    captured: list[dict] = []

    async def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("mimir.git_tracking.commit_turn_changes", _capture)

    ctx = _ctx()
    await agent._post_turn_git_commit(ctx)
    assert len(captured) == 1
    assert captured[0]["turn_id"] == ctx.turn_id
    assert captured[0]["trigger"] == ctx.trigger
    assert captured[0]["home"] == agent._config.home
    # The enabled flag mirrors config.git_tracking_enabled (default
    # depends on env; assertion checks the threading, not the value).
    assert "enabled" in captured[0]


@pytest.mark.asyncio
async def test_git_commit_swallows_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)

    async def _boom(**kwargs: Any) -> None:
        raise RuntimeError("commit boom")

    monkeypatch.setattr("mimir.git_tracking.commit_turn_changes", _boom)
    ctx = _ctx()
    # Must not raise — the hook surface promises best-effort.
    await agent._post_turn_git_commit(ctx)
