"""Subagent inbox + .md definitions (SPEC §4.3, §4.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.subagent_defs import seed_subagent_defs
from mimir.subagent_inbox import (
    SubagentInbox,
    SubagentResult,
    read_output_file,
    render_subagent_updates,
)


def test_seed_creates_three_subagents(tmp_path: Path):
    out = seed_subagent_defs(tmp_path)
    assert out == {
        "climber.md": "created",
        "researcher.md": "created",
        "critic.md": "created",
    }
    for name in ("climber.md", "researcher.md", "critic.md"):
        assert (tmp_path / ".claude" / "agents" / name).is_file()


def test_seed_does_not_overwrite_existing(tmp_path: Path):
    target = tmp_path / ".claude" / "agents" / "climber.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-modified", encoding="utf-8")

    out = seed_subagent_defs(tmp_path)
    assert out["climber.md"] == "present"
    assert target.read_text() == "user-modified"


def test_climber_marked_background(tmp_path: Path):
    seed_subagent_defs(tmp_path)
    body = (tmp_path / ".claude" / "agents" / "climber.md").read_text()
    assert "background: true" in body


@pytest.mark.asyncio
async def test_inbox_push_and_drain():
    inbox = SubagentInbox()
    r = SubagentResult(
        task_id="t1",
        status="completed",
        summary="done",
        output_file="/tmp/x.md",
    )
    await inbox.push("c1", r)
    assert inbox.peek("c1")[0].task_id == "t1"

    drained = await inbox.drain("c1")
    assert len(drained) == 1
    assert inbox.peek("c1") == []


@pytest.mark.asyncio
async def test_inbox_isolates_channels():
    inbox = SubagentInbox()
    await inbox.push("c1", SubagentResult(task_id="t1", status="completed", summary="", output_file=None))
    await inbox.push("c2", SubagentResult(task_id="t2", status="completed", summary="", output_file=None))
    assert len(await inbox.drain("c1")) == 1
    assert len(await inbox.drain("c1")) == 0  # already drained
    assert len(await inbox.drain("c2")) == 1


def test_render_subagent_updates_includes_status_and_summary():
    rendered = render_subagent_updates([
        SubagentResult(
            task_id="t1",
            status="completed",
            summary="climbed to 0.92",
            output_file="/tmp/result.md",
            description="optimize the boids reward",
        )
    ])
    assert "[completed]" in rendered
    assert "climbed to 0.92" in rendered
    assert "/tmp/result.md" in rendered
    assert "optimize the boids reward" in rendered


def test_read_output_file_truncates(tmp_path: Path):
    p = tmp_path / "big.md"
    p.write_text("x" * 50_000)
    body = read_output_file(str(p), max_bytes=200)
    assert body is not None
    assert "[truncated]" in body
    assert len(body) < 50_000


def test_read_output_file_returns_none_for_missing():
    assert read_output_file("/tmp/does-not-exist-zzz.md") is None
    assert read_output_file(None) is None
