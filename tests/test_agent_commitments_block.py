"""Phase 3 — agent's ``_assemble_commitments_block`` integration.

Verifies the channel-scoping + synthetic-channel-skip behavior on top
of the pure render layer (covered by ``test_commitments_render.py``).
Builds a real ``Agent`` against a tmp ``CommitmentsStore`` and seeds
records before calling the assembler directly — no LLM, no SDK
roundtrip.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from mimir.agent import Agent
from mimir.commitments import CommitmentRecord
from mimir.config import Config
from mimir.event_logger import init_logger
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import make_process_session_id
from mimir.session_manager import SessionManager
from mimir.turn_logger import TurnLogger

from ._fake_saga import FakeSaga


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.from_env()
    return replace(cfg, home=tmp_path)


def _build_agent(tmp_path: Path) -> Agent:
    cfg = _cfg(tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(cfg.events_log, make_process_session_id())
    turn_logger = TurnLogger(cfg.turns_log)
    buf = MessageBuffer(history_path=cfg.home / "messages" / "chat_history.jsonl")
    indexes = IndexGenerator(cfg.home)
    return Agent(
        cfg,
        turn_logger,
        buf,
        indexes,
        indexer=None,
        saga_client=FakeSaga(),
        session_manager=SessionManager(idle_minutes=60),
    )


def _seed(agent: Agent, **kw) -> CommitmentRecord:
    return asyncio.run(agent._commitments.add(CommitmentRecord(**kw)))


def test_block_none_when_no_records(tmp_path: Path):
    agent = _build_agent(tmp_path)
    assert agent._assemble_commitments_block("ch-1") is None


def test_block_none_for_none_channel(tmp_path: Path):
    """Direct-call paths without a channel (synthesis turns, batch
    extract jobs) get None — the block is channel-scoped by design."""
    agent = _build_agent(tmp_path)
    _seed(agent, id="c-a", channel_id="ch-1", text="X")
    assert agent._assemble_commitments_block(None) is None


def test_block_suppressed_on_scheduler_channel(tmp_path: Path):
    """Synthetic ``scheduler:*`` channels skip the block — a heartbeat
    tick has no agent-the-operator-promised-someone context."""
    agent = _build_agent(tmp_path)
    _seed(agent, id="c-a", channel_id="ch-1", text="X")
    # Even an unbound record should not surface on a synthetic channel
    _seed(agent, id="c-u", channel_id=None, text="U")
    assert agent._assemble_commitments_block("scheduler:heartbeat") is None


def test_block_suppressed_on_poller_channel(tmp_path: Path):
    agent = _build_agent(tmp_path)
    _seed(agent, id="c-a", channel_id="ch-1", text="X")
    _seed(agent, id="c-u", channel_id=None, text="U")
    assert agent._assemble_commitments_block("poller:github") is None


def test_block_includes_channel_bound_and_unbound(tmp_path: Path):
    agent = _build_agent(tmp_path)
    _seed(agent, id="c-bound", channel_id="ch-target", text="bound work")
    _seed(agent, id="c-other", channel_id="ch-other", text="other work")
    _seed(agent, id="c-unbound", channel_id=None, text="unbound work")

    out = agent._assemble_commitments_block("ch-target")
    assert out is not None
    assert "c-bound" in out
    assert "c-unbound" in out
    assert "c-other" not in out  # other channel's commitments hidden


def test_block_excludes_terminal_records(tmp_path: Path):
    agent = _build_agent(tmp_path)
    rec = _seed(agent, id="c-done", channel_id="ch-1", text="done thing")
    asyncio.run(agent._commitments.complete(rec.id))
    _seed(agent, id="c-pending", channel_id="ch-1", text="still pending")

    out = agent._assemble_commitments_block("ch-1")
    assert out is not None
    assert "c-pending" in out
    assert "c-done" not in out
