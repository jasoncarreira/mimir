"""Tests for the core-memory proposal agent tools (chainlink #339).

The git mechanics are covered by test_core_memory_pr.py; here we test the tool
layer — MIMIR_HOME resolution, arg forwarding, and the operator-facing message
for each outcome — with the library stubbed.
"""

from __future__ import annotations

import asyncio

from mimir.core_memory_pr import OpenResult, ProposalResult
from mimir.tools import core_memory as cm


def _inv(tool, **kwargs) -> str:
    return asyncio.run(tool.ainvoke(kwargs))


# ─── open ────────────────────────────────────────────────────────────


def test_open_tool_returns_edit_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "core-proposals" / "core-memory_x").resolve()
    monkeypatch.setattr(
        cm, "open_proposal",
        lambda home: OpenResult(ok=True, branch="core-memory/x", worktree=wt),
    )
    out = _inv(cm.open_core_memory_proposal)
    assert "scratch/core-proposals/core-memory_x/memory/core/" in out
    assert "submit_core_memory_proposal" in out


def test_open_tool_no_remote(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        cm, "open_proposal",
        lambda home: OpenResult(
            ok=False, branch=None, worktree=None, reason="no_remote", detail="x"
        ),
    )
    assert "no git remote" in _inv(cm.open_core_memory_proposal).lower()


def test_open_tool_already_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "core-proposals" / "core-memory_y").resolve()
    monkeypatch.setattr(
        cm, "open_proposal",
        lambda home: OpenResult(
            ok=False, branch="core-memory/y", worktree=wt, reason="exists", detail="x"
        ),
    )
    out = _inv(cm.open_core_memory_proposal)
    assert "already open" in out and "core-memory/y" in out


def test_open_tool_missing_home(monkeypatch) -> None:
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert "MIMIR_HOME not set" in _inv(cm.open_core_memory_proposal)


# ─── submit ──────────────────────────────────────────────────────────


def test_submit_tool_returns_url_and_forwards_args(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    captured: dict = {}

    def fake(home, *, title, rationale):
        captured.update(title=title, rationale=rationale)
        return ProposalResult(
            ok=True, branch="b", pushed=True,
            pr_url="https://github.com/x/y/pull/3", reason=None,
        )

    monkeypatch.setattr(cm, "finalize_proposal", fake)
    out = _inv(cm.submit_core_memory_proposal, title="T", rationale="R")
    assert "https://github.com/x/y/pull/3" in out and "merge" in out.lower()
    assert captured == {"title": "T", "rationale": "R"}


def test_submit_tool_no_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        cm, "finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch=None, pushed=False, pr_url=None, reason="no_open", detail="x"
        ),
    )
    assert "no proposal is open" in _inv(
        cm.submit_core_memory_proposal, title="t", rationale="r"
    )


def test_submit_tool_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        cm, "finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch="b", pushed=False, pr_url=None,
            reason="secret", detail="contains a secret-shaped token",
        ),
    )
    assert "secret" in _inv(
        cm.submit_core_memory_proposal, title="t", rationale="r"
    ).lower()


def test_submit_tool_requires_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert "required" in _inv(
        cm.submit_core_memory_proposal, title="", rationale="r"
    ).lower()


# ─── abandon ─────────────────────────────────────────────────────────


def test_abandon_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(cm, "list_open_proposals", lambda home: [("core-memory/z", tmp_path)])
    monkeypatch.setattr(cm, "abandon_proposal", lambda home: True)
    out = _inv(cm.abandon_core_memory_proposal)
    assert "core-memory/z" in out and "bandon" in out


def test_abandon_tool_nothing_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(cm, "list_open_proposals", lambda home: [])
    monkeypatch.setattr(cm, "abandon_proposal", lambda home: False)
    assert "nothing to abandon" in _inv(cm.abandon_core_memory_proposal).lower()
