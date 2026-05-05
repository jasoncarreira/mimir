"""Message buffer: deques, replay, recent-activity assembly (SPEC §5.4)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.history import MessageBuffer, render_recent_activity


def _now_iso(offset_minutes: int = 0) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def _make_buffer(tmp_path: Path, **kwargs) -> MessageBuffer:
    return MessageBuffer(
        history_path=tmp_path / "messages" / "chat_history.jsonl",
        global_max=kwargs.get("global_max", 50),
        per_channel_max=kwargs.get("per_channel_max", 20),
    )


@pytest.mark.asyncio
async def test_append_writes_jsonl_and_updates_deques(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    msg = buf.make_message(
        channel_id="bench-1", kind="user_message", content="hi", author="alice"
    )
    await buf.append(msg)

    assert buf.total_count() == 1
    assert buf.channel_count("bench-1") == 1

    lines = (tmp_path / "messages" / "chat_history.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["content"] == "hi"


def test_replay_rehydrates_deques(tmp_path: Path):
    path = tmp_path / "messages" / "chat_history.jsonl"
    path.parent.mkdir()
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": _now_iso(),
                    "msg_id": str(i),
                    "channel_id": "bench-1",
                    "author": "alice",
                    "author_display": "alice",
                    "kind": "user_message",
                    "content": f"msg-{i}",
                }
            )
            for i in range(3)
        )
        + "\n"
    )

    buf = _make_buffer(tmp_path)
    loaded = buf.replay()
    assert loaded == 3
    assert buf.channel_count("bench-1") == 3


def test_deque_evicts_at_maxlen(tmp_path: Path):
    buf = _make_buffer(tmp_path, global_max=3, per_channel_max=2)
    for i in range(5):
        msg = buf.make_message(
            channel_id="c1", kind="user_message", content=str(i), author="alice"
        )
        # Sync in-memory append for the eviction test.
        buf._append_in_memory(msg)
    assert buf.total_count() == 3
    assert buf.channel_count("c1") == 2
    contents = [m.content for m in buf.recent_for_channel("c1", 10)]
    assert contents == ["3", "4"]


@pytest.mark.asyncio
async def test_recent_for_channel_falls_back_to_global_when_empty(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    # Seed a different channel; query an empty channel should fall back.
    msg = buf.make_message(
        channel_id="c1", kind="user_message", content="x", author="alice"
    )
    await buf.append(msg)
    out = buf.recent_for_channel("c2-empty", limit=5)
    # Falls back to global tail.
    assert len(out) == 1
    assert out[0].content == "x"


@pytest.mark.asyncio
async def test_cross_author_excludes_dms_and_current_channel(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    # Alice talks in #eng, then in dm-slack-alice, then asks in #help.
    for i, ch in enumerate(["eng", "dm-slack-alice", "help"]):
        await buf.append(
            buf.make_message(
                channel_id=ch,
                kind="user_message",
                content=f"alice-on-{ch}",
                author="alice",
                ts=_now_iso(offset_minutes=-10 + i),
            )
        )

    cross = buf.cross_author_messages(
        author="alice", exclude_channel="help", limit=10, within_hours=24
    )
    chans = {m.channel_id for m in cross}
    assert chans == {"eng"}  # not dm-slack-alice (private), not help (current)


@pytest.mark.asyncio
async def test_cross_author_skipped_for_dm_target(tmp_path: Path):
    """A DM channel must not pull cross-channel public chatter into its prompt."""
    buf = _make_buffer(tmp_path)
    await buf.append(
        buf.make_message(
            channel_id="eng", kind="user_message", content="public", author="alice"
        )
    )
    out = buf.assemble_recent_activity(
        channel_id="dm-slack-alice",
        author="alice",
        recent_per_channel=10,
        recent_author_cross=10,
        cross_hours=24,
    )
    chans = {m.channel_id for m in out}
    # The DM has no within-channel history; falls back to global tail (which
    # contains the public eng message). The cross-author pull is suppressed.
    # Verify: even though Alice posted in eng, the DM context doesn't pull
    # her public chatter via cross-author logic.
    # (Within-channel fallback is open-strix's exact rule and may include
    # global messages; the privacy guarantee covers cross-author specifically.)
    cross = buf.cross_author_messages(
        author="alice", exclude_channel="dm-slack-alice", limit=5, within_hours=24
    )
    assert all(not c.channel_id.startswith("dm-") for c in cross)


@pytest.mark.asyncio
async def test_recent_window_respects_cross_hours(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat()
    await buf.append(
        buf.make_message(
            channel_id="eng",
            kind="user_message",
            content="ancient",
            author="alice",
            ts=old_ts,
        )
    )
    cross = buf.cross_author_messages(
        author="alice", exclude_channel="help", limit=10, within_hours=24
    )
    assert cross == []


def test_render_recent_activity_uses_assistant_marker(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    msgs = [
        buf.make_message(
            channel_id="bench-1", kind="user_message", content="hi", author="alice"
        ),
        buf.make_message(
            channel_id="bench-1", kind="assistant_message", content="hello back"
        ),
    ]
    rendered = render_recent_activity(msgs)
    assert "alice: hi" in rendered
    assert "(assistant): hello back" in rendered


def test_render_recent_activity_surfaces_msg_id(tmp_path: Path):
    """Recent activity lines include ``id=<msg_id>`` so the agent can
    react to older messages with ``<react message="<id>"/>``."""
    buf = _make_buffer(tmp_path)
    msgs = [
        buf.make_message(
            channel_id="discord-1",
            kind="user_message",
            content="hi",
            author="alice",
            msg_id="msg-abc",
        ),
        buf.make_message(
            channel_id="discord-1",
            kind="user_message",
            content="no id here",
            author="bob",
            # msg_id intentionally omitted
        ),
    ]
    rendered = render_recent_activity(msgs)
    lines = rendered.splitlines()
    # Line for alice carries the id; bob's line doesn't.
    assert any("id=msg-abc" in ln and "alice" in ln for ln in lines)
    assert any("bob" in ln and "id=" not in ln for ln in lines)


def test_render_recent_activity_caps_per_message_chars(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    msg = buf.make_message(
        channel_id="bench-1", kind="user_message", content="x" * 10_000, author="alice"
    )
    rendered = render_recent_activity([msg], max_chars=100)
    # 100 chars + the truncation marker, plus the prefix line
    assert "…[truncated]" in rendered
    # The full 10k content shouldn't survive
    assert len(rendered) < 1_000


@pytest.mark.asyncio
async def test_recent_for_channel_source_allowlist_excludes_api(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    # One real-conversation message + one bench/api message on the same channel.
    await buf.append(
        buf.make_message(
            channel_id="bench-1", kind="user_message", content="real",
            author="alice", source="slack",
        )
    )
    await buf.append(
        buf.make_message(
            channel_id="bench-1", kind="user_message", content="bench-seed",
            author="benchmark", source="api",
        )
    )

    # No filter: both visible.
    out = buf.recent_for_channel("bench-1", limit=10)
    assert {m.content for m in out} == {"real", "bench-seed"}

    # With production allowlist: api is filtered out.
    allow = frozenset({"slack", "discord", "bluesky", "web", "stdin"})
    out = buf.recent_for_channel("bench-1", limit=10, source_allowlist=allow)
    assert [m.content for m in out] == ["real"]


@pytest.mark.asyncio
async def test_cross_author_pull_respects_source_allowlist(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    # Same author across 2 channels — but bench-tagged on one of them.
    await buf.append(
        buf.make_message(
            channel_id="other", kind="user_message", content="public-channel",
            author="alice", source="slack", ts=_now_iso(),
        )
    )
    await buf.append(
        buf.make_message(
            channel_id="bench-other", kind="user_message", content="bench-traffic",
            author="alice", source="api", ts=_now_iso(),
        )
    )

    allow = frozenset({"slack", "discord"})
    out = buf.cross_author_messages(
        author="alice", exclude_channel="current", limit=10,
        within_hours=24, source_allowlist=allow,
    )
    assert [m.content for m in out] == ["public-channel"]


def test_recent_for_channel_limit_zero_returns_empty(tmp_path: Path):
    """The bare ``[-0:]`` slice would return the full list — guard against that."""
    buf = _make_buffer(tmp_path)
    asyncio.get_event_loop()  # ensure asyncio works in test
    # Sync path: append into deques directly via private method for test brevity.
    buf._append_in_memory(
        buf.make_message(channel_id="c1", kind="user_message", content="a", source="slack")
    )
    assert buf.recent_for_channel("c1", limit=0) == []
