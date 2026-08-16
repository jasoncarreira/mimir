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

import pytest

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
        lambda home, lane="agent": OpenResult(ok=True, branch="proposal/x", worktree=wt),
    )
    out = _inv(tp.open_proposal)
    assert "scratch/proposals/proposal_x/memory/core/" in out
    assert "scratch/proposals/proposal_x/prompts/" in out
    assert "submit_proposal" in out
    assert "lane='agent'" in out


def test_open_tool_no_remote(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home, lane="agent": OpenResult(
            ok=False, branch=None, worktree=None, reason="no_remote", detail="x"
        ),
    )
    assert "no git remote" in _inv(tp.open_proposal).lower()


def test_open_tool_already_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "proposals" / "proposal_y").resolve()
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home, lane="agent": OpenResult(
            ok=False, branch="proposal/y", worktree=wt, reason="exists", detail="x"
        ),
    )
    out = _inv(tp.open_proposal)
    assert "already open" in out and "proposal/y" in out


def test_open_tool_forwards_lane(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    captured: dict = {}
    wt = (tmp_path / "scratch" / "proposals" / "upgrade" / "upgrade_x").resolve()

    def fake(home, lane="agent"):
        captured["lane"] = lane
        return OpenResult(ok=True, branch="upgrade/x", worktree=wt)

    monkeypatch.setattr(tp, "_open_proposal", fake)
    out = _inv(tp.open_proposal, lane="upgrade")
    assert captured == {"lane": "upgrade"}
    assert "upgrade" in out and "scratch/proposals/upgrade/upgrade_x" in out


def test_open_tool_missing_home(monkeypatch) -> None:
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert "MIMIR_HOME not set" in _inv(tp.open_proposal)


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (tp.open_proposal, {}),
        (tp.submit_proposal, {"title": "title", "rationale": "reason"}),
        (tp.abandon_proposal, {}),
    ],
)
def test_invalid_lane_returns_tool_failure_string(monkeypatch, tmp_path, tool, kwargs) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))

    result = _inv(tool, lane="manual", **kwargs)

    assert result.startswith(f"{tool.name} failed (")
    assert "unsupported proposal lane" in result


# ─── submit ──────────────────────────────────────────────────────────


def test_submit_tool_returns_url_forwards_args_and_emits_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    captured: dict = {}
    events: list = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr(tp, "log_event", fake_log)

    def fake(home, *, title, rationale, lane="agent"):
        captured.update(title=title, rationale=rationale, lane=lane)
        return ProposalResult(
            ok=True, branch="b", pushed=True,
            pr_url="https://github.com/x/y/pull/3", reason=None,
        )

    monkeypatch.setattr(tp, "_finalize_proposal", fake)
    out = _inv(tp.submit_proposal, title="T", rationale="R", lane="upgrade")
    assert "https://github.com/x/y/pull/3" in out and "merge" in out.lower()
    assert captured == {"title": "T", "rationale": "R", "lane": "upgrade"}
    # Positive feedback event emitted with the PR URL (chainlink #337/#339/#344).
    assert events and events[0][0] == "proposal_pr_opened"
    assert events[0][1]["pr_url"] == "https://github.com/x/y/pull/3"
    assert events[0][1]["lane"] == "upgrade"


def test_submit_tool_no_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch=None, pushed=False, pr_url=None, reason="no_open", detail="x"
        ),
    )
    with pytest.raises(tp.ProposalSubmissionError) as raised:
        _inv(tp.submit_proposal, title="t", rationale="r", lane="upgrade")
    assert raised.value.reason == "no_open"
    assert raised.value.lane == "upgrade"
    assert "no `upgrade` proposal is open" in str(raised.value)


def test_submit_tool_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch="b", pushed=False, pr_url=None,
            reason="secret", detail="contains a secret-shaped token",
        ),
    )
    with pytest.raises(tp.ProposalSubmissionError, match="secret") as raised:
        _inv(tp.submit_proposal, title="t", rationale="r")
    assert raised.value.reason == "secret"
    assert raised.value.lane == "agent"


def test_submit_tool_requires_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    with pytest.raises(tp.ProposalSubmissionError, match="required") as raised:
        _inv(tp.submit_proposal, title="", rationale="r")
    assert raised.value.reason == "invalid_arguments"


@pytest.mark.parametrize("lane", ["agent", "upgrade"])
def test_submit_tool_pr_failure_is_typed_for_every_lane(monkeypatch, tmp_path, lane) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False,
            branch=f"{lane}/change",
            pushed=True,
            pr_url=None,
            reason="pr_open",
            detail="gh pr create failed: authentication required",
        ),
    )

    with pytest.raises(tp.ProposalSubmissionError) as raised:
        _inv(tp.submit_proposal, title="t", rationale="r", lane=lane)

    assert raised.value.reason == "pr_open"
    assert raised.value.lane == lane
    assert "gh pr create failed" in str(raised.value)


# ─── abandon ─────────────────────────────────────────────────────────


def test_abandon_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "list_open_proposals", lambda home, lane="agent": [("upgrade/z", tmp_path)])
    monkeypatch.setattr(tp, "_abandon_proposal", lambda home, lane="agent": True)
    out = _inv(tp.abandon_proposal, lane="upgrade")
    assert "upgrade/z" in out and "upgrade" in out and "bandon" in out


def test_abandon_tool_nothing_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "list_open_proposals", lambda home, lane="agent": [])
    monkeypatch.setattr(tp, "_abandon_proposal", lambda home, lane="agent": False)
    assert "nothing to abandon" in _inv(tp.abandon_proposal, lane="upgrade").lower()
