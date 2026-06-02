"""Tests for the change-proposal agent tools (chainlink #339/#344).

The git mechanics are covered by test_proposals.py; here we test the tool
layer — MIMIR_HOME resolution, arg forwarding, and the operator-facing message
for each outcome — with the library stubbed.

The tool functions (``open_proposal`` / ``submit_proposal`` / ``abandon_proposal``)
share names with the library functions, so the module imports the library under
private aliases (``_open_proposal`` etc.); the stubs target those.
"""

from __future__ import annotations

import asyncio

from mimir.proposals import OpenResult, ProposalResult
from mimir.tools import proposals as tp


def _inv(tool, **kwargs) -> str:
    return asyncio.run(tool.ainvoke(kwargs))


# ─── open ────────────────────────────────────────────────────────────


def test_open_tool_returns_edit_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "proposals" / "proposal_x").resolve()
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home: OpenResult(ok=True, branch="proposal/x", worktree=wt),
    )
    out = _inv(tp.open_proposal)
    assert "scratch/proposals/proposal_x/memory/core/" in out
    assert "scratch/proposals/proposal_x/prompts/" in out
    assert "submit_proposal" in out


def test_open_tool_no_remote(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home: OpenResult(
            ok=False, branch=None, worktree=None, reason="no_remote", detail="x"
        ),
    )
    assert "no git remote" in _inv(tp.open_proposal).lower()


def test_open_tool_already_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "proposals" / "proposal_y").resolve()
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home: OpenResult(
            ok=False, branch="proposal/y", worktree=wt, reason="exists", detail="x"
        ),
    )
    out = _inv(tp.open_proposal)
    assert "already open" in out and "proposal/y" in out


def test_open_tool_missing_home(monkeypatch) -> None:
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert "MIMIR_HOME not set" in _inv(tp.open_proposal)


# ─── submit ──────────────────────────────────────────────────────────


def test_submit_tool_returns_url_forwards_args_and_emits_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    captured: dict = {}
    events: list = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr(tp, "log_event", fake_log)

    def fake(home, *, title, rationale):
        captured.update(title=title, rationale=rationale)
        return ProposalResult(
            ok=True, branch="b", pushed=True,
            pr_url="https://github.com/x/y/pull/3", reason=None,
        )

    monkeypatch.setattr(tp, "_finalize_proposal", fake)
    out = _inv(tp.submit_proposal, title="T", rationale="R")
    assert "https://github.com/x/y/pull/3" in out and "merge" in out.lower()
    assert captured == {"title": "T", "rationale": "R"}
    # Positive feedback event emitted with the PR URL (chainlink #337/#339/#344).
    assert events and events[0][0] == "proposal_pr_opened"
    assert events[0][1]["pr_url"] == "https://github.com/x/y/pull/3"


def test_submit_tool_no_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch=None, pushed=False, pr_url=None, reason="no_open", detail="x"
        ),
    )
    assert "no proposal is open" in _inv(
        tp.submit_proposal, title="t", rationale="r"
    )


def test_submit_tool_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch="b", pushed=False, pr_url=None,
            reason="secret", detail="contains a secret-shaped token",
        ),
    )
    assert "secret" in _inv(
        tp.submit_proposal, title="t", rationale="r"
    ).lower()


def test_submit_tool_requires_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert "required" in _inv(
        tp.submit_proposal, title="", rationale="r"
    ).lower()


# ─── abandon ─────────────────────────────────────────────────────────


def test_abandon_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "list_open_proposals", lambda home: [("proposal/z", tmp_path)])
    monkeypatch.setattr(tp, "_abandon_proposal", lambda home: True)
    out = _inv(tp.abandon_proposal)
    assert "proposal/z" in out and "bandon" in out


def test_abandon_tool_nothing_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "list_open_proposals", lambda home: [])
    monkeypatch.setattr(tp, "_abandon_proposal", lambda home: False)
    assert "nothing to abandon" in _inv(tp.abandon_proposal).lower()
